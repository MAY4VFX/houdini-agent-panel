"""Tests for installing agents: download+sha256, Zip Slip, idempotency."""

from __future__ import annotations

import os
import hashlib
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from houdini_agent_panel import paths, runtime
from houdini_agent_panel.registry import AgentEntry, BinaryDistribution, NpxDistribution
from houdini_agent_panel.settings import CustomAgent, Settings


def _add_tar_file(tf: tarfile.TarFile, arcname: str, content: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name=arcname)
    info.size = len(content)
    info.mode = mode
    tf.addfile(info, io.BytesIO(content))


def _build_agent_tar(dest: Path, cmd_name: str = "myagent", content: bytes = b"#!/bin/sh\necho hi\n") -> bytes:
    archive_path = dest / "agent.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tf:
        _add_tar_file(tf, cmd_name, content, mode=0o755)
    return archive_path.read_bytes()


# --- download_and_verify ------------------------------------------------------


def test_download_and_verify_writes_dest_on_matching_hash(tmp_path, fetcher):
    payload = b"hello world"
    fetcher.add_bytes("https://example.test/file.bin", payload)
    digest = hashlib.sha256(payload).hexdigest()
    dest = tmp_path / "out" / "file.bin"

    result = runtime.download_and_verify(
        "https://example.test/file.bin", digest, dest, fetch=fetcher
    )

    assert result == dest
    assert dest.read_bytes() == payload
    assert not dest.with_name(dest.name + ".part").exists()


def test_download_and_verify_checksum_mismatch_leaves_nothing(tmp_path, fetcher):
    payload = b"hello world"
    fetcher.add_bytes("https://example.test/file.bin", payload)
    dest = tmp_path / "out" / "file.bin"

    with pytest.raises(runtime.ChecksumError):
        runtime.download_and_verify("https://example.test/file.bin", "0" * 64, dest, fetch=fetcher)

    assert not dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()


def test_download_and_verify_network_error_leaves_nothing(tmp_path, fetcher):
    from houdini_agent_panel.network import NetworkError

    dest = tmp_path / "out" / "file.bin"
    with pytest.raises(NetworkError):
        # URL isn't registered in FakeFetcher -> NetworkError.
        runtime.download_and_verify("https://example.test/missing.bin", "0" * 64, dest, fetch=fetcher)
    assert not dest.exists()
    assert not dest.with_name(dest.name + ".part").exists()


# --- extract_archive: happy path + Zip Slip ----------------------------------


def test_extract_archive_tar_gz(tmp_path):
    archive = tmp_path / "a.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        _add_tar_file(tf, "root/bin/run", b"content", mode=0o755)
    dest = tmp_path / "out"

    runtime.extract_archive(archive, dest)

    extracted = dest / "root" / "bin" / "run"
    assert extracted.read_bytes() == b"content"


def test_extract_archive_zip(tmp_path):
    archive = tmp_path / "a.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("root/file.txt", "content")
    dest = tmp_path / "out"

    runtime.extract_archive(archive, dest)

    assert (dest / "root" / "file.txt").read_text() == "content"


def test_extract_archive_rejects_dotdot_in_tar(tmp_path):
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        _add_tar_file(tf, "../../etc/evil.txt", b"pwned")
    dest = tmp_path / "sandbox" / "out"
    dest.mkdir(parents=True)

    with pytest.raises(runtime.InstallError):
        runtime.extract_archive(archive, dest)

    assert not (tmp_path / "etc" / "evil.txt").exists()
    assert not (tmp_path / "sandbox" / "etc").exists()


def test_extract_archive_rejects_absolute_path_in_tar(tmp_path):
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        _add_tar_file(tf, "/etc/evil.txt", b"pwned")
    dest = tmp_path / "out"

    with pytest.raises(runtime.InstallError):
        runtime.extract_archive(archive, dest)

    assert not Path("/etc/evil.txt").exists()


def test_extract_archive_rejects_dotdot_in_zip(tmp_path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("../../etc/evil.txt", "pwned")
    dest = tmp_path / "sandbox" / "out"
    dest.mkdir(parents=True)

    with pytest.raises(runtime.InstallError):
        runtime.extract_archive(archive, dest)

    assert not (tmp_path / "etc" / "evil.txt").exists()


def test_extract_archive_does_not_follow_outward_symlink(tmp_path):
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tf:
        link_info = tarfile.TarInfo(name="link")
        link_info.type = tarfile.SYMTYPE
        link_info.linkname = "/etc/passwd"
        tf.addfile(link_info)
    dest = tmp_path / "out"

    runtime.extract_archive(archive, dest)  # must not raise

    assert not (dest / "link").exists()  # the symlink is simply never created


def test_extract_archive_unknown_format_raises(tmp_path):
    archive = tmp_path / "a.rar"
    archive.write_bytes(b"not really an archive")
    with pytest.raises(runtime.InstallError):
        runtime.extract_archive(archive, tmp_path / "out")


# --- install_agent: npx -------------------------------------------------------


def _npx_entry(version: str = "1.0.0") -> AgentEntry:
    return AgentEntry(
        id="test-npx-agent",
        name="Test Npx Agent",
        version=version,
        npx=NpxDistribution(package="@test/agent@1.0.0", args=["--acp"], env={"FOO": "bar"}),
    )


def test_install_agent_npx_ensures_node_and_writes_manifest(monkeypatch):
    entry = _npx_entry()
    fake_node = Path("/fake/node")
    monkeypatch.setattr(
        "houdini_agent_panel.node.ensure_node", lambda **k: fake_node
    )
    monkeypatch.setattr(
        "houdini_agent_panel.node.npx_argv",
        lambda node_bin, package, args: [str(node_bin), "/fake/npx-cli.js", "--yes", package, *args],
    )

    spec = runtime.install_agent(entry)

    assert spec.command == str(fake_node)
    assert spec.args == ["/fake/npx-cli.js", "--yes", "@test/agent@1.0.0", "--acp"]
    assert spec.env["FOO"] == "bar"

    # A PATH pointing to our Node is mandatory: npx-cli.js spawns child
    # processes with the `node` command and looks for it on PATH. Without
    # this, the agent on a machine without Node dies before its first byte
    # — a regression caught only by a live run.
    assert spec.env["PATH"].split(os.pathsep)[0] == str(fake_node.parent)
    assert runtime.is_installed(entry)
    assert runtime.installed_version(entry.id) == "1.0.0"


def test_install_agent_npx_is_idempotent(monkeypatch):
    # "Idempotent" here is about not re-downloading the package/Node over
    # the NETWORK, not about launch_spec never touching node.ensure_node()
    # at all: that call is cheap (find_system_node is just shutil.which)
    # and gets called every time a LaunchSpec is built, including for an
    # already-installed agent.
    entry = _npx_entry()
    monkeypatch.setattr(
        "houdini_agent_panel.node.ensure_node", lambda **k: Path("/fake/node")
    )
    monkeypatch.setattr(
        "houdini_agent_panel.node.npx_argv",
        lambda node_bin, package, args: [str(node_bin), "/fake/npx-cli.js", "--yes", package, *args],
    )

    first = runtime.install_agent(entry)
    manifest = runtime._manifest_path_readonly(entry.id)
    installed_at_after_first = manifest.read_text("utf-8")

    second = runtime.install_agent(entry)

    assert second == first
    # reinstalling doesn't rewrite the manifest again.
    assert manifest.read_text("utf-8") == installed_at_after_first


# --- install_agent: binary -----------------------------------------------------


def _binary_entry(tmp_path: Path, *, sha256: str | None, version: str = "1.0.0") -> tuple[AgentEntry, bytes]:
    archive_bytes = _build_agent_tar(tmp_path, cmd_name="myagent")
    digest = sha256 if sha256 is not None else hashlib.sha256(archive_bytes).hexdigest()
    entry = AgentEntry(
        id="test-binary-agent",
        name="Test Binary Agent",
        version=version,
        binaries={
            "fake-platform": BinaryDistribution(
                archive="https://example.test/myagent.tar.gz",
                cmd="./myagent",
                args=["serve"],
                sha256=digest,
            )
        },
    )
    return entry, archive_bytes


def test_install_agent_binary_downloads_verifies_extracts(tmp_path, fetcher, monkeypatch):
    monkeypatch.setattr("houdini_agent_panel.runtime.platform_key", lambda: "fake-platform")
    entry, archive_bytes = _binary_entry(tmp_path, sha256=None)
    fetcher.add_bytes(entry.binaries["fake-platform"].archive, archive_bytes)

    spec = runtime.install_agent(entry, fetch=fetcher)

    version_dir = paths.agent_dir(entry.id) / entry.version
    assert Path(spec.command) == version_dir / "myagent"
    assert Path(spec.command).exists()
    assert spec.args == ["serve"]
    assert spec.env == {}
    assert runtime.is_installed(entry)


def test_install_agent_binary_checksum_mismatch_installs_nothing(tmp_path, fetcher, monkeypatch):
    monkeypatch.setattr("houdini_agent_panel.runtime.platform_key", lambda: "fake-platform")
    entry, archive_bytes = _binary_entry(tmp_path, sha256="0" * 64)
    fetcher.add_bytes(entry.binaries["fake-platform"].archive, archive_bytes)

    with pytest.raises(runtime.ChecksumError):
        runtime.install_agent(entry, fetch=fetcher)

    assert not runtime.is_installed(entry)
    assert not (paths.agent_dir(entry.id) / entry.version).exists()


def test_install_agent_binary_missing_sha256_refuses_install(tmp_path, fetcher, monkeypatch):
    monkeypatch.setattr("houdini_agent_panel.runtime.platform_key", lambda: "fake-platform")
    entry = AgentEntry(
        id="test-no-sha",
        name="No Sha",
        version="1.0.0",
        binaries={
            "fake-platform": BinaryDistribution(
                archive="https://example.test/x.tar.gz", cmd="./x", sha256=""
            )
        },
    )
    with pytest.raises(runtime.InstallError):
        runtime.install_agent(entry, fetch=fetcher)
    assert fetcher.calls == []  # didn't even try to download — nothing to verify against


def test_install_agent_unavailable_platform_raises_with_reason(monkeypatch):
    monkeypatch.setattr("houdini_agent_panel.runtime.platform_key", lambda: "darwin-x86_64")
    entry = AgentEntry(
        id="kimi-like",
        name="Kimi-like",
        version="1.0.0",
        binaries={"darwin-aarch64": BinaryDistribution(archive="x", cmd="./x", sha256="a" * 64)},
    )
    with pytest.raises(runtime.InstallError, match="darwin-x86_64"):
        runtime.install_agent(entry)


def test_install_agent_binary_is_idempotent(tmp_path, fetcher, monkeypatch):
    monkeypatch.setattr("houdini_agent_panel.runtime.platform_key", lambda: "fake-platform")
    entry, archive_bytes = _binary_entry(tmp_path, sha256=None)
    fetcher.add_bytes(entry.binaries["fake-platform"].archive, archive_bytes)

    runtime.install_agent(entry, fetch=fetcher)
    assert len(fetcher.calls) == 1

    runtime.install_agent(entry, fetch=fetcher)
    assert len(fetcher.calls) == 1  # same version — didn't download it a second time


# --- uninstall_agent / launch_spec -------------------------------------------


def test_installed_version_does_not_create_directory_for_unknown_agent():
    # is_installed()/installed_version() are called for every agent in the
    # registry when rendering the "Agents" screen — a plain status check
    # must not create empty folders on disk for agents nobody ever installed.
    assert runtime.installed_version("never-installed-agent") is None
    assert not (paths.agents_dir() / "never-installed-agent").exists()


def test_uninstall_agent_removes_only_its_own_directory(tmp_path, fetcher, monkeypatch):
    monkeypatch.setattr("houdini_agent_panel.runtime.platform_key", lambda: "fake-platform")
    entry, archive_bytes = _binary_entry(tmp_path, sha256=None)
    fetcher.add_bytes(entry.binaries["fake-platform"].archive, archive_bytes)
    runtime.install_agent(entry, fetch=fetcher)

    sibling_dir = paths.agent_dir("sibling-agent")
    sibling_marker = sibling_dir / "keep-me.txt"
    sibling_marker.write_text("still here")

    runtime.uninstall_agent(entry.id)

    # We don't check this via paths.agent_dir() here: it creates the
    # directory as a side effect (paths._sub does a mkdir on every access),
    # and checking "doesn't exist" through it would recreate it right there.
    assert not (paths.agents_dir() / entry.id).exists()
    assert sibling_marker.exists()
    assert runtime.installed_version(entry.id) is None


def test_launch_spec_binary_requires_prior_install(monkeypatch):
    monkeypatch.setattr("houdini_agent_panel.runtime.platform_key", lambda: "fake-platform")
    entry = AgentEntry(
        id="not-installed-agent",
        name="Not Installed",
        version="1.0.0",
        binaries={"fake-platform": BinaryDistribution(archive="x", cmd="./x", sha256="a" * 64)},
    )
    with pytest.raises(runtime.InstallError):
        runtime.launch_spec(entry)


def test_custom_launch_spec_passes_through():
    agent = CustomAgent(id="my", name="My", command="/usr/bin/my-agent", args=["--flag"], env={"X": "1"})
    spec = runtime.custom_launch_spec(agent)
    assert spec.command == "/usr/bin/my-agent"
    assert spec.args == ["--flag"]
    assert spec.env == {"X": "1"}


# --- studio proxy: launch_spec / custom_launch_spec / install_agent ----------


def test_custom_agent_launch_carries_the_proxy(monkeypatch):
    monkeypatch.setattr(os, "environ", {})
    agent = CustomAgent(id="mine", name="Mine", command="/bin/echo")
    spec = runtime.custom_launch_spec(agent, settings=Settings(proxy_url="http://studio:8080"))
    assert spec.env["HTTPS_PROXY"] == "http://studio:8080"
    assert "localhost" in spec.env["NO_PROXY"]


def test_agent_env_wins_over_the_studio_proxy(monkeypatch):
    # An artist who set HTTPS_PROXY on one custom agent meant it for that
    # agent. The global setting is a default, not an override.
    monkeypatch.setattr(os, "environ", {})
    agent = CustomAgent(
        id="mine", name="Mine", command="/bin/echo", env={"HTTPS_PROXY": "http://mine:9000"}
    )
    spec = runtime.custom_launch_spec(agent, settings=Settings(proxy_url="http://studio:8080"))
    assert spec.env["HTTPS_PROXY"] == "http://mine:9000"


def test_no_proxy_configured_adds_no_variables(monkeypatch):
    monkeypatch.setattr(os, "environ", {})
    agent = CustomAgent(id="mine", name="Mine", command="/bin/echo")
    spec = runtime.custom_launch_spec(agent, settings=Settings())
    assert spec.env == {}


def test_launch_spec_binary_carries_the_proxy(monkeypatch, tmp_path, fetcher):
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr("houdini_agent_panel.runtime.platform_key", lambda: "fake-platform")
    entry, archive_bytes = _binary_entry(tmp_path, sha256=None)
    fetcher.add_bytes(entry.binaries["fake-platform"].archive, archive_bytes)
    runtime.install_agent(entry, fetch=fetcher)

    spec = runtime.launch_spec(entry, settings=Settings(proxy_url="http://studio:8080"))

    assert spec.env["HTTPS_PROXY"] == "http://studio:8080"
    assert "localhost" in spec.env["NO_PROXY"]


# --- captured OAuth token: launch_spec -----------------------------------
#
# `settings.agent_oauth_tokens` is where a token minted by a terminal-auth
# command that prints it once and writes it nowhere else lands (Claude's
# `setup-token`, docs/facts/acp-sdk.md §21) — this is the only other place
# it needs to reach: the agent's own launch environment, injected the same
# way `_with_proxy` already injects the studio proxy.


def test_launch_spec_carries_a_stored_oauth_token(monkeypatch, tmp_path, fetcher):
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr("houdini_agent_panel.runtime.platform_key", lambda: "fake-platform")
    entry, archive_bytes = _binary_entry(tmp_path, sha256=None)
    fetcher.add_bytes(entry.binaries["fake-platform"].archive, archive_bytes)
    runtime.install_agent(entry, fetch=fetcher)

    settings = Settings(agent_oauth_tokens={entry.id: {"CLAUDE_CODE_OAUTH_TOKEN": "fake-token"}})
    spec = runtime.launch_spec(entry, settings=settings)

    assert spec.env["CLAUDE_CODE_OAUTH_TOKEN"] == "fake-token"


def test_launch_spec_never_leaks_another_agents_stored_token(monkeypatch, tmp_path, fetcher):
    """A token is stored per agent id — this must never reach a DIFFERENT
    agent's own launch just because something is on file somewhere."""
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr("houdini_agent_panel.runtime.platform_key", lambda: "fake-platform")
    entry, archive_bytes = _binary_entry(tmp_path, sha256=None)
    fetcher.add_bytes(entry.binaries["fake-platform"].archive, archive_bytes)
    runtime.install_agent(entry, fetch=fetcher)

    settings = Settings(agent_oauth_tokens={"some-other-agent": {"CLAUDE_CODE_OAUTH_TOKEN": "fake-token"}})
    spec = runtime.launch_spec(entry, settings=settings)

    assert "CLAUDE_CODE_OAUTH_TOKEN" not in spec.env


def test_install_agent_npx_fresh_install_carries_the_proxy(monkeypatch):
    # This is the actual bug: the very first launch of a freshly-installed
    # npx agent is the one that runs `npx`'s own registry fetch, and it goes
    # through `install_agent`, not `launch_spec` — a fix that only touched
    # `launch_spec` would leave this path uncovered.
    monkeypatch.setattr(os, "environ", {})
    entry = _npx_entry()
    monkeypatch.setattr("houdini_agent_panel.node.ensure_node", lambda **k: Path("/fake/node"))
    monkeypatch.setattr(
        "houdini_agent_panel.node.npx_argv",
        lambda node_bin, package, args: [str(node_bin), "/fake/npx-cli.js", "--yes", package, *args],
    )

    spec = runtime.install_agent(entry, settings=Settings(proxy_url="http://studio:8080"))

    assert spec.env["HTTPS_PROXY"] == "http://studio:8080"
    assert spec.env["FOO"] == "bar"  # the distribution's own env survives alongside it


def test_extraction_works_when_the_destination_path_has_a_symlink(tmp_path):
    """The Zip Slip guard compared a resolved member against an unresolved
    destination. On macOS everything under /var is really /private/var, so a
    member named plainly "opencode" was judged to escape its own directory —
    and no binary agent could be installed at all. Caught by running a first
    install on a simulated fresh machine, not by reading the check.
    """
    import tarfile

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    payload = tmp_path / "opencode"
    payload.write_text("#!/bin/sh\necho hi\n")
    archive = tmp_path / "agent.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(payload, arcname="opencode")

    runtime.extract_archive(archive, link)

    assert (real / "opencode").is_file(), "a safe archive was rejected via a symlinked path"


# --- an archive whose payload sits inside one wrapping directory ---------------


def _build_wrapped_agent_tar(dest: Path, *, wrapper: str, cmd_name: str = "myagent") -> bytes:
    """The shape GitHub release tooling produces for a Rust/Go binary:
    everything under one directory named after the release, while the
    registry still says `cmd: "./myagent"`."""
    archive_path = dest / "wrapped.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tf:
        _add_tar_file(tf, f"{wrapper}/{cmd_name}", b"#!/bin/sh\necho hi\n", mode=0o755)
        _add_tar_file(tf, f"{wrapper}/README.md", b"docs\n")
    return archive_path.read_bytes()


def test_install_agent_unwraps_a_single_top_level_directory(tmp_path, fetcher, monkeypatch):
    """Without this the install "succeeded" and produced a launch command
    pointing at a file that was never there — the artist only found out
    minutes later, as a bare "agent did not start"."""
    monkeypatch.setattr("houdini_agent_panel.runtime.platform_key", lambda: "fake-platform")
    archive_bytes = _build_wrapped_agent_tar(tmp_path, wrapper="myagent-1.0.0-x86_64-linux")
    entry = AgentEntry(
        id="wrapped-agent",
        name="Wrapped Agent",
        version="1.0.0",
        binaries={
            "fake-platform": BinaryDistribution(
                archive="https://example.test/wrapped.tar.gz",
                cmd="./myagent",
                args=["acp"],
                sha256=hashlib.sha256(archive_bytes).hexdigest(),
            )
        },
    )
    fetcher.add_bytes(entry.binaries["fake-platform"].archive, archive_bytes)

    spec = runtime.install_agent(entry, fetch=fetcher)

    version_dir = paths.agent_dir(entry.id) / entry.version
    assert Path(spec.command) == version_dir / "myagent"
    assert Path(spec.command).exists()
    # The rest of the archive comes along; only the wrapper is stripped.
    assert (version_dir / "README.md").exists()


def test_install_agent_refuses_an_archive_without_the_command(tmp_path, fetcher, monkeypatch):
    """A missing cmd is an INSTALL failure, said here, and not a silent
    manifest for an agent that can never launch."""
    monkeypatch.setattr("houdini_agent_panel.runtime.platform_key", lambda: "fake-platform")
    archive_bytes = _build_agent_tar(tmp_path, cmd_name="something-else")
    entry = AgentEntry(
        id="mismatched-agent",
        name="Mismatched Agent",
        version="1.0.0",
        binaries={
            "fake-platform": BinaryDistribution(
                archive="https://example.test/mismatched.tar.gz",
                cmd="./myagent",
                args=[],
                sha256=hashlib.sha256(archive_bytes).hexdigest(),
            )
        },
    )
    fetcher.add_bytes(entry.binaries["fake-platform"].archive, archive_bytes)

    with pytest.raises(runtime.InstallError, match="did not contain"):
        runtime.install_agent(entry, fetch=fetcher)

    # Nothing on record: the next attempt must be a real one, not a no-op
    # against a version the manifest already claims is installed.
    assert not runtime.is_installed(entry)
    assert not (paths.agent_dir(entry.id) / entry.version).exists()


def test_npx_launch_spec_reports_the_node_directory_for_the_agents_path(tmp_path, monkeypatch):
    """`env["PATH"]` alone cannot be the whole answer: it is built from
    Houdini's PATH, and `client._agent_path` has to rebuild it against the
    artist's login-shell PATH. It needs the directories, not the string."""
    fake_node = tmp_path / "node" / "bin" / "node"
    fake_node.parent.mkdir(parents=True)
    fake_node.write_text("#!/bin/sh\n")
    monkeypatch.setattr("houdini_agent_panel.node.ensure_node", lambda **k: fake_node)
    monkeypatch.setattr(
        "houdini_agent_panel.node.npx_argv",
        lambda node_bin, package, args: [str(node_bin), "/fake/npx-cli.js", package, *args],
    )
    entry = AgentEntry(
        id="npx-agent",
        name="Npx Agent",
        version="1.0.0",
        npx=NpxDistribution(package="@test/agent@1.0.0"),
    )

    spec = runtime.install_agent(entry)

    assert spec.path_prepend == (str(fake_node.parent),)

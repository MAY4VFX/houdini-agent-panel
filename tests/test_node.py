"""Tests for portable Node.js: system/own, downloading, npx-cli.js."""

from __future__ import annotations

import hashlib
import io
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from houdini_agent_panel import node, paths
from houdini_agent_panel.runtime import ChecksumError


def _add_file(tf: tarfile.TarFile, arcname: str, content: bytes, mode: int = 0o644) -> None:
    info = tarfile.TarInfo(name=arcname)
    info.size = len(content)
    info.mode = mode
    tf.addfile(info, io.BytesIO(content))


def _build_node_tar(dest: Path, root_name: str) -> bytes:
    """A minimal but real nodejs.org archive layout: bin/node +
    lib/node_modules/npm/bin/npx-cli.js — exactly what `install_node` and
    `npx_argv` need."""
    archive_path = dest / f"{root_name}.tar.gz"
    with tarfile.open(archive_path, "w:gz") as tf:
        _add_file(tf, f"{root_name}/bin/node", b"#!/bin/sh\necho fake-node\n", mode=0o755)
        _add_file(tf, f"{root_name}/lib/node_modules/npm/bin/npx-cli.js", b"// fake npx-cli\n")
    return archive_path.read_bytes()


# --- find_system_node --------------------------------------------------------


def test_find_system_node_accepts_fresh_enough_version(monkeypatch):
    monkeypatch.setattr(node.shutil, "which", lambda name: "/usr/local/bin/node")
    monkeypatch.setattr(
        node.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="v22.14.0\n", stderr=""),
    )
    result = node.find_system_node()
    assert result == Path("/usr/local/bin/node")


def test_find_system_node_rejects_too_old_version(monkeypatch):
    monkeypatch.setattr(node.shutil, "which", lambda name: "/usr/local/bin/node")
    monkeypatch.setattr(
        node.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="v18.19.0\n", stderr=""),
    )
    assert node.find_system_node() is None


def test_find_system_node_absent_from_path(monkeypatch):
    monkeypatch.setattr(node.shutil, "which", lambda name: None)
    assert node.find_system_node() is None


def test_find_system_node_garbage_output_treated_as_absent(monkeypatch):
    monkeypatch.setattr(node.shutil, "which", lambda name: "/usr/local/bin/node")
    monkeypatch.setattr(
        node.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="not a version\n", stderr=""),
    )
    assert node.find_system_node() is None


def test_find_system_node_broken_binary_treated_as_absent(monkeypatch):
    monkeypatch.setattr(node.shutil, "which", lambda name: "/usr/local/bin/node")

    def explode(*a, **k):
        raise OSError("permission denied")

    monkeypatch.setattr(node.subprocess, "run", explode)
    assert node.find_system_node() is None


# --- node_platform / dist_url / shasums_url ---------------------------------


@pytest.mark.parametrize(
    "system, machine, expected",
    [
        ("Darwin", "arm64", ("darwin", "arm64")),
        ("Darwin", "x86_64", ("darwin", "x64")),
        ("Linux", "aarch64", ("linux", "arm64")),
        ("Linux", "x86_64", ("linux", "x64")),
        ("Windows", "AMD64", ("win", "x64")),
    ],
)
def test_node_platform(monkeypatch, system, machine, expected):
    monkeypatch.setattr(node.platform, "system", lambda: system)
    monkeypatch.setattr(node.platform, "machine", lambda: machine)
    assert node.node_platform() == expected


def test_dist_url_darwin_arm(monkeypatch):
    monkeypatch.setattr(node.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(node.platform, "machine", lambda: "arm64")
    assert node.dist_url("22.14.0") == (
        "https://nodejs.org/dist/v22.14.0/node-v22.14.0-darwin-arm64.tar.gz"
    )


def test_dist_url_windows_uses_zip(monkeypatch):
    monkeypatch.setattr(node.platform, "system", lambda: "Windows")
    monkeypatch.setattr(node.platform, "machine", lambda: "AMD64")
    assert node.dist_url("22.14.0") == (
        "https://nodejs.org/dist/v22.14.0/node-v22.14.0-win-x64.zip"
    )


def test_shasums_url():
    assert node.shasums_url("22.14.0") == "https://nodejs.org/dist/v22.14.0/SHASUMS256.txt"


# --- install_node -------------------------------------------------------------


def test_install_node_downloads_verifies_and_extracts(tmp_path, fetcher, monkeypatch):
    monkeypatch.setattr(node.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(node.platform, "machine", lambda: "arm64")

    version = "22.14.0"
    root_name = f"node-v{version}-darwin-arm64"
    archive_bytes = _build_node_tar(tmp_path, root_name)
    digest = hashlib.sha256(archive_bytes).hexdigest()

    archive_url = node.dist_url(version)
    shasums_url = node.shasums_url(version)
    fetcher.add_bytes(archive_url, archive_bytes)
    fetcher.add_bytes(shasums_url, f"{digest}  {root_name}.tar.gz\n".encode("utf-8"))

    result = node.install_node(version=version, fetch=fetcher)

    assert result == paths.node_dir() / version / "bin" / "node"
    assert result.read_bytes() == b"#!/bin/sh\necho fake-node\n"
    npx_cli = paths.node_dir() / version / "lib" / "node_modules" / "npm" / "bin" / "npx-cli.js"
    assert npx_cli.exists()
    assert set(fetcher.calls) == {archive_url, shasums_url}


def test_install_node_is_idempotent(tmp_path, fetcher, monkeypatch):
    monkeypatch.setattr(node.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(node.platform, "machine", lambda: "arm64")

    version = "22.14.0"
    root_name = f"node-v{version}-darwin-arm64"
    archive_bytes = _build_node_tar(tmp_path, root_name)
    digest = hashlib.sha256(archive_bytes).hexdigest()
    fetcher.add_bytes(node.dist_url(version), archive_bytes)
    fetcher.add_bytes(node.shasums_url(version), f"{digest}  {root_name}.tar.gz\n".encode("utf-8"))

    node.install_node(version=version, fetch=fetcher)
    calls_after_first = list(fetcher.calls)

    result = node.install_node(version=version, fetch=fetcher)
    assert fetcher.calls == calls_after_first  # didn't hit the network the second time
    assert result.exists()


def test_install_node_checksum_mismatch_leaves_nothing_on_disk(tmp_path, fetcher, monkeypatch):
    monkeypatch.setattr(node.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(node.platform, "machine", lambda: "arm64")

    version = "22.14.0"
    root_name = f"node-v{version}-darwin-arm64"
    archive_bytes = _build_node_tar(tmp_path, root_name)
    wrong_digest = "0" * 64
    fetcher.add_bytes(node.dist_url(version), archive_bytes)
    fetcher.add_bytes(
        node.shasums_url(version), f"{wrong_digest}  {root_name}.tar.gz\n".encode("utf-8")
    )

    with pytest.raises(ChecksumError):
        node.install_node(version=version, fetch=fetcher)

    assert not (paths.node_dir() / version).exists()
    # no leftover temp files either
    assert list(paths.node_dir().iterdir()) == []


def test_install_node_missing_shasums_entry_raises_without_downloading_archive(
    tmp_path, fetcher, monkeypatch
):
    monkeypatch.setattr(node.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(node.platform, "machine", lambda: "arm64")

    version = "22.14.0"
    fetcher.add_bytes(node.shasums_url(version), b"deadbeef  some-other-file.tar.gz\n")
    # archive_url is deliberately not registered — if install_node tried to
    # download it, FakeFetcher would raise NetworkError instead of our
    # ChecksumError.

    with pytest.raises(ChecksumError):
        node.install_node(version=version, fetch=fetcher)


# --- ensure_node --------------------------------------------------------------


def test_ensure_node_prefers_system_node(monkeypatch):
    system_path = Path("/usr/local/bin/node")
    monkeypatch.setattr(node, "find_system_node", lambda **k: system_path)

    def explode(*a, **k):
        raise AssertionError("install_node must not be called when a system node exists")

    monkeypatch.setattr(node, "install_node", explode)
    assert node.ensure_node() == system_path


def test_ensure_node_falls_back_to_install(monkeypatch, fetcher):
    monkeypatch.setattr(node, "find_system_node", lambda **k: None)
    called = {}

    def fake_install(*, version=node.NODE_VERSION, progress=None, fetch=None):
        called["fetch"] = fetch
        return Path("/fake/node")

    monkeypatch.setattr(node, "install_node", fake_install)
    result = node.ensure_node(fetch=fetcher)
    assert result == Path("/fake/node")
    assert called["fetch"] is fetcher


# --- npx_argv / _npx_cli_path -------------------------------------------------


def test_npx_argv_posix_layout(tmp_path):
    root = tmp_path / "node-v22.14.0-darwin-arm64"
    node_bin = root / "bin" / "node"
    node_bin.parent.mkdir(parents=True)
    node_bin.write_text("fake")
    npx_cli = root / "lib" / "node_modules" / "npm" / "bin" / "npx-cli.js"
    npx_cli.parent.mkdir(parents=True)
    npx_cli.write_text("fake")

    argv = node.npx_argv(node_bin, "@scope/pkg@1.0.0", ["--acp"])
    assert argv == [str(node_bin), str(npx_cli), "--yes", "@scope/pkg@1.0.0", "--acp"]


def test_npx_argv_windows_layout(tmp_path):
    root = tmp_path / "node-v22.14.0-win-x64"
    root.mkdir(parents=True)
    node_bin = root / "node.exe"
    node_bin.write_text("fake")
    npx_cli = root / "node_modules" / "npm" / "bin" / "npx-cli.js"
    npx_cli.parent.mkdir(parents=True)
    npx_cli.write_text("fake")

    argv = node.npx_argv(node_bin, "pkg", [])
    assert argv == [str(node_bin), str(npx_cli), "--yes", "pkg"]


def test_npx_argv_resolves_symlinked_system_node(tmp_path):
    real_root = tmp_path / "real" / "node-v22.14.0-darwin-arm64"
    real_bin = real_root / "bin" / "node"
    real_bin.parent.mkdir(parents=True)
    real_bin.write_text("fake")
    npx_cli = real_root / "lib" / "node_modules" / "npm" / "bin" / "npx-cli.js"
    npx_cli.parent.mkdir(parents=True)
    npx_cli.write_text("fake")

    symlink_bin = tmp_path / "bin" / "node"
    symlink_bin.parent.mkdir(parents=True)
    symlink_bin.symlink_to(real_bin)

    argv = node.npx_argv(symlink_bin, "pkg", [])
    assert argv[1] == str(npx_cli)


# --- npm_cache_dir -------------------------------------------------------


def test_npm_cache_dir_defaults_to_home_dot_npm(monkeypatch, tmp_path):
    monkeypatch.setattr(node.platform, "system", lambda: "Darwin")
    monkeypatch.delenv("NPM_CONFIG_CACHE", raising=False)
    monkeypatch.delenv("npm_config_cache", raising=False)
    monkeypatch.setattr(node.Path, "home", lambda: tmp_path)
    assert node.npm_cache_dir() == tmp_path / ".npm"


def test_npm_cache_dir_honours_upper_case_override(monkeypatch, tmp_path):
    monkeypatch.setenv("NPM_CONFIG_CACHE", str(tmp_path / "custom-cache"))
    assert node.npm_cache_dir() == tmp_path / "custom-cache"


def test_npm_cache_dir_honours_lower_case_override(monkeypatch, tmp_path):
    monkeypatch.delenv("NPM_CONFIG_CACHE", raising=False)
    monkeypatch.setenv("npm_config_cache", str(tmp_path / "custom-cache"))
    assert node.npm_cache_dir() == tmp_path / "custom-cache"


def test_npm_cache_dir_windows_default_uses_localappdata(monkeypatch, tmp_path):
    monkeypatch.setattr(node.platform, "system", lambda: "Windows")
    monkeypatch.delenv("NPM_CONFIG_CACHE", raising=False)
    monkeypatch.delenv("npm_config_cache", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    assert node.npm_cache_dir() == tmp_path / "npm-cache"


# --- find_cached_npx_binary ------------------------------------------------


def _fake_runnable_binary(path: Path, version: str = "1.0.0") -> None:
    """A real, tiny, runnable script — same idea as
    `test_self_update_worker.py`'s own fake `uvx`: `sys.executable` can't
    be the file itself here (there's no `-c` script text to hand it), so
    this writes an actual file with a shebang, matching what a real
    executable on disk looks like."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!{sys.executable}\nimport sys\nprint("{version}")\nsys.exit(0)\n')
    path.chmod(0o755)


def _fake_broken_binary(path: Path) -> None:
    """Looks like a binary (exists, executable bit set) but fails to run —
    the "half-downloaded or wrong-architecture leftover" case."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(1)\n")
    path.chmod(0o755)


def test_find_cached_npx_binary_finds_it_inside_a_hash_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("NPM_CONFIG_CACHE", str(tmp_path))
    binary = tmp_path / "_npx" / "abc123" / "node_modules" / "@anthropic-ai" / (
        "claude-agent-sdk-darwin-arm64"
    ) / "claude"
    _fake_runnable_binary(binary)

    found = node.find_cached_npx_binary("@anthropic-ai", "claude-agent-sdk-", "claude")
    assert found == binary


def test_find_cached_npx_binary_none_when_cache_root_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("NPM_CONFIG_CACHE", str(tmp_path / "does-not-exist"))
    assert node.find_cached_npx_binary("@anthropic-ai", "claude-agent-sdk-", "claude") is None


def test_find_cached_npx_binary_none_when_nothing_matches(tmp_path, monkeypatch):
    monkeypatch.setenv("NPM_CONFIG_CACHE", str(tmp_path))
    other = tmp_path / "_npx" / "abc123" / "node_modules" / "@other-scope" / "some-pkg" / "claude"
    _fake_runnable_binary(other)
    assert node.find_cached_npx_binary("@anthropic-ai", "claude-agent-sdk-", "claude") is None


def test_find_cached_npx_binary_skips_a_binary_that_does_not_run(tmp_path, monkeypatch):
    """The exact "half-downloaded or wrong-architecture leftover" case —
    a path existing proves nothing (same discipline as `mcp_runtime.
    find()`'s own search)."""
    monkeypatch.setenv("NPM_CONFIG_CACHE", str(tmp_path))
    broken = tmp_path / "_npx" / "broken-hash" / "node_modules" / "@anthropic-ai" / (
        "claude-agent-sdk-linux-x64"
    ) / "claude"
    _fake_broken_binary(broken)
    working = tmp_path / "_npx" / "working-hash" / "node_modules" / "@anthropic-ai" / (
        "claude-agent-sdk-linux-x64"
    ) / "claude"
    _fake_runnable_binary(working)

    found = node.find_cached_npx_binary("@anthropic-ai", "claude-agent-sdk-", "claude")
    assert found == working


def test_find_cached_npx_binary_picks_the_newest_when_several_work(tmp_path, monkeypatch):
    """npx keys its cache by content hash, not package name — measured on
    two real machines (this Mac, a Linux box): the SAME package can end
    up under several different hash directories at once, all left over
    from earlier resolutions. Nothing else distinguishes them, so the
    newest wins."""
    monkeypatch.setenv("NPM_CONFIG_CACHE", str(tmp_path))
    older = tmp_path / "_npx" / "older-hash" / "node_modules" / "@anthropic-ai" / (
        "claude-agent-sdk-darwin-arm64"
    ) / "claude"
    newer = tmp_path / "_npx" / "newer-hash" / "node_modules" / "@anthropic-ai" / (
        "claude-agent-sdk-darwin-arm64"
    ) / "claude"
    _fake_runnable_binary(older, version="1.0.0")
    _fake_runnable_binary(newer, version="2.0.0")
    # Both now exist; force a real mtime difference rather than trusting
    # write order alone (some filesystems have coarse mtime resolution).
    import os
    import time

    now = time.time()
    os.utime(older, (now - 100, now - 100))
    os.utime(newer, (now, now))

    found = node.find_cached_npx_binary("@anthropic-ai", "claude-agent-sdk-", "claude")
    assert found == newer


def test_find_cached_npx_binary_matches_by_prefix_not_exact_platform_suffix(tmp_path, monkeypatch):
    """No second mapping table needed to compute the exact platform
    suffix — any package starting with `name_prefix` is tried, and a
    wrong-architecture one simply fails to run and gets skipped on its
    own (`test_find_cached_npx_binary_skips_a_binary_that_does_not_run`
    already covers that half; this confirms the prefix match itself
    doesn't require an exact suffix)."""
    monkeypatch.setenv("NPM_CONFIG_CACHE", str(tmp_path))
    binary = tmp_path / "_npx" / "abc" / "node_modules" / "@anthropic-ai" / (
        "claude-agent-sdk-linux-arm64"
    ) / "claude"
    _fake_runnable_binary(binary)
    assert node.find_cached_npx_binary("@anthropic-ai", "claude-agent-sdk-", "claude") == binary


def test_runs_true_for_a_real_working_binary(tmp_path):
    binary = tmp_path / "claude"
    _fake_runnable_binary(binary)
    assert node._runs(binary) is True


def test_runs_false_for_a_binary_that_exits_nonzero(tmp_path):
    binary = tmp_path / "claude"
    _fake_broken_binary(binary)
    assert node._runs(binary) is False


def test_runs_false_for_a_missing_file(tmp_path):
    assert node._runs(tmp_path / "does-not-exist") is False

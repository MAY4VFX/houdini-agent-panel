"""Тесты установки агентов: скачивание+sha256, Zip Slip, идемпотентность."""

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
from houdini_agent_panel.settings import CustomAgent


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
        # URL не зарегистрирован в FakeFetcher -> NetworkError.
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

    runtime.extract_archive(archive, dest)  # не должно упасть

    assert not (dest / "link").exists()  # симлинк просто не создан


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

    # PATH до нашего Node обязателен: npx-cli.js порождает дочерние процессы
    # командой `node` и ищет её в PATH. Без этого агент на машине без Node
    # умирает до первого байта — регрессия, пойманная только живым запуском.
    assert spec.env["PATH"].split(os.pathsep)[0] == str(fake_node.parent)
    assert runtime.is_installed(entry)
    assert runtime.installed_version(entry.id) == "1.0.0"


def test_install_agent_npx_is_idempotent(monkeypatch):
    # "Идемпотентно" здесь — про повторную СЕТЕВУЮ установку пакета/Node, а не
    # про то, что launch_spec вообще не тронет node.ensure_node(): она дешёвая
    # (find_system_node — просто shutil.which) и вызывается при каждой сборке
    # LaunchSpec, в том числе для уже установленного агента.
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
    # повторная установка не переписывает манифест заново.
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
    assert fetcher.calls == []  # даже не пытались качать — нечем проверить


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
    assert len(fetcher.calls) == 1  # та же версия — второй раз не качали


# --- uninstall_agent / launch_spec -------------------------------------------


def test_installed_version_does_not_create_directory_for_unknown_agent():
    # is_installed()/installed_version() дергаются для каждого агента реестра
    # при отрисовке экрана "Агенты" — простая проверка статуса не должна
    # заводить пустые папки на диске для агентов, которых никто не ставил.
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

    # Через paths.agent_dir() тут не проверяем: она создаёт директорию как
    # побочный эффект (paths._sub делает mkdir при каждом обращении), и
    # проверка "не существует" через неё сама бы её пересоздала.
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

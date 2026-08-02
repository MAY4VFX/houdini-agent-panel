"""Тесты портативного Node.js: системный/свой, скачивание, npx-cli.js."""

from __future__ import annotations

import hashlib
import io
import subprocess
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
    """Минимальная, но настоящая раскладка архива nodejs.org: bin/node +
    lib/node_modules/npm/bin/npx-cli.js — ровно то, что нужно `install_node`
    и `npx_argv`."""
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
    assert fetcher.calls == calls_after_first  # второй раз в сеть не ходили
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
    # временных файлов рядом тоже не осталось
    assert list(paths.node_dir().iterdir()) == []


def test_install_node_missing_shasums_entry_raises_without_downloading_archive(
    tmp_path, fetcher, monkeypatch
):
    monkeypatch.setattr(node.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(node.platform, "machine", lambda: "arm64")

    version = "22.14.0"
    fetcher.add_bytes(node.shasums_url(version), b"deadbeef  some-other-file.tar.gz\n")
    # archive_url намеренно не зарегистрирован — если бы install_node попытался
    # его скачать, FakeFetcher бросил бы NetworkError, а не наш ChecksumError.

    with pytest.raises(ChecksumError):
        node.install_node(version=version, fetch=fetcher)


# --- ensure_node --------------------------------------------------------------


def test_ensure_node_prefers_system_node(monkeypatch):
    system_path = Path("/usr/local/bin/node")
    monkeypatch.setattr(node, "find_system_node", lambda **k: system_path)

    def explode(*a, **k):
        raise AssertionError("install_node не должен звонить, когда есть системный node")

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

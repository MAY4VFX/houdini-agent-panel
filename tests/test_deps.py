"""Тесты на поиск hython и установку зависимостей панели в Houdini-Python.

Настоящую Houdini не запускаем: `find_hython`/`python_version_of` работают на
моках файловой системы (через monkeypatch корневых констант модуля) и
`subprocess.run`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from houdini_agent_panel import deps


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return path


# --- find_hython -------------------------------------------------------


def test_find_hython_prefers_hfs_env(tmp_path, monkeypatch):
    hfs = tmp_path / "custom_hfs"
    hython = _touch(hfs / "bin" / "hython")
    monkeypatch.setenv("HFS", str(hfs))

    assert deps.find_hython("20.5") == hython


def test_find_hython_hfs_windows_style(tmp_path, monkeypatch):
    hfs = tmp_path / "custom_hfs"
    hython = _touch(hfs / "bin" / "hython.exe")
    monkeypatch.setenv("HFS", str(hfs))
    monkeypatch.setattr(deps, "_system", lambda: "windows")

    assert deps.find_hython("20.5") == hython


def test_find_hython_ignores_hfs_pointing_nowhere(tmp_path, monkeypatch):
    monkeypatch.setenv("HFS", str(tmp_path / "does_not_exist"))
    monkeypatch.setattr(deps, "_system", lambda: "darwin")
    monkeypatch.setattr(deps, "_MAC_APPLICATIONS_ROOT", tmp_path / "Applications" / "Houdini")

    assert deps.find_hython("20.5") is None


def test_find_hython_darwin_picks_newest_build(tmp_path, monkeypatch):
    monkeypatch.delenv("HFS", raising=False)
    monkeypatch.setattr(deps, "_system", lambda: "darwin")
    root = tmp_path / "Applications" / "Houdini"
    monkeypatch.setattr(deps, "_MAC_APPLICATIONS_ROOT", root)

    older = _touch(
        root / "Houdini20.5.445" / "Frameworks" / "Houdini.framework" / "Versions" / "20.5"
        / "Resources" / "bin" / "hython"
    )
    newer = _touch(
        root / "Houdini20.5.589" / "Frameworks" / "Houdini.framework" / "Versions" / "20.5"
        / "Resources" / "bin" / "hython"
    )

    assert deps.find_hython("20.5") == newer
    assert deps.find_hython("20.5") != older


def test_find_hython_linux(tmp_path, monkeypatch):
    monkeypatch.delenv("HFS", raising=False)
    monkeypatch.setattr(deps, "_system", lambda: "linux")
    root = tmp_path / "opt"
    monkeypatch.setattr(deps, "_LINUX_OPT_ROOT", root)

    hython = _touch(root / "hfs20.5.445" / "bin" / "hython")

    assert deps.find_hython("20.5") == hython


def test_find_hython_windows(tmp_path, monkeypatch):
    monkeypatch.delenv("HFS", raising=False)
    monkeypatch.setattr(deps, "_system", lambda: "windows")
    root = tmp_path / "Program Files" / "Side Effects Software"
    monkeypatch.setattr(deps, "_WINDOWS_PROGRAM_FILES", root)

    hython = _touch(root / "Houdini 20.5.445" / "bin" / "hython.exe")

    assert deps.find_hython("20.5") == hython


def test_find_hython_none_when_root_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("HFS", raising=False)
    monkeypatch.setattr(deps, "_system", lambda: "darwin")
    monkeypatch.setattr(deps, "_MAC_APPLICATIONS_ROOT", tmp_path / "nope")

    assert deps.find_hython("20.5") is None


# --- python_version_of ---------------------------------------------------


def test_python_version_of_parses_stdout(monkeypatch):
    def fake_run(argv, **kwargs):
        assert argv[0] == "/fake/hython"
        return subprocess.CompletedProcess(
            argv, 0, stdout="3.11\n", stderr="UserWarning: setuptools blah\n"
        )

    monkeypatch.setattr(deps.subprocess, "run", fake_run)

    assert deps.python_version_of(Path("/fake/hython")) == (3, 11)


def test_python_version_of_ignores_setuptools_warning_on_stderr(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="3.13\n", stderr="warning noise\n" * 5)

    monkeypatch.setattr(deps.subprocess, "run", fake_run)

    assert deps.python_version_of(Path("/fake/hython")) == (3, 13)


def test_python_version_of_returns_none_on_garbage(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, stdout="not-a-version\n", stderr="")

    monkeypatch.setattr(deps.subprocess, "run", fake_run)

    assert deps.python_version_of(Path("/fake/hython")) is None


def test_python_version_of_raises_deps_error_on_nonzero_exit(monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")

    monkeypatch.setattr(deps.subprocess, "run", fake_run)

    with pytest.raises(deps.DepsError):
        deps.python_version_of(Path("/fake/hython"))


def test_python_version_of_raises_deps_error_on_timeout(monkeypatch):
    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=30)

    monkeypatch.setattr(deps.subprocess, "run", fake_run)

    with pytest.raises(deps.DepsError):
        deps.python_version_of(Path("/fake/hython"))


# --- install_deps ---------------------------------------------------------


def test_install_deps_dry_run_does_not_touch_disk_or_subprocess(tmp_path, monkeypatch):
    def explode(*args, **kwargs):
        raise AssertionError("dry-run не должен звать subprocess")

    monkeypatch.setattr(deps.subprocess, "run", explode)
    target = tmp_path / "deps"
    logged = []

    lines = deps.install_deps(
        Path("/fake/hython"),
        target=target,
        requirement="houdini-agent-panel==0.1.0",
        dry_run=True,
        out=logged.append,
    )

    assert lines == []
    assert not target.exists()
    assert any("houdini-agent-panel==0.1.0" in line for line in logged)


def test_install_deps_success_creates_target_and_returns_log(tmp_path, monkeypatch):
    def fake_run(argv, **kwargs):
        assert "--target" in argv
        assert str(target) in argv
        assert "--upgrade" in argv
        return subprocess.CompletedProcess(argv, 0, stdout="Collecting foo\nInstalling...\n", stderr="")

    target = tmp_path / "deps" / "py3.11"
    monkeypatch.setattr(deps.subprocess, "run", fake_run)
    logged = []

    lines = deps.install_deps(
        Path("/fake/hython"),
        target=target,
        requirement="houdini-agent-panel==0.1.0",
        out=logged.append,
    )

    assert target.is_dir()
    assert lines == ["Collecting foo", "Installing..."]


def _capture_install_argv(monkeypatch, tmp_path, **kwargs) -> list[str]:
    captured = {}

    def fake_run(argv, **_kwargs):
        captured["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(deps.subprocess, "run", fake_run)
    deps.install_deps(
        Path("/fake/hython"),
        target=tmp_path / "deps",
        requirement="houdini-agent-panel",
        out=lambda *_: None,
        **kwargs,
    )
    return captured["argv"]


def test_install_deps_find_links_alone_does_not_cut_off_pypi(monkeypatch, tmp_path):
    """«Возьми колесо панели отсюда» и «не ходи в интернет» — разные намерения.

    Когда это был один флаг, главный сценарий разработки не работал: ставишь
    локально собранное колесо панели, а `acp` и `pydantic` взять неоткуда,
    потому что `--no-index` закрыл заодно и их.
    """
    argv = _capture_install_argv(monkeypatch, tmp_path, find_links="/local/wheels")

    assert "--find-links" in argv
    assert "/local/wheels" in argv
    assert "--no-index" not in argv


def test_install_deps_offline_cuts_off_pypi(monkeypatch, tmp_path):
    argv = _capture_install_argv(
        monkeypatch, tmp_path, find_links="/local/wheels", offline=True
    )

    assert "--no-index" in argv
    assert "--find-links" in argv
    assert "--find-links" in argv
    assert "/local/wheels" in argv


def test_install_deps_raises_on_pip_failure(tmp_path, monkeypatch):
    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="pip blew up")

    monkeypatch.setattr(deps.subprocess, "run", fake_run)

    with pytest.raises(deps.DepsError):
        deps.install_deps(
            Path("/fake/hython"),
            target=tmp_path / "deps",
            requirement="houdini-agent-panel",
            out=lambda *_: None,
        )


def test_install_deps_raises_deps_error_when_subprocess_cannot_start(tmp_path, monkeypatch):
    def fake_run(argv, **kwargs):
        raise OSError("no such file")

    monkeypatch.setattr(deps.subprocess, "run", fake_run)

    with pytest.raises(deps.DepsError):
        deps.install_deps(
            Path("/fake/hython"),
            target=tmp_path / "deps",
            requirement="houdini-agent-panel",
            out=lambda *_: None,
        )


# --- deps_ready ------------------------------------------------------------


def test_deps_ready_false_when_empty(tmp_path):
    assert deps.deps_ready(tmp_path / "deps") is False


def test_deps_ready_false_when_partial(tmp_path):
    target = tmp_path / "deps"
    (target / "acp").mkdir(parents=True)
    assert deps.deps_ready(target) is False


def test_deps_ready_true_when_both_present(tmp_path):
    target = tmp_path / "deps"
    (target / "acp").mkdir(parents=True)
    (target / "houdini_agent_panel").mkdir(parents=True)
    assert deps.deps_ready(target) is True

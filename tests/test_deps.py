"""Tests for finding hython and installing the panel's dependencies into Houdini's Python.

We never launch a real Houdini: `find_hython`/`python_version_of` work
against filesystem mocks (via monkeypatching the module's root constants)
and `subprocess.run`.
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


def test_find_hython_uses_hfs_env(tmp_path, monkeypatch):
    """A version-bearing `$HFS` answers directly, without touching the disk
    search — `_LINUX_OPT_ROOT` is pointed at nothing to prove it."""
    hfs = tmp_path / "hfs20.5.445"
    hython = _touch(hfs / "bin" / "hython")
    monkeypatch.setenv("HFS", str(hfs))
    monkeypatch.setattr(deps, "_system", lambda: "linux")
    monkeypatch.setattr(deps, "_LINUX_OPT_ROOT", tmp_path / "nothing")

    assert deps.find_hython("20.5") == hython


def test_find_hython_hfs_windows_style(tmp_path, monkeypatch):
    hfs = tmp_path / "Houdini 20.5.445"
    hython = _touch(hfs / "bin" / "hython.exe")
    monkeypatch.setenv("HFS", str(hfs))
    monkeypatch.setattr(deps, "_system", lambda: "windows")
    monkeypatch.setattr(deps, "_WINDOWS_PROGRAM_FILES", tmp_path / "nothing")

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
        raise AssertionError("dry-run must not invoke subprocess")

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
    """"Take the panel's wheel from here" and "don't touch the internet" are different intents.

    When this was a single flag, the main development scenario didn't work:
    you install a locally built panel wheel, but there's nowhere to get
    `acp` and `pydantic` from, because `--no-index` shut those off too.
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


def test_the_version_probe_allows_for_a_cold_hython():
    """`hython -c "print(...)"` loads all of Houdini's Python first. Measured
    warm and idle: 18.9s on 20.5, 20.2s on 22.0 — against a 30s ceiling that
    a loaded farm node or a network-mounted install would blow straight
    through, refusing to install and blaming Houdini for starting slowly. A
    dry run on the developer's own machine did exactly that for 22.0."""
    from houdini_agent_panel import deps

    assert deps._VERSION_TIMEOUT >= 120.0, (
        f"{deps._VERSION_TIMEOUT}s leaves no room over a ~20s cold start"
    )


# --- stale metadata in a --target tree --------------------------------------


def test_prune_removes_dist_info_for_versions_pip_replaced(tmp_path):
    """`pip install --target` overwrites the package and leaves the previous
    `dist-info` in place. Six panel releases left six of them, and
    `importlib.metadata` answered with 0.1.6 while the imported code was
    0.2.0 — which made the installer reinstall 0.1.6 over 0.2.0."""
    from houdini_agent_panel.deps import prune_stale_metadata

    for name in (
        "houdini_agent_panel-0.1.6.dist-info",
        "houdini_agent_panel-0.2.0.dist-info",
        "houdini_agent_panel-0.2.1.dist-info",
        "pydantic-2.13.4.dist-info",
    ):
        (tmp_path / name).mkdir()

    removed = prune_stale_metadata(
        tmp_path, ["Successfully installed houdini-agent-panel-0.2.1 pydantic-2.13.4"]
    )

    assert sorted(removed) == [
        "houdini_agent_panel-0.1.6.dist-info",
        "houdini_agent_panel-0.2.0.dist-info",
    ]
    assert (tmp_path / "houdini_agent_panel-0.2.1.dist-info").is_dir()
    assert (tmp_path / "pydantic-2.13.4.dist-info").is_dir()


def test_prune_leaves_alone_what_pip_did_not_install(tmp_path):
    """Metadata for a package this run never touched is somebody else's — an
    old-looking version is not evidence that the files beside it are dead."""
    from houdini_agent_panel.deps import prune_stale_metadata

    (tmp_path / "unrelated-0.0.1.dist-info").mkdir()

    removed = prune_stale_metadata(tmp_path, ["Successfully installed pydantic-2.13.4"])

    assert removed == []
    assert (tmp_path / "unrelated-0.0.1.dist-info").is_dir()


def test_prune_matches_names_across_underscore_and_dash_spelling(tmp_path):
    """pip reports `houdini-agent-panel`, writes `houdini_agent_panel-*.dist-info`."""
    from houdini_agent_panel.deps import prune_stale_metadata

    (tmp_path / "houdini_agent_panel-0.1.6.dist-info").mkdir()

    removed = prune_stale_metadata(
        tmp_path, ["Successfully installed houdini-agent-panel-0.2.1"]
    )

    assert removed == ["houdini_agent_panel-0.1.6.dist-info"]


def test_panel_version_ignores_stale_metadata_and_reports_the_running_code():
    """The number that goes into `houdini-agent-panel==<version>`."""
    from houdini_agent_panel import __version__
    from houdini_agent_panel.install import _panel_version
    from houdini_agent_panel.updates import _current_panel_version

    assert _panel_version() == __version__
    assert _current_panel_version() == __version__


# --- $HFS must not answer for a Houdini it isn't -----------------------------


def test_hfs_is_not_used_for_a_different_houdini_version(tmp_path, monkeypatch):
    """Run from Houdini 22's hython, the 20.5 pass used to pick up 22's
    hython — because `$HFS` won unconditionally — and installed Python 3.13
    wheels into the 20.5 package, `pydantic_core` among them. 20.5 runs
    Python 3.11 and cannot load that binary at all."""
    from houdini_agent_panel import deps as deps_mod

    hfs22 = tmp_path / "hfs22.0.368"
    (hfs22 / "bin").mkdir(parents=True)
    (hfs22 / "bin" / "hython").touch()
    root = tmp_path / "opt"
    hfs205 = root / "hfs20.5.445"
    (hfs205 / "bin").mkdir(parents=True)
    (hfs205 / "bin" / "hython").touch()

    monkeypatch.setenv("HFS", str(hfs22))
    monkeypatch.setattr(deps_mod, "_system", lambda: "linux")
    monkeypatch.setattr(deps_mod, "_LINUX_OPT_ROOT", root)

    assert deps_mod.find_hython("20.5") == hfs205 / "bin" / "hython"
    assert deps_mod.find_hython("22.0") == hfs22 / "bin" / "hython"


def test_hfs_still_wins_when_nothing_is_found_on_disk(tmp_path, monkeypatch):
    """A studio install in a non-standard location is what the variable is for."""
    from houdini_agent_panel import deps as deps_mod

    hfs = tmp_path / "studio" / "houdini-build"
    (hfs / "bin").mkdir(parents=True)
    (hfs / "bin" / "hython").touch()
    monkeypatch.setenv("HFS", str(hfs))
    monkeypatch.setattr(deps_mod, "_system", lambda: "linux")
    monkeypatch.setattr(deps_mod, "_LINUX_OPT_ROOT", tmp_path / "empty")

    assert deps_mod.find_hython("20.5") == hfs / "bin" / "hython"


def test_version_match_is_not_fooled_by_a_longer_number(tmp_path, monkeypatch):
    from houdini_agent_panel.deps import _mentions_version
    from pathlib import Path

    assert _mentions_version(Path("/opt/hfs20.5.445"), "20.5") is True
    assert _mentions_version(Path("/x/Houdini22.0.368/Versions/22.0"), "22.0") is True
    assert _mentions_version(Path("/opt/hfs20.55"), "20.5") is False
    assert _mentions_version(Path("/opt/hfs120.5"), "20.5") is False

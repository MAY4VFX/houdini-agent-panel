"""Cross-platform child-process spawning.

The Windows branches cannot be executed here (there is no Windows machine in
this project — the same limitation `ui/self_update.py` documents), so what
these tests pin down is the DECISION each branch makes, through
`childproc._is_windows`. That is the whole reason that seam exists rather
than `sys.platform` being read at each call site.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

from houdini_agent_panel import childproc


@pytest.fixture
def as_windows(monkeypatch):
    monkeypatch.setattr(childproc, "_is_windows", lambda: True)


def test_no_window_kwargs_are_empty_on_posix():
    """A `creationflags` keyword does not exist off Windows — passing one
    would be a TypeError at every call site, so the dict must be empty."""
    assert childproc.hidden_window_kwargs() == {}


def test_no_window_kwargs_carry_the_flag_on_windows(as_windows):
    assert childproc.hidden_window_kwargs() == {"creationflags": 0x08000000}


def test_run_passes_arguments_through(tmp_path):
    result = childproc.run(
        [sys.executable, "-c", "print('hi')"], capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "hi"


def test_run_adds_the_flag_on_windows(as_windows, monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(childproc.subprocess, "run", fake_run)

    childproc.run(["tasklist"], capture_output=True)

    assert seen["creationflags"] == 0x08000000
    assert seen["capture_output"] is True


def test_spawn_with_asyncio_pipes_gives_pipes_on_posix():
    process = childproc.spawn_with_asyncio_pipes(
        [sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
        env=None,
        cwd=None,
    )
    try:
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
    finally:
        process.stdin.close()
        process.wait(timeout=60)


def test_spawn_on_windows_uses_the_overlapped_popen(as_windows, monkeypatch):
    """The whole point of the Windows branch: a proactor loop cannot read
    an anonymous `subprocess.PIPE`, so the spawn must go through
    `asyncio.windows_utils.Popen` (overlapped handles) instead. Verified by
    substituting that module, since importing the real one off Windows is
    impossible."""
    calls = {}

    class FakeWindowsUtils:
        @staticmethod
        def Popen(argv, **kwargs):  # noqa: N802 - mirrors the stdlib's own name
            calls["argv"] = argv
            calls["kwargs"] = kwargs
            return "fake-process"

    monkeypatch.setitem(sys.modules, "asyncio.windows_utils", FakeWindowsUtils)

    result = childproc.spawn_with_asyncio_pipes(["agent.exe"], env={"A": "1"}, cwd="/tmp")

    assert result == "fake-process"
    assert calls["argv"] == ["agent.exe"]
    # bufsize=0 is required by windows_utils.Popen (it asserts on it), and
    # the console window must be suppressed for a GUI-hosted agent.
    assert calls["kwargs"]["bufsize"] == 0
    assert calls["kwargs"]["creationflags"] == 0x08000000
    assert calls["kwargs"]["env"] == {"A": "1"}
    assert calls["kwargs"]["cwd"] == "/tmp"


def test_terminate_tree_uses_terminate_on_posix():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])

    childproc.terminate_tree(process)

    assert process.wait(timeout=60) != 0


def test_terminate_tree_kills_the_whole_tree_on_windows(as_windows, monkeypatch):
    """`Popen.terminate()` on Windows is `TerminateProcess`: it stops the
    one process and not the grandchild an npx agent actually is."""
    ran = {}

    def fake_run(argv, **kwargs):
        ran["argv"] = argv
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(childproc, "run", fake_run)

    class FakeProcess:
        pid = 4242

        def terminate(self):
            ran["terminate"] = True

    childproc.terminate_tree(FakeProcess())

    assert ran["argv"] == ["taskkill", "/PID", "4242", "/T", "/F"]
    assert "terminate" not in ran


def test_terminate_tree_falls_back_when_taskkill_fails(as_windows, monkeypatch):
    """An incomplete cleanup beats no cleanup: if `taskkill` isn't there or
    refuses, the process we do hold a handle for still goes down."""
    ran = {}

    def fake_run(argv, **kwargs):
        raise OSError("taskkill not found")

    monkeypatch.setattr(childproc, "run", fake_run)

    class FakeProcess:
        pid = 7
        terminated = False

        def terminate(self):
            ran["terminate"] = True

    childproc.terminate_tree(FakeProcess())

    assert ran["terminate"] is True

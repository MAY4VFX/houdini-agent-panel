"""A `QThread` still running when the widget that parented it is destroyed
is not a warning — Qt calls `qFatal()`, which raises `SIGABRT` and takes
the whole process down (docs/facts/houdini.md §14). Reproduced directly:
running `hython` from both a real Houdini 20.5 (PySide2/Qt5) and 22.0
(PySide6/Qt6) install, and this project's own plain PySide6 venv, on a
worker blocked on an event that is never set, with nothing more exotic
than `del` on the widget that owned it.

The crash genuinely kills the process it happens in, so both proving it
and proving the fix run in a SUBPROCESS — a test that can abort the whole
pytest run if something regresses is a hazard, not a test.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap

import pytest

_PACKAGE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "python"
)

#: Common setup for both scripts below: a `Worker` blocked on an event
#: that this script never sets — the exact shape of a network round trip
#: that outlives the panel's own bounded `wait()` (measured on the owner's
#: own machine: ~21KB of a 48KB fetch in 60 seconds direct, vs. half a
#: second through the studio's proxy).
_SETUP = f"""
import os, sys, tempfile, threading, time
os.environ.setdefault("HAP_DATA_DIR", tempfile.mkdtemp(prefix="hap-test-"))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, {_PACKAGE_PATH!r})

from houdini_agent_panel.ui.qt import QtWidgets
from houdini_agent_panel.ui.worker import Worker

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
gate = threading.Event()

class _Blocking(Worker):
    def work(self):
        gate.wait()

widget = QtWidgets.QWidget()
worker = _Blocking(widget)
worker.start()
for _ in range(20):
    app.processEvents()
    time.sleep(0.01)
assert worker.isRunning(), "the worker must still be running for this to test anything"
"""


def _run(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_a_still_running_worker_aborts_the_process_the_naive_way(qapp):
    """Establishes the crash is real, not a guess: the exact pattern every
    one of this project's workers used before `release()` existed —
    `requestInterruption()`, a bounded `wait()`, then drop the reference
    and let the widget go — aborts the process the instant the widget
    that owned the still-running thread as a Qt child is destroyed.
    """
    script = _SETUP + textwrap.dedent(
        """
        worker.requestInterruption()
        worker.wait(50)  # the thread is blocked on `gate` — this times out
        assert worker.isRunning()
        del worker
        del widget  # <-- this is what aborts: QObjectPrivate::deleteChildren()
        print("SMOKE OK")
        """
    )
    result = _run(script)

    # SIGABRT: Python's own `subprocess` reports a signal-terminated
    # child as `-signum`, not the shell's `128+signum` convention.
    assert result.returncode == -6, (
        f"expected SIGABRT (-6), got {result.returncode}\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "SMOKE OK" not in result.stdout


def test_release_prevents_the_abort(qapp):
    """The fix: `release()` instead of the naive requestInterruption+wait.
    This test fails exactly the way it should if `release` is removed,
    renamed, or "fixed" by lengthening the timeout instead — the gate is
    never set until AFTER teardown, so a longer timeout only delays the
    same abort, it does not prevent it.
    """
    script = _SETUP + textwrap.dedent(
        """
        from houdini_agent_panel.ui.worker import release

        release(worker, timeout_ms=50)  # still blocked on `gate` — times out too
        del worker
        del widget  # must NOT abort: `release` already reparented it out
        print("SMOKE OK")

        # Let it actually finish so the process can also exit cleanly,
        # proving `release` did not just leak the thread forever either.
        gate.set()
        for _ in range(200):
            app.processEvents()
            time.sleep(0.01)
        print("CLEAN EXIT")
        """
    )
    result = _run(script)

    assert result.returncode == 0, (
        f"stdout: {result.stdout}\nstderr: {result.stderr}"
    )
    assert "SMOKE OK" in result.stdout
    assert "CLEAN EXIT" in result.stdout

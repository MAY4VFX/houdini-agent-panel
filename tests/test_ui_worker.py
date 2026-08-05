"""A background thread cannot fail silently.

The incident this class exists to prevent: an install worker caught only its
own `InstallError` while `node.npx_argv` raises a plain `RuntimeError`. That
escaped `run()` entirely — PySide printed it to stderr, which nobody reads
inside Houdini — so neither success nor failure was ever emitted, and the
flag saying "an install is in progress" was never cleared. Every later click
on Install and Update for that agent was then declined without a word, for
the rest of the session.
"""

from __future__ import annotations

import logging
import threading

from houdini_agent_panel.ui import worker as worker_module
from houdini_agent_panel.ui.qt import QtCore, QtWidgets
from houdini_agent_panel.ui.worker import Worker, WorkerStopped


def _run_and_wait(qapp, worker: Worker, timeout_ms: int = 5000) -> None:
    worker.start()
    timer = QtCore.QElapsedTimer()
    timer.start()
    while worker.isRunning() and timer.elapsed() < timeout_ms:
        qapp.processEvents()
        QtCore.QThread.msleep(5)
    worker.wait(1000)
    qapp.processEvents()


def test_an_unexpected_exception_becomes_a_signal(qapp):
    class _Boom(Worker):
        def work(self) -> None:
            raise RuntimeError("no npm found next to node")

    seen: list[str] = []
    worker = _Boom()
    worker.failed.connect(seen.append)
    _run_and_wait(qapp, worker)

    assert seen == ["no npm found next to node"], (
        "the failure never reached anyone — this is the bug the class exists for"
    )


def test_an_exception_with_no_message_still_reports_something(qapp):
    """`str(exc)` is empty for plenty of exceptions; an empty error row is
    barely better than no error row."""

    class _Silent(Worker):
        def work(self) -> None:
            raise TimeoutError()

    seen: list[str] = []
    worker = _Silent()
    worker.failed.connect(seen.append)
    _run_and_wait(qapp, worker)

    assert seen == ["TimeoutError"]


def test_the_failure_is_logged_with_its_traceback(qapp, caplog):
    class _Boom(Worker):
        def work(self) -> None:
            raise RuntimeError("gone")

    with caplog.at_level(logging.ERROR, logger="houdini_agent_panel"):
        _run_and_wait(qapp, _Boom())

    assert any(r.exc_info for r in caplog.records), (
        "without the traceback the log cannot answer why"
    )


def test_a_deliberate_stop_is_not_an_error(qapp):
    """Turning a shutdown the panel asked for into an error on screen trades
    an invisible failure for a false alarm."""

    class _Stops(Worker):
        def work(self) -> None:
            raise WorkerStopped()

    seen: list[str] = []
    worker = _Stops()
    worker.failed.connect(seen.append)
    _run_and_wait(qapp, worker)

    assert seen == []


def test_an_interrupted_worker_stays_quiet(qapp):
    """A thread already on its way out may well trip over something. That is
    not news worth showing anybody."""

    class _Interrupted(Worker):
        def work(self) -> None:
            while not self.isInterruptionRequested():
                QtCore.QThread.msleep(5)
            raise RuntimeError("tripped on the way out")

    seen: list[str] = []
    worker = _Interrupted()
    worker.failed.connect(seen.append)
    worker.start()
    QtCore.QThread.msleep(30)
    worker.requestInterruption()
    _run_and_wait(qapp, worker)

    assert seen == []


# --- `release`/`release_all`: a QThread still running when its parent
# widget is destroyed is `qFatal()`/`SIGABRT`, not a warning
# (docs/facts/houdini.md §14; the crash itself is reproduced end to end,
# subprocess and all, in test_worker_teardown_safety.py). These tests
# cover the unit-level behaviour of the fix: what `release()` actually
# does to a worker in each of the two cases it has to handle.


class _Blocking(Worker):
    """Blocks on an event the test controls — never finishes on its own,
    the same shape as a network round trip that outlives the panel's own
    bounded wait."""

    def __init__(self, gate: threading.Event, parent=None) -> None:
        super().__init__(parent)
        self._gate = gate

    def work(self) -> None:
        self._gate.wait()


def test_release_is_a_no_op_once_the_worker_already_finished(qapp):
    """The common case: the wait succeeds, nothing needs reparenting."""
    gate = threading.Event()
    gate.set()  # `work()` returns almost immediately
    worker = _Blocking(gate)
    worker.start()

    worker_module.release(worker, timeout_ms=2000)

    assert worker not in worker_module._orphaned
    assert worker.parent() is None  # never had one to begin with


def test_release_reparents_and_keeps_alive_a_worker_still_running(qapp):
    """The case that used to crash: the wait times out because the worker
    is genuinely still running. `release()` has to detach it from its
    widget parent and keep a real reference until it actually finishes —
    not just drop it and hope."""
    gate = threading.Event()
    widget = QtWidgets.QWidget()
    worker = _Blocking(gate, parent=widget)
    worker.start()
    _wait_until(qapp, lambda: worker.isRunning())

    worker_module.release(worker, timeout_ms=50)

    assert worker.isRunning(), "the gate is still closed — it must not have finished"
    assert worker.parent() is None, "still a Qt child of `widget` would crash when it's destroyed"
    assert worker in worker_module._orphaned

    # Letting the widget go must not disturb the worker at all now.
    del widget

    gate.set()
    _wait_until(qapp, lambda: worker not in worker_module._orphaned)
    assert not worker.isRunning()


def test_worker_start_tracks_it_and_finished_untracks_it(qapp):
    """`release_all` (the `aboutToQuit`/`atexit` safety net) needs to know
    what's running at all regardless of which widget thinks it owns one —
    this is where that list comes from."""
    gate = threading.Event()
    gate.set()
    worker = _Blocking(gate)

    assert worker not in worker_module._live
    worker.start()
    assert worker in worker_module._live

    _wait_until(qapp, lambda: worker not in worker_module._live)


def test_release_all_releases_every_still_running_worker(qapp):
    gate = threading.Event()
    widget = QtWidgets.QWidget()
    a = _Blocking(gate, parent=widget)
    b = _Blocking(gate, parent=widget)
    a.start()
    b.start()
    _wait_until(qapp, lambda: a.isRunning() and b.isRunning())

    worker_module.release_all(timeout_ms=50)

    assert a in worker_module._orphaned
    assert b in worker_module._orphaned
    assert a.parent() is None
    assert b.parent() is None

    gate.set()
    _wait_until(qapp, lambda: a not in worker_module._orphaned and b not in worker_module._orphaned)


def _wait_until(qapp, condition, *, timeout_ms: int = 5000) -> None:
    timer = QtCore.QElapsedTimer()
    timer.start()
    while not condition() and timer.elapsed() < timeout_ms:
        qapp.processEvents()
        QtCore.QThread.msleep(5)
    assert condition(), "condition did not become true in time"

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

from houdini_agent_panel.ui.qt import QtCore
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

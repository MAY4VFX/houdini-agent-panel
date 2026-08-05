"""One place that decides what happens when a background thread fails.

Every slow thing the panel does — a registry round trip, preparing a launch,
installing an agent, uploading audio — happens on a `QThread`, because
Houdini paints its viewport on the same thread that would otherwise be
waiting on the network.

The trap those threads share is not obvious and cost a real artist a real
afternoon. An exception escaping `QThread.run()` is printed by PySide
straight to stderr. Not through `logging`, so not into the panel's own log
file; and inside a GUI Houdini there is no stderr anyone reads. So the
thread dies, nothing is emitted, and whatever was waiting on that signal
waits forever — in the reported case a flag saying "an install is already
running" that nothing ever cleared, which then declined every later click on
Install and Update, in silence, for the rest of the session.

`_InstallWorker` had a narrow `except runtime.InstallError` while
`node.npx_argv` raises `NpxNotFoundError`, a plain `RuntimeError`. Three
sibling workers already caught broadly. That difference was an oversight
rather than a decision, which is exactly the kind of thing a base class
should stop being possible: subclasses implement `work()`, and failing is
handled here, once, the same way every time.

`logbook`'s `sys.excepthook` addition is the net under this one, for a
future worker that does not use this class and for exceptions in ordinary Qt
slots. This is the structural guarantee; that one is the safety net. Neither
replaces the other.

A second trap, just as real, lives at the OTHER end of a worker's life, not
the start: `release()` below exists because of it, see its own docstring.
"""

from __future__ import annotations

import atexit

from ..logbook import logger as _logbook_logger
from .qt import QtCore, Signal

_log = _logbook_logger("houdini_agent_panel.ui.worker")


class Worker(QtCore.QThread):
    """A background thread that cannot fail silently.

    Subclasses implement `work()` and emit whatever result signals they
    define. Anything that escapes `work()` becomes a `failed` signal and a
    log entry with the traceback.

    An interruption is not a failure. A thread asked to stop — via
    `requestInterruption`, or by raising `WorkerStopped` — emits nothing:
    turning a shutdown the panel itself requested into an error message on
    screen would trade an invisible failure for a false alarm, which is not
    an improvement.
    """

    #: Human-readable reason, for the caller to put wherever it belongs (an
    #: agent row, the feed). Emitted only for genuine failures.
    failed = Signal(str)

    def work(self) -> None:  # pragma: no cover - overridden by every subclass
        raise NotImplementedError

    def run(self) -> None:  # noqa: D102 - overrides QThread.run
        try:
            self.work()
        except WorkerStopped:
            return
        except Exception as exc:  # noqa: BLE001 - the whole point of this class
            if self.isInterruptionRequested():
                # It was already on its way out; whatever it tripped over on
                # the way is not news.
                return
            _log.exception("%s failed", type(self).__name__)
            try:
                self.failed.emit(str(exc) or type(exc).__name__)
            except RuntimeError:
                # The receiver was deleted while this thread was running —
                # the panel closed. Nothing left to tell, and the log above
                # already has it.
                pass

    def start(self, *args, **kwargs) -> None:  # noqa: D102 - overrides QThread.start
        # Tracked from the moment it actually starts, not when it's
        # constructed — `release_all`'s whole job is "which of these are
        # still running right now", and a `Worker` built but never started
        # has nothing to be released from.
        _live.add(self)
        self.finished.connect(lambda: _live.discard(self))
        _ensure_global_hooks()
        super().start(*args, **kwargs)


class WorkerStopped(Exception):
    """Raise from `work()` to end quietly, with nothing reported.

    For a thread that notices `isInterruptionRequested()` half-way through
    and has nothing useful to hand back.
    """


#: Every `Worker` currently running, tracked from `Worker.start()` —
#: `release_all`'s own source of "what's out there right now regardless of
#: which widget thinks it owns one."
_live: set[Worker] = set()

#: Workers `release()` has detached from a dying widget and is keeping
#: alive in Python until they actually finish — see `release`'s own
#: docstring. A worker here is never deleted by anything else in the
#: meantime: no widget parent, and this set is a real reference, so even an
#: aggressive GC pass can't collect the wrapper out from under the still-
#: running OS thread.
_orphaned: set[Worker] = set()

_global_hooks_installed = False


def _ensure_global_hooks() -> None:
    """Install the `aboutToQuit`/`atexit` safety net exactly once.

    Deferred to the first `Worker.start()` rather than run at import time:
    a `QCoreApplication` is not guaranteed to exist yet when this module is
    first imported (`QtCore.QCoreApplication.instance()` would just be
    `None`), but one is required to even construct a `QThread`, so by the
    time anything actually calls `start()`, it does.
    """
    global _global_hooks_installed
    if _global_hooks_installed:
        return
    _global_hooks_installed = True
    app = QtCore.QCoreApplication.instance()
    if app is not None:
        app.aboutToQuit.connect(release_all)
    atexit.register(release_all)


def release(worker: "Worker | None", *, timeout_ms: int = 2000) -> None:
    """The one safe way to let go of a worker whose owning widget might be
    torn down before the worker itself finishes.

    A `QThread` still `isRunning()` when the widget that parented it is
    destroyed is not a warning Qt prints and moves on from — it calls
    `qFatal()`, which raises `SIGABRT` and takes the whole process down
    (docs/facts/houdini.md §14). Reproduced directly, on both Houdini
    20.5's PySide2/Qt5 and 22.0's PySide6/Qt6, with nothing more exotic
    than a worker blocked on an event that is never set and a plain
    `del` on the widget that owned it — the exact shape of a network
    round trip that outlives the panel's own bounded `wait()` (measured on
    the owner's own machine: ~21KB of a 48KB fetch in 60 seconds direct,
    vs. half a second through the studio's proxy — a wait long enough to
    be a good citizen on a fast connection is not remotely long enough on
    that one).

    `requestInterruption()` plus a bounded `wait()` is a courtesy, never a
    guarantee — a thread blocked in a socket read does not notice
    `isInterruptionRequested()` until the socket itself gives up. This is
    what happens when the courtesy isn't enough in time: the thread is
    reparented OUT of whatever widget was about to own its destruction
    (`setParent(None)`), kept alive by a REAL reference in `_orphaned` (a
    local reference going out of scope would let garbage collection claim
    the Python wrapper regardless of Qt parentage the moment nothing else
    holds it — same fatal shape, a different cause), and only let go of
    once its own `finished` signal proves the OS thread actually joined —
    at which point `deleteLater()` is finally safe, because by then
    nothing is running to delete out from under.

    Lengthening the timeout is NOT a fix, and must never be mistaken for
    one: it only makes the crash rarer and slower to hit — trading an
    always-reproducible bug for one that shows up unpredictably, in the
    field, on whichever artist's connection is worst that day.
    """
    if worker is None:
        return
    worker.requestInterruption()
    if worker.wait(timeout_ms):
        return  # it actually finished in time — nothing more to do
    worker.setParent(None)
    _orphaned.add(worker)

    def _on_finished(w: "Worker" = worker) -> None:
        _orphaned.discard(w)
        w.deleteLater()

    worker.finished.connect(_on_finished)


def release_all(*, timeout_ms: int = 200) -> None:
    """The safety net itself — connected to `QCoreApplication.aboutToQuit`
    and registered with `atexit` the moment any `Worker` first starts
    (`_ensure_global_hooks`), so it runs regardless of whether any
    particular widget's own `shutdown()` got a chance to run first.
    `docs/facts/houdini.md` §14 is explicit that no single exit path is
    guaranteed on every kind of Houdini teardown — this is the same
    reasoning `client.py`'s `AcpClient` already applies to the ACP
    connection itself, applied here to every `Worker` there is.

    A short timeout on purpose: by the time this runs, the application is
    already on its way out, and a worker still running at this point is
    getting `release()`'s full reparent-and-keep-alive treatment anyway —
    there is nothing to gain from making shutdown itself slower to wait
    longer for a thread that has already had its normal, per-widget
    chance (`AgentPanel.shutdown()` and friends, each with their own
    longer timeout) to finish first.
    """
    for worker in list(_live):
        release(worker, timeout_ms=timeout_ms)


__all__ = ["Worker", "WorkerStopped", "release", "release_all"]

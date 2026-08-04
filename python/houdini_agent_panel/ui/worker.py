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
"""

from __future__ import annotations

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


class WorkerStopped(Exception):
    """Raise from `work()` to end quietly, with nothing reported.

    For a thread that notices `isInterruptionRequested()` half-way through
    and has nothing useful to hand back.
    """


__all__ = ["Worker", "WorkerStopped"]

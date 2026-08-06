"""Sends a bug report off the main thread.

Off-thread for the same reason every network call in this panel is: a
POST can take seconds (or, against the not-yet-live default endpoint,
however long a connect attempt takes to give up), and the one thread
Houdini paints its viewport with must never wait on that.

All the actual logic — payload shape, redaction, error classification —
lives in `bugreport.py`, pure Python and testable without Qt. This file
is only the `Worker` wrapper (`ui/worker.py`'s pattern: an exception here
becomes a `failed` signal and a log entry instead of a silently-dead
thread) that runs it there.
"""

from __future__ import annotations

from types import SimpleNamespace

from .. import bugreport
from .qt import Signal
from .terminal_login import TerminalLoginWorker
from .worker import Worker


class BugReportWorker(Worker):
    succeeded = Signal(str)  # the issue URL

    def __init__(self, endpoint: str, payload: dict, *, parent=None) -> None:
        super().__init__(parent)
        self._endpoint = endpoint
        self._payload = payload

    def work(self) -> None:  # noqa: D102 - Worker.work override
        # Same environment composition as the panel's other spawned/sent
        # network work (`SelfUpdateWorker`, `TerminalLoginWorker` itself)
        # — reused, not re-derived: Houdini's own process saw none of the
        # artist's shell profile (no proxy, on a studio network where
        # nothing reaches out without one), and `bugreport.post_report`
        # needs exactly the same widened environment to find it.
        env = TerminalLoginWorker.build_env(SimpleNamespace(env={}))
        # `bugreport.post_report` raises `BugReportError` with the message
        # already written for the artist (download vs write is this
        # module's own concern for `self_update.py`; here it's "reached
        # the server and got an answer" vs "never reached it at all" —
        # `Worker.run()` catches this and any OTHER exception the same
        # way, `str(exc)` is what `failed` carries either way).
        issue_url = bugreport.post_report(self._endpoint, self._payload, env=env)
        self.succeeded.emit(issue_url)


__all__ = ["BugReportWorker"]

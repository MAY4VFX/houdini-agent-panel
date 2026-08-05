"""Spawns the SEPARATE process a `client.TerminalAuth` points at (Kimi's own
`kimi login`, docs/facts/acp-sdk.md §13-14) and reads its output on a
background thread — `hou` is never touched here, only a subprocess's pipe.

Measured on a real `kimi login` run (§14): it prints a verification URL and
a device code, then polls with a spinner, unbounded, until killed —

    Please visit the following URL to finish authorization.
    Verification URL: https://www.kimi.com/code/authorize_device?user_code=14OI-AX7F
    ⠋ Waiting for user authorization...

— so the process has to stay ALIVE while the artist finishes the login in
their browser; stopping it early cancels the login. `AgentPanel` is the one
that decides when that's appropriate (leaving the sign-in screen, or the
panel closing) — this module only owns the process and the parsing.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess

from .. import orphans, shellenv
from ..logbook import logger as _logbook_logger
from .qt import Signal
from .worker import Worker, WorkerStopped

_log = _logbook_logger("houdini_agent_panel.ui.terminal_login")

#: Sampled once from a real run (docs/facts/acp-sdk.md §14) — the format is
#: NOT established as stable across kimi versions or runs (n=1). A line that
#: doesn't match simply never fires `url_found`; the artist still sees every
#: raw line via `line_received`, so nothing is hidden if this regex goes stale.
_URL_RE = re.compile(r"Verification URL:\s*(\S+)")
#: Kimi's own URL happens to carry the device code as a query parameter —
#: convenient to show separately, but optional: `url_found` still fires with
#: an empty code if this doesn't match.
_CODE_RE = re.compile(r"[?&]user_code=([\w-]+)")


class TerminalLoginWorker(Worker):
    """Runs `terminal_auth.command` and reads its combined stdout+stderr
    line by line, off the main thread.

    Subclasses `ui/worker.py`'s `Worker`: an exception here becomes a
    `failed` signal and a log entry instead of a silently-dead thread — the
    exact trap that class exists to close, and just as real for a process
    the panel spawns as for a network round trip.
    """

    #: Every line, trimmed — the raw-output fallback for when the artist's
    #: agent version prints something `_URL_RE` doesn't recognise.
    line_received = Signal(str)
    #: `(url, code)` — `code` is `""` if the URL carried none.
    url_found = Signal(str, str)
    #: The process's own exit code. Not evidence of success OR failure by
    #: itself — docs/facts/acp-sdk.md §14 explicitly could not measure what
    #: kimi prints when the login actually succeeds (the probe killed it
    #: first, deliberately) — `AgentPanel` treats this as "the process is
    #: gone", nothing more.
    exited = Signal(int)

    def __init__(self, agent_id: str, terminal_auth, *, cwd: str, parent=None) -> None:
        super().__init__(parent)
        self._agent_id = agent_id
        self._terminal_auth = terminal_auth
        self._cwd = cwd
        #: Read only from the thread that owns it, EXCEPT `stop()` — see
        #: its own docstring for why that one call is safe from the main
        #: thread regardless.
        self._process: subprocess.Popen | None = None

    def work(self) -> None:
        ta = self._terminal_auth
        if not ta.command:
            # The SDK's stock `TerminalAuthMethod` shape (`client.
            # TerminalAuth.command is None`) — `AgentPanel._start_terminal_
            # login` is not supposed to construct this worker for that case
            # at all (unmeasured, no agent uses it); this is a defensive
            # backstop, not a path meant to be reached.
            raise WorkerStopped

        # Same reasoning as `client.py::AcpWorker.do_start`: a GUI Houdini
        # never saw the artist's shell profile, and the credentials this
        # login needs live there, not in Houdini's own bare environment.
        env = shellenv.merged(dict(os.environ), ta.env)

        process = subprocess.Popen(
            [ta.command, *ta.args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=self._cwd,
            text=True,
            bufsize=1,
        )
        self._process = process
        # Same insurance as every agent process (`orphans.py`'s own module
        # docstring): if Houdini dies outright between here and this
        # process ending, nothing else will ever notice it's still
        # running. Keyed with a suffix so it never collides with the
        # AGENT's own record for the same `agent_id` (that dict is keyed by
        # pid, not agent_id, so there's no real collision risk either way —
        # this is just for a human reading the file later).
        with contextlib.suppress(Exception):
            orphans.record_started(
                agent_id=f"{self._agent_id}:terminal-auth",
                pid=process.pid,
                command=ta.command,
                args=list(ta.args),
                cwd=self._cwd,
            )
        try:
            assert process.stdout is not None
            for line in process.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue
                self.line_received.emit(line)
                match = _URL_RE.search(line)
                if match:
                    url = match.group(1)
                    code_match = _CODE_RE.search(url)
                    self.url_found.emit(url, code_match.group(1) if code_match else "")
        finally:
            exit_code = process.wait()
            with contextlib.suppress(Exception):
                orphans.record_stopped(process.pid)
            self.exited.emit(exit_code)

    def stop(self) -> None:
        """Terminate the child. Safe to call from the main thread (unlike
        reading `self._process`'s pipes, which only ever happens on this
        worker's own thread) — `Popen.terminate()`/`.kill()` themselves are
        thread-safe, they just send a signal.

        Unlike cancelling a pending `authenticate()` (nothing to cancel —
        docs/facts/acp-sdk.md §12), this genuinely stops something: the
        process is ours alone, spawned by this worker and read by nobody
        else, so ending it early is a real, safe choice — at the cost of
        cancelling whatever login the artist had in progress in their
        browser, which `AgentPanel` says plainly when this is offered.
        """
        process = self._process
        if process is None or process.poll() is not None:
            return
        with contextlib.suppress(OSError):
            process.terminate()


__all__ = ["TerminalLoginWorker"]

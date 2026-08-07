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

Claude's own `setup-token` (§14, and `AgentPanel._builtin_terminal_auth_for`
— it isn't advertised by any `AuthMethod` at all, so it's the panel's own
data, not the wire's) is a THIRD shape: it prints an OAuth URL, then stops
at an actual input prompt ("Paste code here if prompted >") and waits for
ONE line back — `send_line` is what answers that, still no terminal
emulator, still not what opencode's arrow-key menu would need (§14 already
settled that one: no).

A live failure on the owner's own Linux box (docs/facts/acp-sdk.md §18)
found a fourth shape from a newer `claude-code` build (2.1.224): a SECOND
prompt string, "Or paste the redirect URL here: ", extracted from the
installed binary itself — a build's own `no_tty_stdin` handling, going by
the strings sitting next to it. Piped stdin (exactly what `subprocess.
Popen(stdin=PIPE)` here gives it) is not a terminal, so that binary is
free to be reformatting THIS panel's exact case differently from an
interactive run — the one this module was originally measured against.
Two changes follow from that: a second marker, and ANSI stripped from
whatever's checked, since a build willing to reformat for a non-tty stdin
is equally free to colour it.
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
#: Claude's `setup-token` prints a bare OAuth URL (no separate code — the
#: URL itself is the whole artefact, docs/facts/acp-sdk.md §14) on its own
#: line, distinct from kimi's "Verification URL:" prefix. Matched only when
#: `_URL_RE` above didn't already claim the line, so a future agent that
#: happens to print both shapes doesn't double-fire.
_BARE_URL_RE = re.compile(r"https?://\S+")
#: What Claude's `setup-token` prints right before it blocks on stdin —
#: two shapes measured so far (§14, §18), both matched loosely
#: (case-insensitive substring) since the exact wording is exactly the
#: kind of detail a future CLI version could reword again: "Paste code
#: here if prompted >" (an interactive run) and "Or paste the redirect
#: URL here:" (found in a build's own binary strings, apparently specific
#: to a non-tty stdin — precisely what this module always gives it).
_INPUT_PROMPT_MARKERS = ("paste code here", "paste the redirect url here")
#: CSI escape sequences (`\x1b[` + parameters + a final letter) — cursor
#: moves, colour, clearing. Stripped before anything is matched against
#: `_INPUT_PROMPT_MARKERS`/the URL patterns, and before a line is shown to
#: the artist: a build using colour or cursor tricks around its own prompt
#: (§18: the build measured there uses `\r`-redraws for at least some of
#: its output) must not be able to hide plain text inside escape noise
#: either from our detection or from the artist's own eyes.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
#: A run this long with no line break, no carriage return and no
#: recognised marker is almost certainly not a human-paced prompt —
#: flushed as a line anyway so raw output is never invisible for good
#: (§18: "must not be able to wait forever with no way forward"),
#: regardless of whether the child ever sends a `\n`/`\r` at all.
_FORCE_FLUSH_CHARS = 500


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


#: A device code (kimi's `user_code`, an OAuth redirect fragment) is meant
#: to be read and typed by a human — not a secret, and the artist needs to
#: SEE it to use it. An actual token/key is a different shape: long,
#: opaque, never meant to be retyped. This is a width heuristic, not a
#: parser: any run this long made of token-shaped characters is masked,
#: whatever it actually is — the cost of a false positive (an unusually
#: long device code, never measured) is a slightly less readable log line;
#: the cost of a false negative is a credential on disk.
_LOOKS_LIKE_A_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-\.]{24,}")


def _redact_for_log(line: str) -> str:
    def _mask(match: "re.Match[str]") -> str:
        return f"<{len(match.group(0))} chars redacted>"

    return _LOOKS_LIKE_A_TOKEN_RE.sub(_mask, line)


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
    #: `(url, code)` — `code` is `""` if the URL carried none (Claude's own
    #: URL always fires this with an empty code — see `_BARE_URL_RE`).
    url_found = Signal(str, str)
    #: The child just printed something that looks like an input prompt
    #: (Claude's `setup-token`, §14) — `AgentPanel` shows the one-line input
    #: field only now, from this, never from a timer.
    input_requested = Signal()
    #: The process's own exit code. Not evidence of success OR failure by
    #: itself — docs/facts/acp-sdk.md §14 explicitly could not measure what
    #: kimi prints when the login actually succeeds (the probe killed it
    #: first, deliberately) — `AgentPanel` treats this as "the process is
    #: gone", nothing more.
    exited = Signal(int)
    #: Fired once, right before spawning, ONLY when `resolve_command` was
    #: given and actually changed what runs — the panel's own "run it
    #: yourself" fallback advice (`_on_terminal_login_stuck`) is built from
    #: `terminal_auth.command`/`.args` before this worker even starts, and
    #: would otherwise go on naming the WRONG command once the real one was
    #: decided off-thread (a bundled binary, not the npx line it started as
    #: a placeholder for).
    command_resolved = Signal(str, list)

    def __init__(
        self,
        agent_id: str,
        terminal_auth,
        *,
        cwd: str,
        parent=None,
        resolve_command=None,
    ) -> None:
        super().__init__(parent)
        self._agent_id = agent_id
        self._terminal_auth = terminal_auth
        self._cwd = cwd
        #: Optional, called at the START of `work()` (already off the main
        #: thread) to get the REAL `(command, args)`, overriding
        #: `terminal_auth`'s own — for a caller whose best command needs
        #: work too slow for the main thread to decide up front (a
        #: filesystem search plus verifying each candidate actually runs).
        #: `None` means `terminal_auth.command`/`.args` are already final,
        #: the ordinary case (Kimi's own `kimi login`, the SDK's stock
        #: `TerminalAuthMethod`). What the callable decides BETWEEN stays
        #: entirely the caller's own knowledge — this only provides the
        #: "figure it out off the main thread" mechanism, generic to any
        #: terminal-auth command, not just Claude's.
        self._resolve_command = resolve_command
        #: Read only from the thread that owns it, EXCEPT `stop()`/
        #: `send_line()` — see their own docstrings for why those two
        #: calls are safe from the main thread regardless.
        self._process: subprocess.Popen | None = None

    @staticmethod
    def build_env(terminal_auth) -> dict[str, str]:
        """The environment this process actually runs in — a plain
        subprocess like any other, so it gets the SAME proxy treatment the
        agent process itself does (`runtime.py::_with_proxy`). Reported
        for real: on a machine where nothing reaches the network without
        the studio's proxy (exactly why `proxy_url` exists in Settings),
        a login command spawned without it hangs indistinguishably from
        the dead button issue #33 already fixed once.

        Precedence, weakest first — same shape as `runtime._with_proxy`'s
        own docstring: the OS environment, widened by the artist's login
        shell (`shellenv.merged`, same reason `client.py::do_start` needs
        it — Houdini never saw their profile), then the studio proxy the
        artist typed into Settings, then this METHOD's own env last —
        `terminal_auth.env` is the most specific thing here (currently
        always `{}` for kimi, measured; Claude's own built-in recipe also
        sets none), so it wins over a general proxy default the same way
        an agent's own explicit env already does.
        """
        from .. import proxy as proxy_module
        from .. import settings as settings_module

        current_settings = settings_module.load()
        env = shellenv.merged(dict(os.environ), proxy_module.child_env(current_settings))
        env.update(terminal_auth.env)
        return env

    def work(self) -> None:
        ta = self._terminal_auth
        command, args = ta.command, list(ta.args)
        if self._resolve_command is not None:
            # Off the main thread now — this is exactly the point of
            # accepting a callable instead of a final `(command, args)`:
            # whatever it does (a filesystem search, running `--version`
            # on each candidate to confirm it actually works) is free to
            # take real time here, the same freedom every other worker in
            # this codebase already has.
            resolved = self._resolve_command()
            if resolved is not None and resolved != (command, args):
                command, args = resolved
                self.command_resolved.emit(command, list(args))
        if not command:
            # The SDK's stock `TerminalAuthMethod` shape (`client.
            # TerminalAuth.command is None`) — `AgentPanel._start_terminal_
            # login` is not supposed to construct this worker for that case
            # at all (unmeasured, no agent uses it); this is a defensive
            # backstop, not a path meant to be reached.
            raise WorkerStopped

        env = self.build_env(ta)

        # The command/args themselves are never secret — they're the
        # panel's own recipe (a fixed npx invocation, or the SDK's own
        # `TerminalAuth`) or, at most, a device-code CLI name. Nothing
        # from `env` is logged here or anywhere below: an artist's proxy
        # credentials or shell profile could easily be sitting in there.
        _log.info(
            "terminal login: spawning %s %s",
            command,
            _redact_for_log(" ".join(args)),
        )

        process = subprocess.Popen(
            [command, *args],
            stdin=subprocess.PIPE,
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
                command=command,
                args=list(args),
                cwd=self._cwd,
            )
        url_already_found = False
        buffer = ""
        exit_code = None
        try:
            assert process.stdout is not None
            # Reading whole LINES (`for line in process.stdout`) was the
            # first cut here, and it deadlocks against Claude's own
            # `setup-token`: "Paste code here if prompted >" is an actual
            # input prompt, which never ends with a newline — the cursor
            # has to stay on that line for the human's answer to land next
            # to it. A line-iterating reader would sit forever waiting for
            # a "\n" that is never coming, against a child that is ALREADY
            # waiting on stdin: a real deadlock, not just a missed event.
            # Reading one character at a time costs nothing on output this
            # small and human-paced, and lets the prompt marker be seen
            # (and `input_requested` fired) the instant it appears,
            # newline or not.
            #
            # `\r` flushes exactly like `\n` now (docs/facts/acp-sdk.md
            # §18) — a build redrawing a status line with carriage returns
            # used to leave everything it printed sitting unseen in
            # `buffer` until (if ever) a real `\n` arrived; a spinner or a
            # progress line drawn that way now actually reaches the
            # artist, the same way a plain `\n`-terminated one already did.
            while True:
                char = process.stdout.read(1)
                if not char:
                    break  # EOF — the child closed its output
                if char not in ("\n", "\r") and len(buffer) < _FORCE_FLUSH_CHARS:
                    buffer += char
                    stripped = _strip_ansi(buffer)
                    if any(marker in stripped.lower() for marker in _INPUT_PROMPT_MARKERS):
                        self._emit_line(stripped)
                        buffer = ""
                        self.input_requested.emit()
                        _log.info("terminal login: input prompt detected")
                    continue
                # A flush: a real separator, or `buffer` ran long enough
                # that sitting on it any longer would mean invisible
                # output again — see `_FORCE_FLUSH_CHARS`.
                if char not in ("\n", "\r"):
                    buffer += char
                line = _strip_ansi(buffer)
                buffer = ""
                if not line.strip():
                    continue
                self._emit_line(line)
                if any(marker in line.lower() for marker in _INPUT_PROMPT_MARKERS):
                    self.input_requested.emit()
                    _log.info("terminal login: input prompt detected")
                if not url_already_found:
                    match = _URL_RE.search(line)
                    if match:
                        url = match.group(1)
                        code_match = _CODE_RE.search(url)
                        self.url_found.emit(url, code_match.group(1) if code_match else "")
                        url_already_found = True
                    else:
                        bare = _BARE_URL_RE.search(line)
                        if bare:
                            self.url_found.emit(bare.group(0), "")
                            url_already_found = True
        finally:
            exit_code = process.wait()
            with contextlib.suppress(Exception):
                orphans.record_stopped(process.pid)
            _log.info("terminal login: exited, code=%s", exit_code)
            self.exited.emit(exit_code)

    def _emit_line(self, line: str) -> None:
        self.line_received.emit(line)
        _log.info("terminal login line: %s", _redact_for_log(line))

    def send_line(self, text: str) -> None:
        """Write one line to the child's stdin — the one thing Claude's
        `setup-token` needs once the artist has the code from their
        browser (docs/facts/acp-sdk.md §14: it blocks at "Paste code here
        if prompted >" for exactly this). Safe to call from the main
        thread while `work()` runs on this worker's own thread — writing
        to a pipe's file descriptor is a plain syscall, it doesn't need
        Qt's thread-affinity rules the way touching a widget would.
        """
        process = self._process
        if process is None or process.poll() is not None or process.stdin is None:
            return
        # The content itself is never logged — it's what the artist just
        # pasted from their browser, closer to a credential than a device
        # code is. Only the fact that something was sent.
        _log.info("terminal login: artist input submitted (%d chars)", len(text))
        with contextlib.suppress(OSError, ValueError):
            process.stdin.write(text.rstrip("\n") + "\n")
            process.stdin.flush()

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
        _log.info("terminal login: stop() requested")
        with contextlib.suppress(OSError):
            process.terminate()


__all__ = ["TerminalLoginWorker"]

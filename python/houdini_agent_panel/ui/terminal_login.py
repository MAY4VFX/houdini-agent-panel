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

A second live failure (docs/facts/acp-sdk.md §20) found the same build
going all the way to SILENT with plain pipes: the bundled binary
`_resolve_claude_terminal_command` finds prints nothing at all — no URL,
no prompt, not even the non-tty variant above — and just sits there. It
wants a REAL terminal, not merely non-empty stdin. `use_pty=True` is the
fix, and it changes what this module has to parse: a real pty run
(measured, mayfx02) is drastically richer than either pipe shape — cursor-
absolute-positioning escapes (`\x1b[<N>G`, simulating spaces by MOVING
the cursor rather than sending space characters — stripping them without
accounting for this loses the spacing, corrupting substring matches: a
real capture produced literal `"Pastecodehereifprompted>"`, no spaces at
all), OSC-8 terminal hyperlinks wrapping the OAuth URL (`\x1b]8;id=X;
<url><BEL><display text>\x1b]8;;<BEL>` — the display text this build
renders is a truncated, REPEATED copy of the same URL, and a naive
`\\S+`-style URL match runs straight through the BEL into it, corrupting
what should be a clean link), and animated spinner frames arriving as a
rapid burst of single-glyph "lines". All three are handled below,
each with the real captured bytes that justified the fix — see
`_strip_ansi`, `_marker_in`, `_BARE_URL_RE`, `_is_spinner_noise`.

A completed run (docs/facts/acp-sdk.md §21) overturned the model this
whole module was built on: `setup-token` does not sign anything in. It
MINTS a subscription-scoped OAuth token, prints it exactly ONCE ("Store
this token securely. You won't be able to see it again."), and exits —
no credentials file is ever written, so `signin_evidence`'s file check
can never fire for this flow, and a real owner's token going uncaptured
the first time this shipped is the report that added `token_captured`
below. See `_OAUTH_TOKEN_RE`'s own comment for the exact wording and why
it's matched on the confirmed variable name, not a generic parser.

§20's own "Windows" note used to end here: "no Windows machine exists in
this project... that measurement is future work". This is that work,
still without a Windows machine to run it on. `_conpty_windows.py` is
the Windows counterpart to `_PtyMasterReader` below — a real ConPTY
(`CreatePseudoConsole` + `CreateProcessW`, `ctypes`-only, no third-party
dependency) instead of `pty.openpty()`, for the identical reason: the
bundled binary needs a real controlling terminal, and Windows has no
POSIX pty at all. The one rule that changed from §20's own Windows note:
back then, an unavailable pty silently forced the plain-pipe path — the
exact path already measured (§20's own opening line) to produce ZERO
output for this specific binary. That silent downgrade is gone. On
Windows, `_use_pty` (POSIX) and `_use_conpty` (this module) are mutually
exclusive and never fall through to plain pipes for `claude-setup-token`
— an unavailable or failing ConPTY now raises a `ConPtyError` with a
concrete step and error code, which `Worker.run()` turns into a `failed`
signal the artist actually sees, instead of a screen that never moves.
See `_conpty_windows.py`'s own module docstring for exactly what is, and
is not, verified about the implementation itself.
"""

from __future__ import annotations

import codecs
import contextlib
import os
import platform
import re
import subprocess

from .. import orphans, shellenv
from ..logbook import logger as _logbook_logger
from .qt import Signal
from .worker import Worker, WorkerStopped

try:  # pragma: no cover - exercised only on POSIX, where this project runs its tests
    import pty
    import termios

    _PTY_AVAILABLE = True
except ImportError:  # Windows has no pty/termios modules at all
    _PTY_AVAILABLE = False

try:
    from . import _conpty_windows
except Exception:  # noqa: BLE001 - a diagnostics-capable import must never break the panel
    _conpty_windows = None  # type: ignore[assignment]

#: Mirrors `_PTY_AVAILABLE`'s own role, deliberately kept as a SEPARATE
#: flag (module docstring, the new §20 addendum) rather than folded into
#: it — `_PTY_AVAILABLE` means "POSIX pty available", this means "ConPTY
#: available", and the two are never both true on the same machine (one
#: is POSIX-only, the other Windows-only) but conflating their NAMES
#: would make a future reader assume they're interchangeable, which is
#: exactly the assumption this module's own Windows section had to
#: correct once already (a silent pipe fallback that looked like "some
#: kind of terminal was used" but wasn't).
_CONPTY_AVAILABLE = bool(_conpty_windows is not None and _conpty_windows._CONPTY_AVAILABLE)

_log = _logbook_logger("houdini_agent_panel.ui.terminal_login")

#: Sampled once from a real run (docs/facts/acp-sdk.md §14) — the format is
#: NOT established as stable across kimi versions or runs (n=1). A line that
#: doesn't match simply never fires `url_found`; the artist still sees every
#: raw line via `line_received`, so nothing is hidden if this regex goes stale.
#:
#: Excludes BEL (`\x07`) and ESC (`\x1b`) from the URL body, alongside plain
#: whitespace — neither can be part of a real URL, and under a pty (§20) an
#: OSC-8 terminal hyperlink wraps the OAuth URL as `\x1b]8;id=X;<url><BEL>
#: <display text>\x1b]8;;<BEL>`; a bare `\S+` runs straight through that BEL
#: into the display text (measured: a real capture produced the real URL
#: with the SAME url, truncated and re-wrapped, appended after it — a
#: broken link, not a cosmetic glitch). No agent measured so far puts a URL
#: inside a hyperlink on the plain-pipe path, only under a pty, but the
#: exclusion costs nothing either way.
_URL_RE = re.compile(r"Verification URL:\s*([^\s\x07\x1b]+)")
#: Kimi's own URL happens to carry the device code as a query parameter —
#: convenient to show separately, but optional: `url_found` still fires with
#: an empty code if this doesn't match.
_CODE_RE = re.compile(r"[?&]user_code=([\w-]+)")
#: Claude's `setup-token` prints a bare OAuth URL (no separate code — the
#: URL itself is the whole artefact, docs/facts/acp-sdk.md §14) on its own
#: line, distinct from kimi's "Verification URL:" prefix. Matched only when
#: `_URL_RE` above didn't already claim the line, so a future agent that
#: happens to print both shapes doesn't double-fire. Same BEL/ESC exclusion
#: as `_URL_RE`, same reason.
_BARE_URL_RE = re.compile(r"https?://[^\s\x07\x1b]+")
#: What Claude's `setup-token` prints right before it blocks on stdin —
#: two shapes measured so far (§14, §18), both matched loosely
#: (case-insensitive substring) since the exact wording is exactly the
#: kind of detail a future CLI version could reword again: "Paste code
#: here if prompted >" (an interactive run) and "Or paste the redirect
#: URL here:" (found in a build's own binary strings, apparently specific
#: to a non-tty stdin — precisely what this module always gives it).
#: Checked with `_marker_in`, not a plain substring test — see its own
#: docstring for why (§20: a real pty run loses ALL spacing around these).
_INPUT_PROMPT_MARKERS = ("paste code here", "paste the redirect url here")
#: CSI escape sequences (`\x1b[` + parameters + a final letter) — cursor
#: moves, colour, clearing. Stripped before anything is matched against
#: `_INPUT_PROMPT_MARKERS`/the URL patterns, and before a line is shown to
#: the artist: a build using colour or cursor tricks around its own prompt
#: (§18: the build measured there uses `\r`-redraws for at least some of
#: its output) must not be able to hide plain text inside escape noise
#: either from our detection or from the artist's own eyes.
#:
#: `>` added to the parameter class and the bare two-byte `\x1b7`/`\x1b8`
#: (save/restore cursor) added alongside — both measured missing from a
#: real pty capture (§20): `\x1b[>0q` (a device-attributes query this
#: build sends on startup) uses a `>` prefix the CSI parameter class
#: didn't include, and `\x1b7`/`\x1b8` aren't `\x1b[...` sequences at all,
#: a different (2-byte, no bracket) escape family entirely.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?>]*[ -/]*[@-~]|\x1b[78]")
#: A run this long with no line break, no carriage return and no
#: recognised marker is almost certainly not a human-paced prompt —
#: flushed as a line anyway so raw output is never invisible for good
#: (§18: "must not be able to wait forever with no way forward"),
#: regardless of whether the child ever sends a `\n`/`\r` at all.
#:
#: Sized well above real OSC-8 lines, not just above plain text: a real
#: pty capture (§20) of the OAuth verification URL — wrapped as an OSC-8
#: hyperlink, escape sequence and repeated display text included — ran to
#: 448 characters, within 52 of the old 500-char threshold. That length
#: tracks variable OAuth query params (`state`, `code_challenge`,
#: `client_id`); a longer state token in a future build would silently
#: force-flush mid-sequence, splitting the OSC-8 escape and breaking both
#: `_URL_RE`/`_BARE_URL_RE` matching and the terminator that lets
#: `_ANSI_RE` strip it. 2000 leaves several times the measured length as
#: headroom.
_FORCE_FLUSH_CHARS = 2000
#: `claude setup-token` does not sign anything in — it MINTS a subscription-
#: scoped OAuth token and prints it ONCE (docs/facts/acp-sdk.md §21,
#: overturning what §14/§20 assumed): no credentials file is ever written,
#: so waiting for one (`signin_evidence`) can never succeed for this
#: command. The exact wording, confirmed from the real bundled binary's own
#: string table, not guessed: "Your OAuth token (valid for 1 year): ...
#: Store this token securely. You won't be able to see it again. Use this
#: token by setting: export CLAUDE_CODE_OAUTH_TOKEN=<token>" — the variable
#: name is the anchor `_OAUTH_TOKEN_RE` below matches on, not a generic
#: "any KEY=VALUE line" parser: a real pty capture squeezes ALL whitespace
#: out (§20's own finding, same mechanism as `_marker_in`), so "export" and
#: the variable name arrive glued together as one run with no space
#: between them — a generic parser would capture "exportCLAUDE_CODE_OAUTH_
#: TOKEN" as the "variable name", which is not a real environment variable
#: anything reads. Anchoring on the known, confirmed constant sidesteps
#: that entirely.
_OAUTH_TOKEN_ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
_OAUTH_TOKEN_RE = re.compile(re.escape(_OAUTH_TOKEN_ENV_VAR) + r"=(\S+)")
#: The label line that starts this build's token dump — matched the same
#: whitespace-insensitive way as `_INPUT_PROMPT_MARKERS`, for the same
#: reason (§20). Once seen, every line emitted for the rest of this run is
#: redacted the same way the log already is: a real, usable secret must
#: never reach the transcript either, unlike a device code (which the
#: artist is meant to read and type) or the OAuth URL (not a secret at
#: all) — see `_emit_line`.
_OAUTH_TOKEN_LABEL = "your oauth token"

#: A pty run's own animated "thinking" spinner (§20: a real capture showed
#: this build cycling `✢ * ✶ ✻ ✽ ✻ ✶ * ✢ ·`, one glyph per redraw frame,
#: each arriving as its own "line" once `\r`-flushed) — pure animation
#: noise, not information, and unfiltered it would flash through the
#: artist's own "still working" field and the log as dozens of near-
#: identical single-character entries. Never fires on a real word (the
#: shortest word — "a" — passes `isalnum()`); only a lone symbol/glyph is
#: dropped.
_SPINNER_NOISE_RE = re.compile(r"^\W$", re.UNICODE)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _marker_in(marker: str, text: str) -> bool:
    """Is `marker` present in `text`, ignoring case AND all whitespace on
    both sides?

    A plain substring check was enough for every shape measured before a
    real pty run (§20): a real `claude setup-token` capture produced the
    literal string `"Pastecodehereifprompted>"` for what a normal
    terminal renders as "Paste code here if prompted >" — cursor-
    absolute-positioning escapes (`\\x1b[<N>G`, "move to column N")
    simulate spacing by MOVING the cursor rather than sending space
    characters, so stripping them (necessary to read the text at all)
    loses the spacing along with it. `"paste code here" in text.lower()`
    would never fire again on this build. Comparing with all whitespace
    removed on both sides is immune to however much — or how little —
    spacing a given render happens to preserve.
    """
    squeeze = lambda s: re.sub(r"\s+", "", s.lower())  # noqa: E731
    return squeeze(marker) in squeeze(text)


def _is_spinner_noise(line: str) -> bool:
    return bool(_SPINNER_NOISE_RE.match(line.strip()))


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


class _PtyMasterReader:
    """Adapts a pty master fd to the same one-character-at-a-time
    `.read(1) -> str` interface `process.stdout.read(1)` already gives
    the plain-pipe path (`text=True` on `Popen`), so `TerminalLoginWorker.
    work`'s own read loop needs only one version, not two.

    Two real pty quirks, both measured directly (mayfx02, a real
    `claude setup-token` run under a pty, §20) rather than assumed:

    - `os.read()` on the master fd raises `OSError` (`EIO`) once the
      slave side closes, instead of returning `b""` the way a pipe's EOF
      does — caught here and turned into the same `""` a pipe EOF
      already means to the caller, so `work()` doesn't need to know the
      difference.
    - Multi-byte UTF-8 characters (this build's own spinner glyphs are
      ALL multi-byte) can arrive split across two separate `os.read`
      calls. An incremental decoder, not a bare `.decode()` per chunk,
      is what keeps a character split mid-sequence from becoming two
      mangled ones (a naive per-chunk decode raised
      `UnicodeDecodeError` on this exact capture until this was added).
    """

    def __init__(self, fd: int) -> None:
        self._fd = fd
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending = ""

    def read(self, _size: int = 1) -> str:
        while not self._pending:
            try:
                chunk = os.read(self._fd, 4096)
            except OSError:
                return ""  # EIO — the slave side closed; same meaning as a pipe's b""
            if not chunk:
                return ""
            self._pending = self._decoder.decode(chunk)
        char, self._pending = self._pending[0], self._pending[1:]
        return char


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
    #: `(env_var, token)` — `claude setup-token` mints a subscription-scoped
    #: OAuth token and prints it exactly once (docs/facts/acp-sdk.md §21);
    #: this is the one and only chance to capture it. Never logged, never
    #: on `line_received` — see `_OAUTH_TOKEN_RE`/`_emit_line`.
    token_captured = Signal(str, str)
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
        use_pty: bool = False,
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
        #: Give the child a real pty instead of plain pipes — see the
        #: module's own docstring (§20) for why: Claude's bundled binary
        #: prints NOTHING at all over plain pipes and just sits there,
        #: unlike every other command measured for this class so far.
        #: Defaults to `False`, unchanged plain-pipe behaviour, because
        #: it's the one already MEASURED correct for everything else —
        #: Kimi's own `kimi login`, run for real (not the ACP channel
        #: probe from §13, a different target entirely): plain pipes
        #: already produce its `Verification URL:` line immediately, no
        #: pty needed. Silently forced back to `False` on a platform with
        #: no `pty` module (Windows) — see `_PTY_AVAILABLE`; Claude's
        #: setup-token stays silent there until someone with a Windows
        #: machine can measure what it actually needs.
        self._use_pty = use_pty and _PTY_AVAILABLE
        #: The Windows counterpart to `self._use_pty` — requested
        #: whenever the caller asked for a real terminal but POSIX `pty`
        #: isn't there at all (i.e. this is Windows). Deliberately NOT
        #: gated on `_CONPTY_AVAILABLE` here: `work()` is what decides
        #: between "spawn via ConPTY" and "raise a clear, specific
        #: error" — this flag only records what was ASKED for, so a
        #: Windows machine with no ConPTY support still gets a real
        #: explanation instead of quietly being treated as if `use_pty`
        #: had never been passed at all.
        self._use_conpty = use_pty and not self._use_pty and platform.system() == "Windows"
        #: Read only from the thread that owns it, EXCEPT `stop()`/
        #: `send_line()` — see their own docstrings for why those two
        #: calls are safe from the main thread regardless.
        self._process: subprocess.Popen | None = None
        #: The pty master fd, only while `self._use_pty` and `work()` is
        #: running — `send_line()` writes here instead of `process.stdin`
        #: (which doesn't exist for a pty-backed Popen; see `work()`).
        self._pty_master_fd: int | None = None
        #: The `_conpty_windows.ConPtyProcess`, only while
        #: `self._use_conpty` and `work()` is running — `send_line()`
        #: writes through its `.write()` instead of `process.stdin`, the
        #: same way `self._pty_master_fd` stands in for it on POSIX.
        self._conpty_process = None
        #: Set once `_OAUTH_TOKEN_LABEL` is seen — see its own comment.
        #: Every line emitted from then on is redacted before reaching
        #: `line_received`, not only the log.
        self._token_flow_active = False

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

        if self._use_pty:
            # A real pty, not plain pipes — see the module docstring
            # (§20) for why: Claude's bundled `setup-token` prints
            # nothing at all otherwise. `master_fd` is ours to read/write;
            # `slave_fd` becomes the child's stdin/stdout/stderr, all
            # three, exactly what a real terminal gives a foreground
            # process.
            master_fd, slave_fd = pty.openpty()
            with contextlib.suppress(termios.error):
                # Local echo off — measured to make NO observable
                # difference either way (a real run with it left on
                # produced identical output, §20): this build already
                # puts the pty into raw mode itself once it reaches an
                # actual input prompt, overriding whatever's set here.
                # Kept anyway as a second line of defence for whatever
                # command runs here next that might not manage its own
                # echo — cheap, and `termios.error` (an unsupported fd,
                # not expected here but not worth crashing over) is the
                # only way this can fail.
                attrs = termios.tcgetattr(slave_fd)
                attrs[3] = attrs[3] & ~termios.ECHO  # lflags
                termios.tcsetattr(slave_fd, termios.TCSANOW, attrs)
            process = subprocess.Popen(
                [command, *args],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=env,
                cwd=self._cwd,
                start_new_session=True,  # the child becomes its own session leader — needed for the pty to act as its controlling terminal, same as a real interactive shell would give it
            )
            os.close(slave_fd)  # the child has its own dup'd copy from Popen; this parent-side one is no longer needed
            self._pty_master_fd = master_fd
            reader = _PtyMasterReader(master_fd)
        elif self._use_conpty:
            # Windows: no POSIX pty, but the bundled binary needs a REAL
            # controlling terminal for exactly the same reason it does on
            # POSIX (§20) — a ConPTY is the Windows equivalent. Never
            # silently downgraded to plain pipes (the module docstring's
            # own Windows addendum, and `_conpty_windows.py`'s own
            # docstring, both explain why that used to be the trap): an
            # unavailable or failing ConPTY raises here, `Worker.run()`
            # turns that into a `failed` signal with a specific step and
            # error code, and `AgentPanel._on_terminal_login_failed`
            # already knows how to append the "run it yourself" fallback
            # command to whatever this says.
            if not _CONPTY_AVAILABLE:
                _log.error(
                    "terminal login: ConPTY unavailable on this Windows build "
                    "(CreatePseudoConsole not found in kernel32 — Windows 10 "
                    "1809+ is required)"
                )
                raise RuntimeError(
                    "This Windows build has no ConPTY support "
                    "(CreatePseudoConsole was not found in kernel32.dll — "
                    "Windows 10 version 1809 or later is required)."
                )
            _log.info("terminal login: spawning via ConPTY (windows)")
            conpty_process = _conpty_windows.spawn(
                command, list(args), env=env, cwd=self._cwd or None
            )
            self._conpty_process = conpty_process
            process = conpty_process
            reader = _conpty_windows.ConPtyReader(conpty_process)
        else:
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
            reader = process.stdout
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
            # newline or not. `reader` is `process.stdout` (plain pipes),
            # a `_PtyMasterReader` (POSIX pty), or a `_conpty_windows.
            # ConPtyReader` (Windows ConPTY) — all three expose the same
            # `.read(1) -> str` contract, so nothing below needs to know
            # which one it's talking to.
            #
            # `\r` flushes exactly like `\n` now (docs/facts/acp-sdk.md
            # §18) — a build redrawing a status line with carriage returns
            # used to leave everything it printed sitting unseen in
            # `buffer` until (if ever) a real `\n` arrived; a spinner or a
            # progress line drawn that way now actually reaches the
            # artist, the same way a plain `\n`-terminated one already did.
            while True:
                char = reader.read(1)
                if not char:
                    break  # EOF — the child closed its output
                if char not in ("\n", "\r") and len(buffer) < _FORCE_FLUSH_CHARS:
                    buffer += char
                    stripped = _strip_ansi(buffer)
                    if any(_marker_in(marker, stripped) for marker in _INPUT_PROMPT_MARKERS):
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
                if not line.strip() or _is_spinner_noise(line):
                    continue
                if not self._token_flow_active and _marker_in(_OAUTH_TOKEN_LABEL, line):
                    # From here on, `_emit_line` redacts too — checked
                    # BEFORE emitting THIS line, in case a future build
                    # ever puts the label and the token on the same line.
                    self._token_flow_active = True
                token_match = _OAUTH_TOKEN_RE.search(line)
                if token_match:
                    self.token_captured.emit(_OAUTH_TOKEN_ENV_VAR, token_match.group(1))
                    _log.info("terminal login: OAuth token captured (%s)", _OAUTH_TOKEN_ENV_VAR)
                self._emit_line(line)
                if any(_marker_in(marker, line) for marker in _INPUT_PROMPT_MARKERS):
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
            if self._pty_master_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(self._pty_master_fd)
                self._pty_master_fd = None
            if self._conpty_process is not None:
                with contextlib.suppress(Exception):
                    self._conpty_process.close()
                self._conpty_process = None
            _log.info("terminal login: exited, code=%s", exit_code)
            self.exited.emit(exit_code)

    def _emit_line(self, line: str) -> None:
        # A device code or an OAuth URL is safe to show — the artist is
        # meant to read or type it. Once `_token_flow_active` is set, this
        # run is minting a real, usable secret instead: redacted on the
        # LIVE signal too, not only the log (docs/facts/acp-sdk.md §21) —
        # the one case in this module where those two are not the same
        # decision.
        shown = _redact_for_log(line) if self._token_flow_active else line
        self.line_received.emit(shown)
        _log.info("terminal login line: %s", _redact_for_log(line))

    def send_line(self, text: str) -> None:
        """Write one line to the child's input — the one thing Claude's
        `setup-token` needs once the artist has the code from their
        browser (docs/facts/acp-sdk.md §14: it blocks at "Paste code here
        if prompted >" for exactly this). Safe to call from the main
        thread while `work()` runs on this worker's own thread — writing
        to a file descriptor is a plain syscall, it doesn't need Qt's
        thread-affinity rules the way touching a widget would.

        Three destinations, matching however `work()` spawned the child:
        the pty master fd (`self._use_pty`), the ConPTY input pipe
        (`self._use_conpty`, Windows), or `process.stdin` (plain pipes).
        Measured directly on POSIX (mayfx02, a real run, a garbage code
        that could never complete anything, killed right after): what
        comes back through the pty after writing shows the build masking
        its own input as a row of `*` — the character count matched the
        written text exactly, not the text itself — so there is nothing
        of the artist's actual code to redact here either way; only
        `send_line`'s own existing "how many characters" log line, never
        the content. NOT independently re-measured for the ConPTY branch
        (no Windows machine in this project) — the mechanism (writing to
        a pipe the child reads as its own stdin) is the same shape either
        way, but whether THIS build's own input-masking behaviour is
        identical on Windows is unverified.
        """
        process = self._process
        if process is None or process.poll() is not None:
            return
        line = text.rstrip("\n") + "\n"
        # The content itself is never logged — it's what the artist just
        # pasted from their browser, closer to a credential than a device
        # code is. Only the fact that something was sent.
        _log.info("terminal login: artist input submitted (%d chars)", len(text))
        if self._use_pty:
            if self._pty_master_fd is None:
                return
            with contextlib.suppress(OSError):
                os.write(self._pty_master_fd, line.encode("utf-8"))
            return
        if self._use_conpty:
            if self._conpty_process is None:
                return
            with contextlib.suppress(Exception):
                self._conpty_process.write(line.encode("utf-8"))
            return
        if process.stdin is None:
            return
        with contextlib.suppress(OSError, ValueError):
            process.stdin.write(line)
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

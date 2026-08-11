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

from .. import childproc, mcp_runtime, orphans, shellenv, token_check
from ..logbook import logger as _logbook_logger
from .qt import Signal
from .worker import Worker, WorkerStopped

try:  # pragma: no cover - exercised only on POSIX, where this project runs its tests
    import fcntl
    import pty
    import struct
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
#:
#: Briefly (0.8.12-0.8.13) restricted to a hand-picked set of "assigned"
#: CSI final bytes, on the theory that an incomplete `\x1b[10` was
#: terminating on the token's own "o" and eating it. That theory was
#: wrong — see docs/facts/acp-sdk.md §26 (rewritten): the real capture's
#: `\x1b[10G` was a complete, well-formed sequence the whole time, and no
#: incomplete CSI was ever involved. The missing character was one an
#: EARLIER redraw frame had already painted at that screen position; this
#: regex only ever sees one flush's own buffer, so it had no way to know
#: that cell was occupied. Fixed at the right layer instead — see
#: `_TerminalScreen`, which models cursor position across the whole
#: stream rather than one buffer at a time. The restriction bought
#: nothing real and cost a class of legitimate CSI sequences their
#: stripping, so it's gone; this is the plain standard range again.
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
#: `<token>` in that instruction line is LITERAL placeholder text the
#: artist is meant to replace by hand — not a slot the binary fills in.
#: §21 read the wording off the binary's string table and assumed the
#: opposite, and this module's own test encoded the same assumption by
#: interpolating a real token where the real build prints six characters
#: of punctuation. A real run on Linux (mayfx02, 2026-08-07) settles it:
#:
#:     Your OAuth token (valid for 1 year):
#:     <the token — 79 characters, ALONE on its own line>
#:     Store this token securely. You won't be able to see it again.
#:     Use this token by setting: export CLAUDE_CODE_OAUTH_TOKEN=<token>
#:
#: So the value is never on the `VAR=` line at all. It arrives bare, one
#: line after `_OAUTH_TOKEN_LABEL`, which is why capture anchors on that
#: label rather than on the variable name. The `VAR=` path is kept only
#: for a future build that might really interpolate there — and is now
#: shape-checked, so the literal `<token>` can never be stored and
#: reported as a successful sign-in.
_TOKEN_VALUE_RE = re.compile(r"[A-Za-z0-9_\-\.]{24,}")
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


#: Long enough that a secret can't be reconstructed from what survives,
#: short enough that `sk-ant-` — the part that turned out to matter — is
#: still printed literally.
_SHAPE_RUN_RE = re.compile(r"[A-Za-z0-9_\-\.]{8,}")


#: How many characters of each masked run to show. A CSI's parameters and
#: final byte are token-shaped, so they get swallowed into the run that
#: follows them — and those are exactly the bytes that decide whether a
#: sequence was `\x1b[10G` (complete, final byte `G`) or `\x1b[10`
#: (incomplete, terminating on whatever came next). Four characters names
#: that, while a 100-character secret stays a length.
#:
#: What leaks is the head of the run: for the token itself that is the
#: published, constant `sk-a`/`oat0` prefix, not entropy. Masking it
#: instead cost a whole round of guessing — a shape of `<9>` fitted two
#: different layouts that a single visible character would have told
#: apart at a glance.
_SHAPE_HEAD = 4


#: What pressing Enter actually sends on a terminal: carriage return, not
#: line feed. A keyboard has no LF key — the terminal transmits `\r`, and
#: it is the receiving side that decides what to do with it.
#:
#: This module's own docstring already records that this build "puts the
#: pty into raw mode itself once it reaches an actual input prompt". Raw
#: mode is exactly the mode with no `ICRNL` translation, so a `\n` written
#: there is a line feed and nothing else — never a submit. Measured on the
#: owner's machine (2026-08-08, first time anyone ever used this path):
#: the pasted code reached the child (it echoed the row of `*` that masks
#: its own input, one per character) and the process then waited forever,
#: because Enter never arrived.
#:
#: Safe for a cooperative reader too: a pty in canonical mode has `ICRNL`
#: on by default and turns this into the `\n` such a reader expects. Only
#: the plain-pipe path, which has no line discipline at all, still needs
#: a literal `\n` — see `send_line`.
_ENTER_ON_A_TERMINAL = "\r"


def _shape_for_log(raw: str) -> str:
    """The escape structure of a line, with every long run masked.

    Three times now a token has been silently corrupted between the pty
    and `settings.json` (§21, §25, §26), and every time the log could not
    say where, because it only ever recorded text AFTER `_strip_ansi` had
    already run. A stripped line cannot show which escape stripped it.

    This records the raw bytes instead: escape sequences survive intact,
    each long run becomes `<N:head…>` — its length plus its first few
    characters. Emitted only during the token flow, so it costs nothing
    on a normal run.
    """

    def _mask(match: "re.Match[str]") -> str:
        run = match.group(0)
        return f"<{len(run)}:{run[:_SHAPE_HEAD]}…>"

    return repr(_SHAPE_RUN_RE.sub(_mask, raw))


def _redact_for_log(line: str) -> str:
    def _mask(match: "re.Match[str]") -> str:
        return f"<{len(match.group(0))} chars redacted>"

    return _LOOKS_LIKE_A_TOKEN_RE.sub(_mask, line)


class _TerminalScreen:
    """A minimal, persistent model of what a real terminal screen would
    show, fed the same character-at-a-time stream `TerminalLoginWorker.
    work`'s read loop already reads (docs/facts/acp-sdk.md §26, rewritten).

    An Ink-based build repaints the screen as a diff between frames: each
    redraw sends only the runs that changed, moving the cursor between
    them rather than reprinting the whole line every time. `_strip_ansi`
    on one flush's own buffer — a plain concatenation of whatever text
    arrived between two `\\r`/`\\n`s — throws that structure away: a
    character an EARLIER frame already drew, which this frame's diff
    doesn't touch, is simply invisible to anything reading only the
    current buffer. That is what actually ate one character of a real
    OAuth token: the real capture's `\\x1b[10G` was a complete, well-formed
    move from column 9 to column 10 — over a cell an earlier frame had
    already painted with the token's own "o". No incomplete escape was
    ever involved; §26's first read of the same measurement got that
    part wrong (see the corrected section for the full account).

    This model does NOT replace `_strip_ansi` or the buffer-based line
    detection the rest of the module already relies on for markers, URLs,
    the spinner filter, and the force-flush — see the module docstring's
    own warning against rewriting that. It is fed the same characters IN
    PARALLEL, keeps every cell it has ever been told to write until
    something overwrites or erases it, and `TerminalLoginWorker.work` only
    ever asks it for one thing: the text of the row the cursor was on at
    the moment a line was considered complete. Because it is never reset
    between flushes, a character painted several frames ago and never
    touched since is still exactly where it was.

    Deliberately minimal — only what a real Ink-based build has actually
    been measured to emit (§18, §20, §26): printable characters, `\\r`,
    `\\n`, relative cursor moves (`\\x1b[NA/B/C/D`), absolute column
    (`\\x1b[NG`), absolute position (`\\x1b[N;MH`/`f`), line/screen erase
    (`\\x1b[NK`/`\\x1b[NJ`), and OSC sequences (recognised and consumed
    without moving the cursor or writing a cell, so an OSC-8 hyperlink
    wrapping a URL doesn't corrupt whatever row it sits on — only the
    DISPLAY text outside the OSC body is ever written, exactly as a real
    terminal renders one). Any other CSI is recognised as a complete
    escape sequence and consumed — its bytes never reach a cell — but is
    NOT interpreted, so it never moves the cursor either: an unhandled
    sequence is a no-op here, never a guess.
    """

    #: 0x40-0x7E — the CSI final-byte range as ECMA-48 defines it. Whether
    #: a byte in this range is an ASSIGNED final doesn't matter here the
    #: way it briefly mattered for `_ANSI_RE`: this model only needs to
    #: know where one escape sequence ENDS, not what a legacy final byte
    #: historically meant, and an unassigned final still terminates a real
    #: CSI on any real terminal.
    _CSI_FINAL_RANGE = frozenset(chr(c) for c in range(0x40, 0x7F))

    def __init__(self) -> None:
        self._rows: list[list[str]] = [[]]
        self._row = 0
        self._col = 0
        #: "ground" (plain text) | "esc" (saw a bare ESC) | "csi" (inside
        #: `\x1b[...`) | "osc" (inside `\x1b]...`) | "osc_esc" (inside an
        #: OSC body, just saw ESC — deciding whether it's the `\x1b\\`
        #: string terminator or more OSC body).
        self._state = "ground"
        self._csi_buf = ""

    @property
    def cursor_row(self) -> int:
        return self._row

    def row_text(self, row: int) -> str:
        """Every cell of `row`, unwritten cells rendered as the space they
        visually are — a gap left by a cursor jump IS a space on a real
        terminal. Trailing space trimmed (padding, not content); leading
        space is left for the caller to strip, the same way
        `_token_value_in` already strips whatever line it's handed.
        """
        if not (0 <= row < len(self._rows)):
            return ""
        return "".join(self._rows[row]).rstrip(" ")

    def feed(self, ch: str) -> None:
        if self._state == "ground":
            self._feed_ground(ch)
        elif self._state == "esc":
            self._feed_esc(ch)
        elif self._state == "csi":
            self._feed_csi(ch)
        else:  # "osc" / "osc_esc"
            self._feed_osc(ch)

    def _feed_ground(self, ch: str) -> None:
        if ch == "\x1b":
            self._state = "esc"
        elif ch == "\r":
            self._col = 0
        elif ch == "\n":
            self._row += 1
            self._ensure_row(self._row)
        else:
            self._write(ch)

    def _feed_esc(self, ch: str) -> None:
        if ch == "[":
            self._state = "csi"
            self._csi_buf = ""
        elif ch == "]":
            self._state = "osc"
        else:
            # `\x1b7`/`\x1b8` (save/restore cursor, §20) and anything else
            # single-byte — none of them are tracked here, the same as
            # `_ANSI_RE` only ever recognised them well enough to strip,
            # never to interpret.
            self._state = "ground"

    def _feed_csi(self, ch: str) -> None:
        if ch in self._CSI_FINAL_RANGE:
            self._apply_csi(self._csi_buf, ch)
            self._state = "ground"
            self._csi_buf = ""
        else:
            self._csi_buf += ch
            if len(self._csi_buf) > 64:  # a runaway sequence, not a real one
                self._state = "ground"
                self._csi_buf = ""

    def _feed_osc(self, ch: str) -> None:
        if self._state == "osc_esc":
            self._state = "ground" if ch == "\\" else "osc"
            return
        if ch == "\x07":
            self._state = "ground"
        elif ch == "\x1b":
            self._state = "osc_esc"
        # else: still inside the OSC body — ignored, never written to a
        # cell and never moves the cursor.

    def _apply_csi(self, params: str, final: str) -> None:
        nums = [int(p) for p in re.findall(r"\d+", params)]

        def n(default: int = 1, index: int = 0) -> int:
            # A present-but-zero parameter means the same as absent for
            # every one of these (ECMA-48) — `\x1b[0C` moves forward one
            # column, same as `\x1b[C`.
            return nums[index] if index < len(nums) and nums[index] else default

        if final == "A":
            self._row = max(0, self._row - n())
        elif final == "B":
            self._row += n()
            self._ensure_row(self._row)
        elif final == "C":
            self._col += n()
        elif final == "D":
            self._col = max(0, self._col - n())
        elif final == "G":
            self._col = max(0, n() - 1)
        elif final in ("H", "f"):
            self._row = max(0, n(1, 0) - 1)
            self._col = max(0, n(1, 1) - 1)
            self._ensure_row(self._row)
        elif final == "K":
            self._erase_line(n(0))
        elif final == "J":
            self._erase_screen(n(0))
        # Every other final byte: recognised as a complete CSI and
        # consumed, but not interpreted — no cursor movement, no cell
        # write. See the class docstring.

    def _erase_line(self, mode: int) -> None:
        self._ensure_row(self._row)
        row = self._rows[self._row]
        if mode == 0:  # cursor to end of line
            del row[self._col :]
        elif mode == 1:  # start of line to cursor
            for i in range(min(self._col + 1, len(row))):
                row[i] = " "
        elif mode == 2:  # whole line
            self._rows[self._row] = []

    def _erase_screen(self, mode: int) -> None:
        if mode == 2:  # whole screen
            self._rows = [[] for _ in self._rows]
        elif mode == 0:  # cursor to end of screen
            self._erase_line(0)
            for r in range(self._row + 1, len(self._rows)):
                self._rows[r] = []
        elif mode == 1:  # start of screen to cursor
            for r in range(self._row):
                self._rows[r] = []
            self._erase_line(1)

    def _ensure_row(self, row: int) -> None:
        while len(self._rows) <= row:
            self._rows.append([])

    def _ensure_col(self, row: int, col: int) -> None:
        r = self._rows[row]
        while len(r) <= col:
            r.append(" ")

    def _write(self, ch: str) -> None:
        self._ensure_row(self._row)
        self._ensure_col(self._row, self._col)
        self._rows[self._row][self._col] = ch
        self._col += 1


#: How wide the pty claims to be. `pty.openpty()` hands out a terminal
#: with NO size set, and a build that lays its output out with Ink asks
#: the tty for its width and hard-wraps to it — 80 columns, the fallback
#: for an unset size.
#:
#: That silently corrupted the one thing this whole module exists to
#: capture. A real Linux run (mayfx02, 2026-08-08) printed the minted
#: token as TWO lines — 79 characters, then 29 — because a leading space
#: plus 79 characters is exactly 80. The panel captured the first line,
#: stored it, reported "Signed in.", and the agent's first prompt came
#: back `401 OAuth access token is invalid`: a token 79 characters long
#: where the real one is 108. Nothing in the output says a line was
#: continued, so no parser downstream can tell a wrapped token from a
#: complete one — the only honest fix is to stop the wrapping happening.
#:
#: Wide enough for several times the longest thing measured here (the
#: 108-character token; the ~250-character OAuth URL, which arrives
#: wrapped as an OSC-8 hyperlink at 448 characters, §20), and still well
#: under `_FORCE_FLUSH_CHARS` so a full-width line can never be mistaken
#: for a run that forgot to end.
_PTY_COLUMNS = 1000
#: Rows matter far less — nothing here is laid out vertically — but a
#: terminal claiming zero rows is a strange thing to hand a program that
#: may reasonably check.
_PTY_ROWS = 50


def _set_pty_size(fd: int, *, columns: int = _PTY_COLUMNS, rows: int = _PTY_ROWS) -> None:
    """Tell the pty how wide it is, so the child stops wrapping output.

    Set on the SLAVE fd — that's the side the child sees as its
    controlling terminal, and the side whose size `process.stdout.
    columns` reports. Failure is suppressed for the same reason the
    `termios` block below suppresses it: an fd that won't take the ioctl
    is not worth losing a sign-in over, and the old (wrapping) behaviour
    is what we'd fall back to anyway.
    """
    with contextlib.suppress(OSError):
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, columns, 0, 0))


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
    #: The capture produced something the API refused. Nothing was
    #: stored — see `_verify_and_emit_token` for why that matters more
    #: than it sounds.
    token_rejected = Signal(str)
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
        #: Set the moment `_OAUTH_TOKEN_LABEL` is seen: the very next
        #: token-shaped line is the minted secret itself.
        self._awaiting_token_value = False
        #: Held between capture and verification — see
        #: `_verify_and_emit_token`.
        self._captured_token = ""

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

        Unlike `client.py::do_start`, "the OS environment" here really is
        `os.environ` in full — not the ACP SDK's six-variable minimum —
        because this same method also builds the environment for `uvx
        --refresh ...` (`self_update.py`) and for an in-process HTTP POST
        (`bugreport.post_report`), both of which plausibly want more than
        that (`HOME`/cache dirs for `uv`, whatever else a real machine's
        network stack already has configured). Narrowing that base is a
        second project on its own, not a safe drive-by here — self-update
        already broke twice in one week from smaller changes nearby
        (`3e38cea`).

        `SHADOWING_VARS` (`PYTHONPATH`/`PYTHONHOME`/`PYTHONSTARTUP`) is
        stripped from the RESULT, as the very last step below — not from
        `os.environ` up front. Houdini's own package json writes
        `PYTHONPATH` into `os.environ` for the PANEL's own benefit
        (`houdini_package.py`), same value this method's own caller never
        intends for a login CLI (or `uvx`, or nothing at all, for the
        in-process HTTP case) — but `shellenv.merged` widens whatever base
        it's given with the artist's own login shell afterwards
        (`capture()` only filters names starting `HAP_`, nothing else),
        and on a real VFX machine a studio pipeline's `PYTHONPATH` in
        `.zshenv`/`.zprofile` is routine, not exotic. Stripping the base
        alone would have let exactly that back in through `capture()` —
        caught in review, before it shipped, by reproducing it: a fake
        profile exporting its own `PYTHONPATH` came back out of `merged()`
        untouched even with Houdini's leaked one gone. One caller already
        knew this the hard way and stripped AFTER calling this
        (`self_update.py`, with the report of a stale panel importing off
        a shadowed `PYTHONPATH` to prove it necessary) — another one
        currently in this codebase does not (`bugreport_worker.py`,
        `SimpleNamespace(env={})` straight into `post_report`). Doing it
        HERE, once, on the finished result, makes that forgettable
        per-caller step structurally unnecessary instead of merely
        unlikely to be missed — the exact shape of bug the fx MCP server
        just went a week silently broken from (`shellenv.py`'s own fix),
        except this is the one spot that would hit it for any
        terminal-login command that happens to be a Python program, should
        one ever join the roughly forty agents in the ACP registry that
        aren't today.
        """
        from .. import proxy as proxy_module
        from .. import settings as settings_module

        current_settings = settings_module.load()
        env = shellenv.merged(dict(os.environ), proxy_module.child_env(current_settings))
        env.update(terminal_auth.env)
        for name in mcp_runtime.SHADOWING_VARS:
            env.pop(name, None)
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
            _set_pty_size(slave_fd)
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
            # Same width as the POSIX pty, passed explicitly rather than
            # left to that module's own default: wrapping corrupts a
            # token identically on either platform, and two independent
            # defaults drifting apart is exactly how one platform ends up
            # quietly capturing 79 characters of a 108-character secret.
            conpty_process = _conpty_windows.spawn(
                command,
                list(args),
                env=env,
                cwd=self._cwd or None,
                columns=_PTY_COLUMNS,
                rows=_PTY_ROWS,
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
                # The login's output belongs in the panel, which is already
                # reading it off these pipes. Without this, Windows also
                # opens a console window for it — an empty black one, since
                # everything the child prints has been redirected here
                # (`childproc.py`).
                **childproc.hidden_window_kwargs(),
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
        # Fed every character the read loop sees, never reset — see
        # `_TerminalScreen`'s own docstring (§26, rewritten) for why a
        # per-flush buffer alone corrupted a real token: a character an
        # earlier redraw frame already painted survives here even after
        # the buffer that carried it has long since been flushed away.
        screen = _TerminalScreen()
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
                # Captured BEFORE feeding this char to the screen model:
                # if this char is the `\n`/`\r` that triggers a flush
                # below, feeding it first would already have moved the
                # cursor off the row the buffered text was actually drawn
                # on. Harmless for the non-flush branch too — a plain
                # printable char never changes the row on its own.
                row_before = screen.cursor_row
                screen.feed(char)
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
                raw = buffer
                line = _strip_ansi(buffer)
                # The screen model's own view of the row this buffer was
                # drawn on — used ONLY for the token value itself (see
                # `_token_value_in`); every other decision below still
                # runs off `line`, unchanged.
                screen_line = screen.row_text(row_before)
                buffer = ""
                if self._token_flow_active:
                    # Raw, pre-strip structure — the one thing missing
                    # every time this has gone wrong. See `_shape_for_log`.
                    _log.info("terminal login raw shape: %s", _shape_for_log(raw))
                if not line.strip() or _is_spinner_noise(line):
                    continue
                if not self._token_flow_active and _marker_in(_OAUTH_TOKEN_LABEL, line):
                    # From here on, `_emit_line` redacts too — checked
                    # BEFORE emitting THIS line, in case a future build
                    # ever puts the label and the token on the same line.
                    self._token_flow_active = True
                    self._awaiting_token_value = True
                token = self._token_value_in(line, screen_line)
                if token:
                    self._awaiting_token_value = False
                    # Held, not emitted: verified once the child has
                    # finished, so a slow network can't stall the read
                    # loop while the build is still printing. See
                    # `_verify_and_emit_token`.
                    self._captured_token = token
                    # The length is not a secret, and it is the single
                    # cheapest tripwire there is: a token 79 characters
                    # long (§25) or one character short is obvious here
                    # and invisible everywhere else until the agent's
                    # first prompt fails.
                    # The prefix is a published constant, not entropy, and
                    # it is the fastest possible read on whether capture
                    # went wrong: `sk-ant-oat01` is whole, `sk-ant-at01-`
                    # is the §26 corruption, and anything else is new.
                    _log.info(
                        "terminal login: OAuth token captured (%s), %d characters, starts %r",
                        _OAUTH_TOKEN_ENV_VAR,
                        len(token),
                        token[:12],
                    )
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
            self._verify_and_emit_token()
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

    def _verify_and_emit_token(self) -> None:
        """Hand over the captured token — but only if it actually works.

        Runs on the worker thread, after the child has finished printing,
        so a slow check never stalls the read loop and never touches the
        UI thread.

        The rule this enforces comes straight from the owner's report:
        signing in again is the obvious thing to try when something looks
        wrong, and until now doing so OVERWROTE a working credential with
        whatever capture produced. Three different parsing faults (§21,
        §25, §26) each shipped a plausible-looking, unusable token and
        each was announced as a successful sign-in. A rejected token is
        therefore not stored at all: whatever was there before it is
        worth more.

        `UNKNOWN` — offline, proxy down, timeout — stores the token, the
        same as before this check existed. Being unable to ask is not an
        answer, and an artist with no connection still deserves to keep
        the credential they just minted.
        """
        token = self._captured_token
        if not token:
            return
        self._captured_token = ""

        status = token_check.verify(token)
        _log.info("terminal login: token check: %s", status)
        if status == token_check.REJECTED:
            self.token_rejected.emit(
                "That sign-in produced a token the API rejected, so it was not saved — "
                "any token already stored is untouched. Please try signing in again."
            )
            return
        self.token_captured.emit(_OAUTH_TOKEN_ENV_VAR, token)

    def _token_value_in(self, line: str, screen_line: str) -> str:
        """The minted OAuth token if THIS line carries it, else `""`.

        Two shapes, in the order a real run produces them. The bare line
        right after the label is the one that actually fires on today's
        build; the `VAR=value` form has never been observed carrying a
        real value and is kept only so a future build that starts doing
        it isn't missed.

        `screen_line` — the `_TerminalScreen` row this buffer was drawn
        on, not `line` (`_strip_ansi(buffer)`) — is what the bare-line
        branch reads. A build that redraws its own token line across
        several frames (docs/facts/acp-sdk.md §26, rewritten) can leave a
        character painted by an earlier frame that this flush's own
        buffer never re-sent; `line` cannot see it, `screen_line` can,
        because the screen model is never reset between flushes. The
        `VAR=value` branch still reads `line` — that shape has never been
        observed carrying a real value at all (see below), so it isn't
        worth the same treatment.

        Both are shape-checked. Without that check the instruction line
        `export CLAUDE_CODE_OAUTH_TOKEN=<token>` hands over the literal
        string `<token>`, which stores cleanly, reports "Signed in." and
        then fails on the artist's first prompt with nothing pointing
        back here — strictly worse than capturing nothing at all.
        """
        if self._awaiting_token_value:
            candidate = screen_line.strip()
            if _TOKEN_VALUE_RE.fullmatch(candidate):
                return candidate
        match = _OAUTH_TOKEN_RE.search(line)
        if match and _TOKEN_VALUE_RE.fullmatch(match.group(1)):
            return match.group(1)
        return ""

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
        body = text.rstrip("\r\n")
        # The content itself is never logged — it's what the artist just
        # pasted from their browser, closer to a credential than a device
        # code is. Only the fact that something was sent.
        _log.info("terminal login: artist input submitted (%d chars)", len(text))
        if self._use_pty:
            if self._pty_master_fd is None:
                return
            with contextlib.suppress(OSError):
                os.write(self._pty_master_fd, (body + _ENTER_ON_A_TERMINAL).encode("utf-8"))
            return
        if self._use_conpty:
            if self._conpty_process is None:
                return
            with contextlib.suppress(Exception):
                self._conpty_process.write((body + _ENTER_ON_A_TERMINAL).encode("utf-8"))
            return
        if process.stdin is None:
            return
        with contextlib.suppress(OSError, ValueError):
            # Plain pipes have no line discipline to translate anything:
            # a reader there is waiting for `\n` and would never see a
            # bare `\r`. See `_ENTER_ON_A_TERMINAL`.
            process.stdin.write(body + "\n")
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

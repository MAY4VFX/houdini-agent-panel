"""`TerminalLoginWorker`: spawns Kimi's `kimi login`-shaped process off the
main thread, reads its output, and turns a `Verification URL:` line into a
real link (docs/facts/acp-sdk.md §14) — or degrades quietly if the line
never appears, since the format isn't guaranteed stable (n=1 sample).
"""

from __future__ import annotations

import os
import sys

import pytest

from houdini_agent_panel import orphans
from houdini_agent_panel.client import TerminalAuth
from houdini_agent_panel.ui.terminal_login import TerminalLoginWorker


def _wait_until(app, condition, *, timeout_ms: int = 5000) -> None:
    from PySide6 import QtTest

    elapsed = 0
    step = 20
    while not condition() and elapsed < timeout_ms:
        app.processEvents()
        QtTest.QTest.qWait(step)
        elapsed += step
    assert condition(), "condition did not become true in time"


#: A stand-in for `kimi login`: prints the exact shape measured for real
#: (docs/facts/acp-sdk.md §14), then exits — real kimi polls forever
#: instead, but a worker under test needs a script that finishes.
_KIMI_LIKE_SCRIPT = (
    "import sys, time\n"
    "print('Please visit the following URL to finish authorization.')\n"
    "print('Verification URL: https://www.kimi.com/code/authorize_device?user_code=14OI-AX7F')\n"
    "sys.stdout.flush()\n"
)

#: Never exits on its own — for testing `stop()`.
_LONG_RUNNING_SCRIPT = "import time\nwhile True:\n    time.sleep(0.05)\n"

#: A stand-in for Claude's `setup-token` (docs/facts/acp-sdk.md §14): a
#: bare URL (no "Verification URL:" prefix, no separate code — the URL IS
#: the whole artefact), then an input prompt it actually blocks on.
_CLAUDE_LIKE_SCRIPT = (
    "import sys\n"
    "print('Opening browser to sign in...')\n"
    "print('https://claude.com/cai/oauth/authorize?code=true&client_id=abc')\n"
    "print('Paste code here if prompted > ', end='')\n"
    "sys.stdout.flush()\n"
    "code = sys.stdin.readline().strip()\n"
    "print('got:' + code)\n"
)


def test_url_and_code_are_parsed_from_a_real_shaped_line(qapp, tmp_path):
    ta = TerminalAuth(command=sys.executable, args=["-c", _KIMI_LIKE_SCRIPT], env={})
    worker = TerminalLoginWorker("kimi", ta, cwd=str(tmp_path))

    found: list[tuple[str, str]] = []
    lines: list[str] = []
    worker.url_found.connect(lambda url, code: found.append((url, code)))
    worker.line_received.connect(lines.append)
    worker.start()

    _wait_until(qapp, lambda: bool(found))

    url, code = found[0]
    assert url == "https://www.kimi.com/code/authorize_device?user_code=14OI-AX7F"
    assert code == "14OI-AX7F"
    assert any("Please visit" in line for line in lines)

    worker.wait(3000)


def test_a_line_that_does_not_match_the_pattern_is_still_seen_raw(qapp, tmp_path):
    """Not established (docs/facts/acp-sdk.md §14) whether this exact
    format is stable across kimi versions — a script that never prints a
    recognisable line must not hide its output entirely."""
    script = "print('some other login flow, unrecognised')\n"
    ta = TerminalAuth(command=sys.executable, args=["-c", script], env={})
    worker = TerminalLoginWorker("kimi", ta, cwd=str(tmp_path))

    lines: list[str] = []
    found: list[tuple[str, str]] = []
    worker.line_received.connect(lines.append)
    worker.url_found.connect(lambda url, code: found.append((url, code)))
    worker.exited.connect(lambda _code: None)
    worker.start()

    _wait_until(qapp, lambda: bool(lines))
    worker.wait(3000)

    assert any("unrecognised" in line for line in lines)
    assert found == []


def test_the_process_is_registered_for_orphans_and_deregistered_on_exit(qapp, tmp_path):
    ta = TerminalAuth(command=sys.executable, args=["-c", _KIMI_LIKE_SCRIPT], env={})
    worker = TerminalLoginWorker("kimi", ta, cwd=str(tmp_path))

    exited: list[int] = []
    worker.exited.connect(exited.append)
    worker.start()

    _wait_until(qapp, lambda: bool(exited))
    worker.wait(3000)

    # Gone the moment the process stopped on its own — nothing left for a
    # crash-recovery sweep to find.
    assert orphans._load() == {}


def test_stop_terminates_a_process_that_never_exits_on_its_own(qapp, tmp_path):
    """Real `kimi login` polls indefinitely (docs/facts/acp-sdk.md §14) —
    this is what leaving the sign-in screen, cancelling, or closing the
    panel actually has to be able to do."""
    ta = TerminalAuth(command=sys.executable, args=["-c", _LONG_RUNNING_SCRIPT], env={})
    worker = TerminalLoginWorker("kimi", ta, cwd=str(tmp_path))

    exited: list[int] = []
    worker.exited.connect(exited.append)
    worker.start()

    _wait_until(qapp, lambda: worker._process is not None)
    worker.stop()

    _wait_until(qapp, lambda: bool(exited), timeout_ms=5000)
    worker.wait(3000)
    assert orphans._load() == {}


def test_a_bare_url_with_no_prefix_is_recognised_too(qapp, tmp_path):
    """Claude's `setup-token` prints a naked URL, not kimi's "Verification
    URL:"-prefixed one (docs/facts/acp-sdk.md §14) — both have to work."""
    ta = TerminalAuth(command=sys.executable, args=["-c", _CLAUDE_LIKE_SCRIPT], env={})
    worker = TerminalLoginWorker("claude-acp", ta, cwd=str(tmp_path))

    found: list[tuple[str, str]] = []
    worker.url_found.connect(lambda url, code: found.append((url, code)))
    worker.send_line("IGNORED")  # too early — process hasn't started yet, must be a no-op
    worker.start()

    _wait_until(qapp, lambda: bool(found))
    assert found == [("https://claude.com/cai/oauth/authorize?code=true&client_id=abc", "")]

    worker.stop()
    worker.wait(3000)


def test_input_prompt_is_detected_and_send_line_answers_it(qapp, tmp_path):
    """Claude's `setup-token` blocks at "Paste code here if prompted >"
    for exactly one line back (docs/facts/acp-sdk.md §14) — `input_
    requested` is how `AgentPanel` knows to show the field at all, and
    `send_line` is the only way to answer it; no terminal emulator."""
    ta = TerminalAuth(command=sys.executable, args=["-c", _CLAUDE_LIKE_SCRIPT], env={})
    worker = TerminalLoginWorker("claude-acp", ta, cwd=str(tmp_path))

    awaiting: list[bool] = []
    lines: list[str] = []
    worker.input_requested.connect(lambda: awaiting.append(True))
    worker.line_received.connect(lines.append)
    worker.start()

    _wait_until(qapp, lambda: bool(awaiting))
    worker.send_line("MY-CODE-123")

    _wait_until(qapp, lambda: any("got:MY-CODE-123" in line for line in lines))
    worker.wait(3000)


#: A newer `claude-code` build found on the owner's own Linux box
#: (docs/facts/acp-sdk.md §18), extracted directly from the installed
#: binary: a SECOND prompt string, "Or paste the redirect URL here: ",
#: not the one this module was originally written against — the exact
#: mismatch that left the code field never appearing while a browser tab
#: sat on "you're all set up" and the process waited on stdin forever.
_CLAUDE_NEW_BUILD_SCRIPT = (
    "import sys\n"
    "print('https://claude.com/cai/oauth/authorize?code=true&client_id=abc')\n"
    "sys.stdout.write('Or paste the redirect URL here: ')\n"
    "sys.stdout.flush()\n"
    "code = sys.stdin.readline().strip()\n"
    "print('got:' + code)\n"
)

#: A build redrawing a status line with carriage returns instead of a
#: plain newline (measured as at least possible on the same build, §18) —
#: used to leave everything printed this way sitting invisibly in
#: `buffer` until (if ever) a genuine `\n` arrived.
_CARRIAGE_RETURN_SCRIPT = (
    "import sys\n"
    "sys.stdout.write('connecting...\\r')\n"
    "sys.stdout.flush()\n"
    "sys.stdout.write('still connecting...   \\r')\n"
    "sys.stdout.flush()\n"
    "sys.stdout.write('Paste code here if prompted > ')\n"
    "sys.stdout.flush()\n"
    "code = sys.stdin.readline().strip()\n"
    "print('got:' + code)\n"
)

#: Colour wrapped around the exact text being matched — a build willing to
#: reformat its prompt for a non-tty stdin (§18) is equally free to
#: colour it.
_ANSI_WRAPPED_SCRIPT = (
    "import sys\n"
    "sys.stdout.write('\\x1b[33mPaste code here if prompted > \\x1b[0m')\n"
    "sys.stdout.flush()\n"
    "code = sys.stdin.readline().strip()\n"
    "print('got:' + code)\n"
)

#: A "token" long enough to trip `_redact_for_log` (24+ chars), standing
#: in for a PKCE code_challenge or similar — never actually a real secret
#: in a test, but shaped like one on purpose.
_TOKEN_LOOKING_STRING = "aZ9-x7Qw2vN8mK3pL6rT1sY4"


def test_the_second_prompt_shape_from_a_newer_build_is_also_detected(qapp, tmp_path):
    """docs/facts/acp-sdk.md §18: reproduces the live failure directly —
    without this marker, `input_requested` never fires and the artist is
    left on a field that never appears, forever, exactly as reported."""
    ta = TerminalAuth(command=sys.executable, args=["-c", _CLAUDE_NEW_BUILD_SCRIPT], env={})
    worker = TerminalLoginWorker("claude-acp", ta, cwd=str(tmp_path))

    awaiting: list[bool] = []
    lines: list[str] = []
    worker.input_requested.connect(lambda: awaiting.append(True))
    worker.line_received.connect(lines.append)
    worker.start()

    _wait_until(qapp, lambda: bool(awaiting))
    worker.send_line("MY-CODE-456")

    _wait_until(qapp, lambda: any("got:MY-CODE-456" in line for line in lines))
    worker.wait(3000)


def test_a_carriage_return_redrawn_status_line_is_not_invisible(qapp, tmp_path):
    """A `\\r`-redrawn line used to sit in `buffer` forever, seen by
    nobody, unless a real `\\n` eventually arrived — the "must not wait
    forever with no way forward" half of the same report: raw output has
    to reach the artist even when nothing recognises it."""
    ta = TerminalAuth(command=sys.executable, args=["-c", _CARRIAGE_RETURN_SCRIPT], env={})
    worker = TerminalLoginWorker("claude-acp", ta, cwd=str(tmp_path))

    lines: list[str] = []
    awaiting: list[bool] = []
    worker.line_received.connect(lines.append)
    worker.input_requested.connect(lambda: awaiting.append(True))
    worker.start()

    _wait_until(qapp, lambda: bool(awaiting))
    assert any("connecting" in line for line in lines), (
        "a \\r-redrawn line must still reach line_received, not stay buffered"
    )
    worker.send_line("X")
    worker.wait(3000)


def test_a_colour_coded_prompt_is_still_recognised(qapp, tmp_path):
    """ANSI codes are stripped before matching — a build free to reformat
    its prompt for a non-tty stdin (§18) is equally free to colour it, and
    the literal substring check must not be defeated by an escape
    sequence sitting in the middle of it."""
    ta = TerminalAuth(command=sys.executable, args=["-c", _ANSI_WRAPPED_SCRIPT], env={})
    worker = TerminalLoginWorker("claude-acp", ta, cwd=str(tmp_path))

    awaiting: list[bool] = []
    worker.input_requested.connect(lambda: awaiting.append(True))
    worker.start()

    _wait_until(qapp, lambda: bool(awaiting))
    worker.send_line("X")
    worker.wait(3000)


def test_the_spawned_command_is_logged(qapp, tmp_path, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="houdini_agent_panel.ui.terminal_login")
    ta = TerminalAuth(command=sys.executable, args=["-c", _KIMI_LIKE_SCRIPT], env={})
    worker = TerminalLoginWorker("kimi", ta, cwd=str(tmp_path))
    exited: list[int] = []
    worker.exited.connect(exited.append)
    worker.start()

    _wait_until(qapp, lambda: bool(exited))
    worker.wait(3000)

    messages = [r.message for r in caplog.records]
    assert any("spawning" in m and sys.executable in m for m in messages), (
        "the report this fixes: 'not a single line about the terminal login "
        "— not the command, not its output, not its exit'"
    )
    assert any("Verification URL" in m for m in messages)
    assert any("exited, code=" in m for m in messages)


def test_a_token_looking_line_is_redacted_in_the_log_but_not_in_the_signal(qapp, tmp_path, caplog):
    """A device code is not a secret and stays readable in the log; a
    long, opaque, token-shaped run is masked there — but the LIVE signal
    (what the panel actually shows the artist) is untouched either way,
    since the artist needs the real thing to use it."""
    import logging

    caplog.set_level(logging.INFO, logger="houdini_agent_panel.ui.terminal_login")
    script = f"print('access_token={_TOKEN_LOOKING_STRING}')\n"
    ta = TerminalAuth(command=sys.executable, args=["-c", script], env={})
    worker = TerminalLoginWorker("claude-acp", ta, cwd=str(tmp_path))
    lines: list[str] = []
    exited: list[int] = []
    worker.line_received.connect(lines.append)
    worker.exited.connect(exited.append)
    worker.start()

    _wait_until(qapp, lambda: bool(exited))
    worker.wait(3000)

    assert any(_TOKEN_LOOKING_STRING in line for line in lines), (
        "the artist-facing signal must carry the real content"
    )
    messages = [r.message for r in caplog.records]
    assert not any(_TOKEN_LOOKING_STRING in m for m in messages), (
        "a token-shaped run must never reach the on-disk log verbatim"
    )
    assert any("redacted" in m for m in messages)


def test_submitted_input_content_is_never_logged(qapp, tmp_path, caplog):
    """The artist's pasted code/URL is closer to a credential than a
    device code — only the fact that something was submitted belongs in
    the log, never the text itself."""
    import logging

    caplog.set_level(logging.INFO, logger="houdini_agent_panel.ui.terminal_login")
    ta = TerminalAuth(command=sys.executable, args=["-c", _CLAUDE_LIKE_SCRIPT], env={})
    worker = TerminalLoginWorker("claude-acp", ta, cwd=str(tmp_path))
    awaiting: list[bool] = []
    worker.input_requested.connect(lambda: awaiting.append(True))
    worker.start()

    _wait_until(qapp, lambda: bool(awaiting))
    secret_looking_code = "SECRET-PASTE-BACK-VALUE-999"
    worker.send_line(secret_looking_code)
    worker.wait(3000)

    messages = [r.message for r in caplog.records]
    assert not any(secret_looking_code in m for m in messages)
    assert any("input submitted" in m for m in messages)


def _no_shell(monkeypatch) -> None:
    """`build_env` widens the OS environment with the artist's login shell
    (`shellenv.merged`, same reason `client.py::do_start` needs it) — real
    for production, but a real `subprocess.run` in a test would be slow
    AND could pick up whatever proxy the test machine's OWN shell profile
    happens to export, exactly the non-determinism `test_shellenv.py`
    itself avoids by always stubbing this out."""
    from houdini_agent_panel import shellenv as shellenv_module

    monkeypatch.setattr(shellenv_module, "capture", lambda **_: {})


def test_build_env_includes_the_configured_studio_proxy(monkeypatch):
    """Reported for real: a login process spawned without the studio's
    proxy hangs trying to reach the network — on a machine where nothing
    gets through without one (why `proxy_url` exists in Settings at all),
    that reads exactly like the dead button issue #33 already fixed once.
    """
    from houdini_agent_panel import settings as settings_module

    _no_shell(monkeypatch)
    current = settings_module.load()
    current.proxy_url = "http://proxy.studio.local:8080"
    settings_module.save(current)

    ta = TerminalAuth(command=sys.executable, args=[], env={})
    env = TerminalLoginWorker.build_env(ta)

    assert env["HTTPS_PROXY"] == "http://proxy.studio.local:8080"
    assert env["HTTP_PROXY"] == "http://proxy.studio.local:8080"


def test_build_env_lets_the_methods_own_env_win_over_the_studio_proxy(monkeypatch):
    """Precedence matches `runtime.py::_with_proxy`'s own docstring: the
    studio proxy underneath, the method's own env (the most specific
    thing here) on top."""
    from houdini_agent_panel import settings as settings_module

    _no_shell(monkeypatch)
    current = settings_module.load()
    current.proxy_url = "http://proxy.studio.local:8080"
    settings_module.save(current)

    ta = TerminalAuth(command=sys.executable, args=[], env={"HTTPS_PROXY": "http://method-specific:9"})
    env = TerminalLoginWorker.build_env(ta)

    assert env["HTTPS_PROXY"] == "http://method-specific:9"


def test_build_env_adds_nothing_when_no_proxy_is_configured(monkeypatch):
    """Empty in, empty out — same contract `proxy.child_env` itself
    documents: an unnecessary `HTTPS_PROXY=""` is worse than none at all
    for a CLI that treats a present-but-empty variable as "use a proxy at
    this address", i.e. nowhere."""
    from houdini_agent_panel import settings as settings_module

    _no_shell(monkeypatch)
    from houdini_agent_panel.proxy import PROXY_VARS

    for name in PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
    current = settings_module.load()
    assert current.proxy_url == ""
    settings_module.save(current)

    ta = TerminalAuth(command=sys.executable, args=[], env={})
    env = TerminalLoginWorker.build_env(ta)

    assert "HTTPS_PROXY" not in env


# --- resolve_command ---------------------------------------------------
#
# `_builtin_terminal_auth_method` can't decide synchronously whether a
# better command than its npx placeholder exists — confirming a candidate
# actually runs took ~1.7s measured on a real Mac, far too slow for the
# main thread. `resolve_command` is the escape hatch: called once, at the
# START of `work()` — already off the main thread — to get the REAL
# `(command, args)` before anything is spawned.


def test_resolve_command_overrides_the_placeholder(qapp, tmp_path):
    placeholder = TerminalAuth(command="npx", args=["--yes", "unused-placeholder"], env={})
    resolved_script = "import sys\nprint('resolved and running')\nsys.exit(0)\n"

    worker = TerminalLoginWorker(
        "claude-acp",
        placeholder,
        cwd=str(tmp_path),
        resolve_command=lambda: (sys.executable, ["-c", resolved_script]),
    )
    lines: list[str] = []
    resolved: list[tuple[str, list]] = []
    worker.line_received.connect(lines.append)
    worker.command_resolved.connect(lambda cmd, args: resolved.append((cmd, args)))
    worker.start()

    _wait_until(qapp, lambda: bool(lines))

    assert any("resolved and running" in line for line in lines)
    assert resolved == [(sys.executable, ["-c", resolved_script])]
    worker.wait(3000)


def test_resolve_command_returning_none_keeps_the_placeholder(qapp, tmp_path):
    """`None` means nothing better was found (or nothing that actually
    ran) — the npx placeholder still has to run, not silently do
    nothing."""
    script = "import sys\nprint('placeholder ran')\nsys.exit(0)\n"
    placeholder = TerminalAuth(command=sys.executable, args=["-c", script], env={})

    worker = TerminalLoginWorker(
        "claude-acp", placeholder, cwd=str(tmp_path), resolve_command=lambda: None
    )
    lines: list[str] = []
    resolved: list[tuple[str, list]] = []
    worker.line_received.connect(lines.append)
    worker.command_resolved.connect(lambda cmd, args: resolved.append((cmd, args)))
    worker.start()

    _wait_until(qapp, lambda: bool(lines))

    assert any("placeholder ran" in line for line in lines)
    assert resolved == []  # never fires when nothing actually changed
    worker.wait(3000)


def test_resolve_command_returning_the_same_command_does_not_re_signal(qapp, tmp_path):
    """`command_resolved` exists so the panel can correct a stale "run it
    yourself" fallback string — firing it for a resolver that agreed with
    the placeholder would be a no-op announcement, not new information."""
    script = "import sys\nprint('same command')\nsys.exit(0)\n"
    ta = TerminalAuth(command=sys.executable, args=["-c", script], env={})

    worker = TerminalLoginWorker(
        "claude-acp",
        ta,
        cwd=str(tmp_path),
        resolve_command=lambda: (sys.executable, ["-c", script]),
    )
    lines: list[str] = []
    resolved: list[tuple[str, list]] = []
    worker.line_received.connect(lines.append)
    worker.command_resolved.connect(lambda cmd, args: resolved.append((cmd, args)))
    worker.start()

    _wait_until(qapp, lambda: bool(lines))

    assert resolved == []
    worker.wait(3000)


def test_no_resolve_command_uses_terminal_auth_unchanged(qapp, tmp_path):
    """The ordinary case (Kimi, or Claude with `claude` already on PATH):
    no resolver given, `work()` never touches anything besides what
    `terminal_auth` already said."""
    script = "import sys\nprint('plain run')\nsys.exit(0)\n"
    ta = TerminalAuth(command=sys.executable, args=["-c", script], env={})

    worker = TerminalLoginWorker("kimi", ta, cwd=str(tmp_path))
    lines: list[str] = []
    worker.line_received.connect(lines.append)
    worker.start()

    _wait_until(qapp, lambda: bool(lines))

    assert any("plain run" in line for line in lines)
    worker.wait(3000)


# --- §20: a real pty run ------------------------------------------------
#
# The bundled `claude setup-token` binary prints nothing at all over plain
# pipes (docs/facts/acp-sdk.md §20) — it wants a real controlling terminal.
# `use_pty=True` is the fix; these tests cover the pieces that fix needed:
# `_marker_in` and `_is_spinner_noise` (unit-level, no process involved),
# `_PtyMasterReader` (the two POSIX quirks it exists to paper over), and an
# end-to-end run of `TerminalLoginWorker(use_pty=True)` itself, shaped after
# the real raw bytes captured on mayfx02, not synthesized from imagination.


def test_marker_in_ignores_whitespace_lost_to_cursor_positioning_escapes():
    """A real pty capture of "Paste code here if prompted >" arrived, after
    ANSI stripping, as the literal string "Pastecodehereifprompted>" — the
    build simulates spacing with cursor-move escapes, not space characters,
    and stripping the escapes throws the spacing away with them. The plain
    substring check this module used before §20 can never match that."""
    from houdini_agent_panel.ui.terminal_login import _marker_in

    assert _marker_in("paste code here", "Pastecodehereifprompted>")
    assert not _marker_in("paste code here", "totally unrelated output")


def test_marker_in_is_case_insensitive():
    from houdini_agent_panel.ui.terminal_login import _marker_in

    assert _marker_in("paste code here", "PASTE CODE HERE if prompted >")


def test_is_spinner_noise_matches_only_a_lone_symbol():
    """docs/facts/acp-sdk.md §20: a real capture showed this build cycling
    a single glyph per redraw frame (`✢ * ✶ ✻ ✽ ✻ ✶ * ✢ ·`) — filtered so
    it doesn't flash through the artist's own status field and the log as
    dozens of near-identical one-character lines. Must never fire on a
    real word, not even the shortest one."""
    from houdini_agent_panel.ui.terminal_login import _is_spinner_noise

    for glyph in ("✢", "*", "✶", "✻", "✽", "·"):
        assert _is_spinner_noise(glyph), f"{glyph!r} should be recognised as spinner noise"

    for word in ("a", "1", "A", "ok", ""):
        assert not _is_spinner_noise(word), f"{word!r} must not be dropped as spinner noise"


@pytest.mark.skipif(os.name == "nt", reason="pty is POSIX-only — see _PTY_AVAILABLE")
def test_pty_master_reader_treats_eio_as_eof(monkeypatch):
    """`os.read()` on a pty master fd raises `OSError` (EIO) once the slave
    side closes — a real, measured POSIX quirk (mayfx02, §20), NOT the
    clean `b""` EOF a pipe gives. Must be caught and treated as the same
    end-of-output signal, not left to crash the read loop."""
    from houdini_agent_panel.ui import terminal_login as terminal_login_mod

    def _raise(_fd, _n):
        raise OSError("Input/output error")

    monkeypatch.setattr(terminal_login_mod.os, "read", _raise)
    reader = terminal_login_mod._PtyMasterReader(99)
    assert reader.read(1) == ""


@pytest.mark.skipif(os.name == "nt", reason="pty is POSIX-only — see _PTY_AVAILABLE")
def test_pty_master_reader_reassembles_a_utf8_character_split_across_reads(monkeypatch):
    """Multi-byte UTF-8 characters (this build's own spinner glyphs are all
    multi-byte) can arrive split across two separate `os.read()` calls — a
    naive per-chunk `.decode()` raised `UnicodeDecodeError` against the
    real captured bytes until `_PtyMasterReader` used an incremental
    decoder instead."""
    from houdini_agent_panel.ui import terminal_login as terminal_login_mod

    encoded = "✢".encode("utf-8")
    assert len(encoded) == 3  # a real multi-byte character, not a coincidence
    chunks = [encoded[:1], encoded[1:]]

    def _fake_read(_fd, _n):
        return chunks.pop(0)

    monkeypatch.setattr(terminal_login_mod.os, "read", _fake_read)
    reader = terminal_login_mod._PtyMasterReader(99)
    assert reader.read(1) == "✢"


#: Shaped after the real raw bytes captured on mayfx02 (docs/facts/acp-
#: sdk.md §20), not a plain print — the three things that made plain-pipe
#: parsing insufficient, all in one script: a cursor-absolute-positioning
#: escape mid-prompt (spacing simulated by moving the cursor, not spaces),
#: an OSC-8 hyperlink wrapping the URL (BEL-terminated, with a truncated
#: repeated copy of the URL as "display text"), and a spinner glyph burst
#: before either of those. Blocks on stdin like the real build does.
_PTY_SHAPED_SCRIPT = (
    "import sys\n"
    "for glyph in '✢*✶':\n"
    "    sys.stdout.write(glyph + chr(13))\n"
    "    sys.stdout.flush()\n"
    "sys.stdout.write('Opening browser to sign in...\\n')\n"
    "url = 'https://claude.com/cai/oauth/authorize?state=abc123'\n"
    "sys.stdout.write('\\x1b]8;id=1;' + url + '\\x07' + url[:30] + '\\x1b]8;;\\x07\\n')\n"
    "sys.stdout.write('Paste\\x1b[10Gcode\\x1b[15Ghere\\x1b[20Gif\\x1b[25Gprompted\\x1b[35G> ')\n"
    "sys.stdout.flush()\n"
    "code = sys.stdin.readline().strip()\n"
    "sys.stdout.write('got:' + code + chr(10))\n"
)


@pytest.mark.skipif(os.name == "nt", reason="pty is POSIX-only — see _PTY_AVAILABLE")
def test_use_pty_true_parses_a_real_pty_shaped_run_end_to_end(qapp, tmp_path):
    """The integration case §20 exists for: a real child spawned with a
    real pty (not a mocked reader), through the actual `TerminalLoginWorker`
    codepath the panel uses (`use_pty=True`) — the URL must survive OSC-8
    wrapping, the input prompt must still be detected despite cursor-
    positioning spacing, and the spinner burst must not reach the artist
    as noise."""
    ta = TerminalAuth(command=sys.executable, args=["-c", _PTY_SHAPED_SCRIPT], env={})
    worker = TerminalLoginWorker("claude-acp", ta, cwd=str(tmp_path), use_pty=True)
    assert worker._use_pty is True  # confirms _PTY_AVAILABLE on this (POSIX) test machine

    lines: list[str] = []
    found: list[tuple[str, str]] = []
    awaiting: list[bool] = []
    worker.line_received.connect(lines.append)
    worker.url_found.connect(lambda url, code: found.append((url, code)))
    worker.input_requested.connect(lambda: awaiting.append(True))
    worker.start()

    _wait_until(qapp, lambda: bool(awaiting), timeout_ms=8000)
    assert found == [("https://claude.com/cai/oauth/authorize?state=abc123", "")]
    assert not any(line.strip() in ("✢", "*", "✶") for line in lines), (
        "a lone spinner glyph must be filtered, not shown to the artist as a line"
    )

    worker.send_line("PTY-INTEGRATION-TEST-CODE")
    _wait_until(qapp, lambda: any("got:PTY-INTEGRATION-TEST-CODE" in line for line in lines), timeout_ms=8000)
    worker.wait(3000)


# --- §21: setup-token mints a token, it doesn't sign anything in -----------
#
# `claude setup-token` writes no credentials file at all (docs/facts/acp-
# sdk.md §21) — it prints a subscription-scoped OAuth token exactly once
# and exits. A real, completed run on mayfx02 is what surfaced this: the
# owner's own token was never captured anywhere, gone the moment that
# process's stdout closed. These tests never use a real token — a fake,
# obviously-not-real value stands in throughout.

_FAKE_TOKEN = "FAKE-TOKEN-VALUE-NOT-REAL-1234567890"

#: Byte-for-byte the shape of a real run, taken from a captured Linux
#: sign-in (mayfx02, 2026-08-07) and not from the binary's string table.
#:
#: The last line matters as much as the token line. This fixture used to
#: interpolate `_FAKE_TOKEN` after `CLAUDE_CODE_OAUTH_TOKEN=`, and that
#: single wrong character span is why capture shipped broken: the code
#: matched the fixture, the fixture matched the assumption, and neither
#: matched the build. The real build prints `<token>` there — literally,
#: angle brackets and all — as instructions for a human to fill in.
_CLAUDE_TOKEN_SCRIPT = (
    "import sys\n"
    "print('Your OAuth token (valid for 1 year):')\n"
    f"print('{_FAKE_TOKEN}')\n"
    "print(\"Store this token securely. You won't be able to see it again.\")\n"
    "print('Use this token by setting: export CLAUDE_CODE_OAUTH_TOKEN=<token>')\n"
    "sys.stdout.flush()\n"
)


def test_setup_token_output_fires_token_captured_with_the_real_variable_name(qapp, tmp_path):
    ta = TerminalAuth(command=sys.executable, args=["-c", _CLAUDE_TOKEN_SCRIPT], env={})
    worker = TerminalLoginWorker("claude-acp", ta, cwd=str(tmp_path))

    captured: list[tuple[str, str]] = []
    worker.token_captured.connect(lambda env_var, token: captured.append((env_var, token)))
    worker.start()

    _wait_until(qapp, lambda: bool(captured))
    assert captured == [("CLAUDE_CODE_OAUTH_TOKEN", _FAKE_TOKEN)]
    worker.wait(3000)


def test_the_literal_placeholder_is_never_mistaken_for_a_token(qapp, tmp_path):
    """The regression that cost a real sign-in on Linux.

    A build that prints ONLY the instruction line — no label, no bare
    token — has handed the artist nothing worth storing. Capturing the
    literal `<token>` from it is worse than capturing nothing: it stores
    cleanly, reports "Signed in.", and then fails on the first prompt
    with no trace pointing back at the sign-in step.
    """
    script = (
        "import sys\n"
        "print('Use this token by setting: export CLAUDE_CODE_OAUTH_TOKEN=<token>')\n"
        "sys.stdout.flush()\n"
    )
    ta = TerminalAuth(command=sys.executable, args=["-c", script], env={})
    worker = TerminalLoginWorker("claude-acp", ta, cwd=str(tmp_path))

    captured: list[tuple[str, str]] = []
    exited: list[int] = []
    worker.token_captured.connect(lambda env_var, token: captured.append((env_var, token)))
    worker.exited.connect(exited.append)
    worker.start()

    _wait_until(qapp, lambda: bool(exited))
    worker.wait(3000)
    assert captured == [], f"stored a placeholder as if it were a token: {captured}"


def test_the_token_is_taken_from_the_bare_line_after_the_label(qapp, tmp_path):
    """Where the real build actually puts it (§21 corrected).

    The token arrives ALONE on the line after `Your OAuth token (valid
    for 1 year):`, and never appears after `CLAUDE_CODE_OAUTH_TOKEN=`.
    Anchoring on the variable name — as this module first did — cannot
    see it at all, which is exactly what a real Linux run produced:
    login succeeded, token printed, `agent_oauth_tokens` left empty.
    """
    script = (
        "import sys\n"
        "print('Your OAuth token (valid for 1 year):')\n"
        f"print('  {_FAKE_TOKEN}  ')\n"
        "sys.stdout.flush()\n"
    )
    ta = TerminalAuth(command=sys.executable, args=["-c", script], env={})
    worker = TerminalLoginWorker("claude-acp", ta, cwd=str(tmp_path))

    captured: list[tuple[str, str]] = []
    worker.token_captured.connect(lambda env_var, token: captured.append((env_var, token)))
    worker.start()

    _wait_until(qapp, lambda: bool(captured))
    worker.wait(3000)
    assert captured == [("CLAUDE_CODE_OAUTH_TOKEN", _FAKE_TOKEN)]


def test_the_token_never_reaches_line_received_once_the_label_is_seen(qapp, tmp_path):
    """Unlike a device code or a URL, this is a real, usable secret with
    nothing for the artist to read it FOR — redacted on the LIVE signal
    too, not only the log (§21's own departure from `test_a_token_looking_
    line_is_redacted_in_the_log_but_not_in_the_signal`'s general rule,
    deliberate, not an oversight)."""
    ta = TerminalAuth(command=sys.executable, args=["-c", _CLAUDE_TOKEN_SCRIPT], env={})
    worker = TerminalLoginWorker("claude-acp", ta, cwd=str(tmp_path))

    lines: list[str] = []
    exited: list[int] = []
    worker.line_received.connect(lines.append)
    worker.exited.connect(exited.append)
    worker.start()

    _wait_until(qapp, lambda: bool(exited))
    worker.wait(3000)

    assert lines, "the run produced no output at all — nothing to check"
    assert not any(_FAKE_TOKEN in line for line in lines), (
        "the raw token reached the transcript-facing signal"
    )
    # The label line itself carries no secret and is untouched.
    assert any("Your OAuth token" in line for line in lines)


def test_the_token_never_reaches_the_log_either(qapp, tmp_path, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="houdini_agent_panel.ui.terminal_login")
    ta = TerminalAuth(command=sys.executable, args=["-c", _CLAUDE_TOKEN_SCRIPT], env={})
    worker = TerminalLoginWorker("claude-acp", ta, cwd=str(tmp_path))
    exited: list[int] = []
    worker.exited.connect(exited.append)
    worker.start()

    _wait_until(qapp, lambda: bool(exited))
    worker.wait(3000)

    messages = [r.message for r in caplog.records]
    assert not any(_FAKE_TOKEN in m for m in messages)
    assert any("OAuth token captured" in m for m in messages)


def test_the_pty_is_wide_enough_that_the_child_never_wraps_a_token(qapp, tmp_path):
    """The `401 OAuth access token is invalid` regression.

    `pty.openpty()` hands out a terminal with no size, which an Ink-based
    build reads as 80 columns and hard-wraps to. A real run split the
    108-character token into 79 + 29, and the panel stored the first
    piece as if it were the whole thing — "Signed in.", then a 401 on the
    first prompt. Nothing marks a continued line, so this has to be
    prevented at the source rather than repaired afterwards.
    """
    import fcntl
    import pty
    import struct
    import termios

    from houdini_agent_panel.ui import terminal_login as tl

    master_fd, slave_fd = pty.openpty()
    try:
        rows, cols, _, _ = struct.unpack(
            "HHHH", fcntl.ioctl(slave_fd, termios.TIOCGWINSZ, b"\0" * 8)
        )
        assert cols in (0, 80), f"unexpected default width {cols} — the premise changed"

        tl._set_pty_size(slave_fd)

        rows, cols, _, _ = struct.unpack(
            "HHHH", fcntl.ioctl(slave_fd, termios.TIOCGWINSZ, b"\0" * 8)
        )
        assert cols == tl._PTY_COLUMNS
        assert rows == tl._PTY_ROWS
        # The measured failure: a 108-character token plus a leading
        # space has to fit on one line with room to spare.
        assert cols > 108 * 2
    finally:
        os.close(master_fd)
        os.close(slave_fd)


def test_a_real_pty_run_reports_the_width_we_set(qapp, tmp_path):
    """End to end through `work()`: the child asks its own tty how wide
    it is and prints the answer — the same question the real build asks
    before deciding where to wrap."""
    script = (
        "import os, sys\n"
        "print('cols=%d' % os.get_terminal_size(sys.stdout.fileno()).columns)\n"
        "sys.stdout.flush()\n"
    )
    ta = TerminalAuth(command=sys.executable, args=["-c", script], env={})
    worker = TerminalLoginWorker("claude-acp", ta, cwd=str(tmp_path), use_pty=True)

    lines: list[str] = []
    exited: list[int] = []
    worker.line_received.connect(lines.append)
    worker.exited.connect(exited.append)
    worker.start()

    _wait_until(qapp, lambda: bool(exited))
    worker.wait(3000)

    from houdini_agent_panel.ui import terminal_login as tl

    assert any(f"cols={tl._PTY_COLUMNS}" in line for line in lines), lines

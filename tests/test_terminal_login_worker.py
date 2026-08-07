"""`TerminalLoginWorker`: spawns Kimi's `kimi login`-shaped process off the
main thread, reads its output, and turns a `Verification URL:` line into a
real link (docs/facts/acp-sdk.md §14) — or degrades quietly if the line
never appears, since the format isn't guaranteed stable (n=1 sample).
"""

from __future__ import annotations

import sys

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

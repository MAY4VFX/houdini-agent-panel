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

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

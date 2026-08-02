"""An agent that doesn't reply to initialize.

Regression from a live Houdini session: the panel printed "Launching
claude-acp…" and hung forever. The agent process had died right after
starting — the path to `npx-cli.js` resolved to a file that didn't exist on
a Homebrew machine — its pipes closed, a reply to `initialize` could no
longer arrive under any circumstances, and the client just kept waiting.

The cause was one specific thing that time, but there's no reason good
enough to wait forever. So these tests don't check for that specific broken
path, they check the client's behavior: the process died or is silent — the
panel is obligated to say so.
"""

from __future__ import annotations

import sys

import pytest

from houdini_agent_panel.client import AcpClient
from houdini_agent_panel.runtime import LaunchSpec


def _wait_for(qapp, predicate, timeout_ms: int = 15000) -> bool:
    from houdini_agent_panel.ui.qt import QtCore

    timer = QtCore.QElapsedTimer()
    timer.start()
    while timer.elapsed() < timeout_ms:
        qapp.processEvents()
        if predicate():
            return True
        QtCore.QThread.msleep(20)
    return False


def test_agent_that_dies_immediately_reports_instead_of_hanging(qapp):
    client = AcpClient()
    failures: list[str] = []
    client.failed.connect(failures.append)

    # A process that dies instantly with a non-zero code and writes to
    # stderr — exactly what `node <nonexistent-file>.js` does.
    spec = LaunchSpec(
        command=sys.executable,
        args=["-c", "import sys; sys.stderr.write('cannot find module npx-cli.js\\n'); sys.exit(1)"],
        env={},
    )
    client.start(spec, cwd=".")

    assert _wait_for(qapp, lambda: bool(failures)), "the client hung instead of reporting an error"
    assert not client.is_running()

    message = failures[0]
    # Which one wins the race — the SDK's own connection drop or our own
    # process watchdog — depends on which got there first, and it doesn't
    # matter. What matters is that the message explains the cause, and the
    # substance is almost always in stderr: a missing file, missing
    # permissions, a missing environment variable.
    assert "npx-cli.js" in message, f"the stderr tail should end up in the message: {message!r}"
    assert len(message.splitlines()) > 1, f"a single line with no detail is useless: {message!r}"

    client.stop()


def test_agent_that_starts_but_never_answers_hits_the_ceiling(qapp, monkeypatch):
    """The process is alive and silent — also not a reason to wait forever."""
    from houdini_agent_panel import client as client_module

    monkeypatch.setattr(client_module, "_CONNECT_TIMEOUT", 1.0)

    client = AcpClient()
    failures: list[str] = []
    client.failed.connect(failures.append)

    spec = LaunchSpec(
        command=sys.executable,
        args=["-c", "import time; time.sleep(60)"],
        env={},
    )
    client.start(spec, cwd=".")

    assert _wait_for(qapp, lambda: bool(failures), timeout_ms=15000), "the client hung on a silent agent"
    assert "initialize" in failures[0]

    client.stop()

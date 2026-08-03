"""Starting a second agent on a live client must take the first one down.

`AcpClient.start` reuses the worker thread it already has, so a restart or
an agent switch used to run `do_start` straight over a live connection:
`_conn`, `_writer` and `_process` were overwritten while the previous agent
was still running. Its process kept going, its pipes stayed open, and the
SDK's own receive/dispatch/send tasks stayed on the loop with nothing left
holding them — surfacing at shutdown as "Task was destroyed but it is
pending" and "cannot reuse already awaited coroutine", one set per switch.

The noise was the visible part; the leak was the real one.
"""

from __future__ import annotations

import sys

from houdini_agent_panel.client import AcpClient
from houdini_agent_panel.runtime import LaunchSpec
from houdini_agent_panel.ui.qt import QtCore


def _wait_for(qapp, predicate, timeout_ms: int = 15000) -> bool:
    timer = QtCore.QElapsedTimer()
    timer.start()
    while timer.elapsed() < timeout_ms:
        qapp.processEvents()
        if predicate():
            return True
        QtCore.QThread.msleep(20)
    return False


def _idle_agent() -> LaunchSpec:
    """A process that stays up and says nothing — an agent as far as the
    client can tell, right until it is asked to go away."""
    return LaunchSpec(
        command=sys.executable,
        args=["-c", "import sys; sys.stdin.read()"],
        env={},
    )


def test_starting_again_stops_the_process_already_running(qapp):
    client = AcpClient()
    client.start(_idle_agent(), cwd=".")

    assert _wait_for(qapp, lambda: client._worker is not None
                     and client._worker._process is not None)
    first = client._worker._process
    assert first.poll() is None, "the first agent should be running"

    client.start(_idle_agent(), cwd=".")

    assert _wait_for(
        qapp,
        lambda: client._worker._process is not None and client._worker._process is not first,
    ), "the second start never replaced the first process"

    # The point of the whole exercise: the first process is gone, not merely
    # forgotten about.
    assert _wait_for(qapp, lambda: first.poll() is not None), (
        "the first agent process was left running after starting a second one"
    )

    client.stop()

"""A stuck busy flag must never turn the panel into a dead end.

From a live session: the send button sat as a stop button and pressing it
did nothing at all. A turn that was in flight when the agent went away can
never finish, so the session stayed marked busy — and came back that way on
every switch.
"""

from __future__ import annotations

import pytest

from houdini_agent_panel import sessions
from houdini_agent_panel.ui import panel as panel_mod


@pytest.fixture(autouse=True)
def isolated(qapp, monkeypatch):
    monkeypatch.setattr(panel_mod.scene, "hip_dir", lambda: "/tmp")
    monkeypatch.setattr(
        panel_mod.scene, "mcp_servers",
        lambda: [{"name": "fxhoudini", "command": "python", "args": [], "env": []}],
    )
    monkeypatch.setattr(panel_mod._RefreshWorker, "start", lambda self: None)
    panel_mod.reset_shared_state_for_tests()
    yield
    panel_mod.reset_shared_state_for_tests()


def _state(session_id: str) -> sessions.SessionState:
    return sessions.SessionState(
        session_id=session_id, title="New conversation", cwd="/tmp", created_at=0.0
    )


def test_disconnect_clears_busy_on_every_session(qapp):
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    client = panel_mod.shared_client(widget._agent_id)

    for name in ("a", "b"):
        client.session_started.emit(name, _state(name))
    qapp.processEvents()
    for state in widget._pool.all():
        state.busy = True

    client.disconnected.emit("agent went away")
    qapp.processEvents()

    assert all(not s.busy for s in widget._pool.all())
    assert not widget._composer._busy
    widget.shutdown()


def test_new_session_is_never_born_busy(qapp):
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    client = panel_mod.shared_client(widget._agent_id)

    stale = _state("fresh")
    stale.busy = True
    client.session_started.emit("fresh", stale)
    qapp.processEvents()

    assert not widget._pool.get("fresh").busy
    widget.shutdown()


def test_submitting_while_busy_queues_instead_of_refusing(qapp):
    """This used to be the dead end: a turn in flight meant "send" silently
    refused, and a thought that arrived mid-turn had nowhere to go but the
    artist's own head. It queues now (`ui/panel.py::_on_enqueue_requested`)
    — busy no longer means refused, only "not yet"."""
    from houdini_agent_panel.ui.composer import Composer

    composer = Composer()
    queued: list[list] = []
    rejected: list[str] = []
    composer.enqueue_requested.connect(queued.append)
    composer.attachment_rejected.connect(rejected.append)
    composer.set_busy(True)
    composer._text_edit.setPlainText("hello")

    composer._submit()

    assert queued, "busy must queue the message, not drop it in silence"
    assert queued[0][0]["text"] == "hello"
    assert not rejected
    composer.deleteLater()

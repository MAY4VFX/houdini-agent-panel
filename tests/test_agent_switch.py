"""Switching agents starts a clean conversation.

A session id is issued by one specific agent process and means nothing to
any other. The pool used to survive the switch, so the panel believed it
still had a live conversation, skipped creating a new session, and sent
prompts carrying an id the new agent had never issued — which just hung.
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
        session_id=session_id, title="Conversation", cwd="/tmp", created_at=0.0
    )


def test_pool_clear_drops_everything_and_announces_it():
    pool = sessions.SessionPool()
    removed: list[str] = []
    pool.removed.connect(removed.append)
    pool.add(_state("a"))
    pool.add(_state("b"))

    pool.clear()

    assert pool.all() == []
    assert pool.current() is None
    assert sorted(removed) == ["a", "b"]


def test_switching_agents_does_not_carry_the_old_session(qapp, monkeypatch):
    widget = panel_mod.AgentPanel()
    qapp.processEvents()

    client = panel_mod.shared_client()
    client.session_started.emit("claude-session", _state("claude-session"))
    qapp.processEvents()
    assert widget._pool.current() is not None

    monkeypatch.setattr(widget, "_start_agent", lambda agent_id: None)
    widget._on_agent_chosen("codex-acp")

    assert widget._pool.current() is None, (
        "a session issued by the previous agent must not survive the switch"
    )
    assert widget._pool.all() == []
    widget.shutdown()


def test_conversation_survives_an_agent_switch(qapp, monkeypatch):
    """The artist's words are not the agent's property.

    Wiping the transcript on every switch was the bug, not the feature: an
    agent session id dies with its process, but what was written and read
    belongs to the person who wrote it.
    """
    from houdini_agent_panel import conversations_store as store

    widget = panel_mod.AgentPanel()
    qapp.processEvents()

    client = panel_mod.shared_client()
    client.session_started.emit("claude-session", _state("claude-session"))
    qapp.processEvents()
    widget._pool.get("claude-session").title = "Rotor pyro"
    client.message_chunk.emit("claude-session", "m1", "answer from Claude")
    qapp.processEvents()

    monkeypatch.setattr(widget, "_start_agent", lambda agent_id: None)
    widget._on_agent_chosen("codex-acp")

    # The dead session id is gone — it means nothing to the new agent.
    assert widget._pool.all() == []
    # The conversation is not.
    saved = store.load()
    assert [c.title for c in saved] == ["Rotor pyro"]
    assert any("answer from Claude" in e["text"] for e in saved[0].entries)
    widget.shutdown()



def test_stop_releases_the_input_even_if_the_agent_never_answers(qapp, monkeypatch):
    """`session/cancel` is a notification — the agent may ignore it. The panel
    must not stay locked forever because of that."""
    monkeypatch.setattr(panel_mod, "_CANCEL_GRACE_MS", 10)

    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    client = panel_mod.shared_client()
    state = _state("live")
    client.session_started.emit(state.session_id, state)
    qapp.processEvents()

    pooled = widget._pool.get("live")
    pooled.busy = True
    widget._composer.set_busy(True)
    monkeypatch.setattr(client, "cancel", lambda _sid: None)  # agent stays silent

    widget._on_cancelled()

    from houdini_agent_panel.ui.qt import QtCore

    deadline = QtCore.QElapsedTimer()
    deadline.start()
    while deadline.elapsed() < 3000 and pooled.busy:
        qapp.processEvents()
        QtCore.QThread.msleep(5)

    assert not pooled.busy, "the panel trapped the artist with a dead stop button"
    assert not widget._composer._busy
    widget.shutdown()


def test_stop_without_a_session_still_unlocks(qapp):
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    widget._composer.set_busy(True)

    widget._on_cancelled()

    assert not widget._composer._busy
    widget.shutdown()

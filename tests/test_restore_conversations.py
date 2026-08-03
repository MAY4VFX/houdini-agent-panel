"""Conversations come back after Houdini restarts.

They are stored on disk already; what was missing was showing them. A
restored conversation has no agent session behind it — it is history the
artist can read — and the first message sent into one has to open a real
session without losing what was typed.
"""

from __future__ import annotations

import pytest

from houdini_agent_panel import client as client_mod
from houdini_agent_panel import conversations_store as store
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


def _stored(title: str, text: str, *, updated: float = 1.0) -> store.StoredConversation:
    conversation = store.StoredConversation.new(title=title)
    conversation.updated_at = updated
    conversation.entries = [{"kind": "user", "id": "e1", "text": text}]
    return conversation


def test_restored_conversations_appear_with_their_transcripts(qapp):
    first = _stored("Rotor pyro", "make dust", updated=2.0)
    store.save([first, _stored("Older", "hello", updated=1.0)])

    widget = panel_mod.AgentPanel()
    widget._restore_conversations()
    qapp.processEvents()

    titles = [s.title for s in widget._pool.all()]
    assert "Rotor pyro" in titles and "Older" in titles

    key = panel_mod._RESTORED_PREFIX + first.id
    assert [e.text for e in widget._model(key).entries()] == ["make dust"]
    widget.shutdown()


def test_the_conversation_that_was_open_comes_back_on_top(qapp):
    """Recency alone can't answer this: saving bumps every live conversation,
    so more than one can tie for most recent."""
    wanted = _stored("Was open", "text", updated=5.0)
    store.save([_stored("Other", "text", updated=5.0), wanted], active_id=wanted.id)

    widget = panel_mod.AgentPanel()
    widget._restore_conversations()
    qapp.processEvents()

    current = widget._pool.current()
    assert current is not None and current.title == "Was open"
    widget.shutdown()


def test_sending_into_a_restored_conversation_opens_a_real_session(qapp, monkeypatch):
    conversation = _stored("Rotor pyro", "make dust")
    store.save([conversation])

    widget = panel_mod.AgentPanel()
    widget._restore_conversations()
    qapp.processEvents()

    opened: list[bool] = []
    monkeypatch.setattr(widget, "_start_new_session", lambda: opened.append(True))

    widget._on_submitted([{"type": "text", "text": "and more dust"}])

    assert opened, "a restored conversation must open a session instead of refusing"
    assert widget._pending_prompt, "the typed message must not be thrown away"
    widget.shutdown()


def test_the_transcript_moves_onto_the_live_session(qapp, monkeypatch):
    conversation = _stored("Rotor pyro", "make dust")
    store.save([conversation])

    widget = panel_mod.AgentPanel()
    widget._restore_conversations()
    qapp.processEvents()
    monkeypatch.setattr(widget, "_start_new_session", lambda: None)
    widget._on_submitted([{"type": "text", "text": "and more"}])

    live = sessions.SessionState(
        session_id="live-1", title="New conversation", cwd="/tmp", created_at=0.0
    )
    panel_mod.shared_client().session_started.emit("live-1", live)
    qapp.processEvents()

    texts = [e.text for e in widget._model("live-1").entries()]
    # The restored history came across, and the message typed while there was
    # no session went out rather than being dropped.
    assert texts[0] == "make dust"
    assert "and more" in texts
    assert widget._pool.get(panel_mod._RESTORED_PREFIX + conversation.id) is None
    assert widget._pool.get("live-1").title == "Rotor pyro"
    widget.shutdown()


def test_connecting_gives_the_restored_conversation_a_live_session(qapp, monkeypatch):
    """A transcript off disk must not sit there without an agent under it.

    Modes, slash commands and the model picker all arrive with `session/new`.
    While the panel waited for the artist's first message before opening a
    session, a restored conversation came back on screen with no controls
    beneath it — no mode chip, no model — and nothing said why.
    """
    conversation = _stored("Rotor pyro", "make dust")
    store.save([conversation])

    widget = panel_mod.AgentPanel()
    widget._restore_conversations()
    qapp.processEvents()

    opened: list[bool] = []
    monkeypatch.setattr(widget, "_start_new_session", lambda: opened.append(True))

    widget._on_connected(
        client_mod.AgentInfo(
            name="claude",
            version="1.0",
            protocol_version=1,
            supports_image=False,
            supports_audio=False,
            supports_embedded_context=False,
            supports_load_session=False,
            supports_logout=False,
            auth_methods=(),
        )
    )

    assert opened, "connecting must open a session for the restored conversation"
    assert widget._adopting_restored == panel_mod._RESTORED_PREFIX + conversation.id
    # Nothing was typed, so nothing may be queued to send.
    assert not widget._pending_prompt
    widget.shutdown()

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


def _stored(
    title: str, text: str, *, updated: float = 1.0, cwd: str = "/tmp"
) -> store.StoredConversation:
    """`cwd` defaults to what the fixture reports as `$HIP`, because a
    conversation belongs to a scene now — one written for another scene is
    not supposed to show up here."""
    conversation = store.StoredConversation.new(title=title, cwd=cwd)
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


def test_nothing_restored_is_opened_on_screen(qapp):
    """History belongs in the drawer, not on the screen.

    Opening the last conversation at startup looked helpful and misled: it
    was usually had with a different agent, so today's agent was shown a
    transcript it has no memory of, presented as continuous. The panel opens
    a new chat; the old ones are one click away.
    """
    wanted = _stored("Was open", "text", updated=5.0)
    store.save([_stored("Other", "text", updated=5.0), wanted], active_id=wanted.id)

    widget = panel_mod.AgentPanel()
    widget._restore_conversations()
    qapp.processEvents()

    assert widget._current_session() is None, (
        "a conversation from a previous session was put on screen"
    )
    titles = sorted(s.title for s in widget._pool.all())
    assert titles == ["Other", "Was open"], "the drawer must still list them"
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
    # Opening one is a click now, not something the panel does at startup.
    widget._set_current_session(panel_mod._RESTORED_PREFIX + conversation.id)
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


def test_connecting_opens_a_fresh_chat_rather_than_reviving_history(qapp, monkeypatch):
    """The reason the panel no longer adopts a restored conversation on
    connect: there is nothing on screen to adopt. A new chat gets its modes,
    its model picker and its slash commands from `session/new` the ordinary
    way, and the artist is not shown a conversation their agent never had.
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
            name="claude", version="1.0", protocol_version=1,
            supports_image=False, supports_audio=False, supports_embedded_context=False,
            supports_load_session=False, supports_logout=False, auth_methods=(),
        )
    )

    assert opened, "connecting must open a session"
    assert widget._adopting_restored is None, "nothing was on screen to adopt"
    assert not widget._pending_prompt
    widget.shutdown()


def test_only_this_scenes_conversations_come_back(qapp):
    """Talking to an agent about one shot has nothing to do with the next.

    Everything lived in one undifferentiated list, so opening any scene
    showed every conversation ever had in every scene — which is how an
    artist ends up scrolling past someone else's shot to find their own.
    """
    here = _stored("This scene", "about this shot", cwd="/tmp")
    store.save([here, _stored("Another scene", "about that shot", cwd="/elsewhere")])

    widget = panel_mod.AgentPanel()
    widget._restore_conversations()
    qapp.processEvents()

    titles = [s.title for s in widget._pool.all()]
    assert titles == ["This scene"], f"the other scene's conversation leaked in: {titles}"
    widget.shutdown()


def test_the_scene_a_conversation_belongs_to_is_written_down(qapp, monkeypatch):
    """Saving stamps the session's own directory, not wherever the panel
    happens to be looking when the save runs."""
    widget = panel_mod.AgentPanel()
    state = sessions.SessionState(
        session_id="live-9", title="Rotor pyro", cwd="/shots/rotor", created_at=0.0
    )
    widget._pool.add(state)
    widget._conversation_ids["live-9"] = "conv-9"
    widget._model("live-9").append_user("make dust")

    widget._persist_conversations()

    written = {c.id: c for c in store.load()}
    assert written["conv-9"].cwd == "/shots/rotor"
    assert not store.load("/tmp"), "it must not show up under a different scene"
    assert [c.title for c in store.load("/shots/rotor")] == ["Rotor pyro"]
    widget.shutdown()

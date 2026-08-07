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
from houdini_agent_panel import settings as settings_mod
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
    panel_mod.shared_client(widget._agent_id).session_started.emit("live-1", live)
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


def test_only_this_agents_conversations_come_back(qapp):
    """A conversation had with Claude has nothing to do with Gemini's own
    list — the same scoping as scene, one level down."""
    claude = _stored("With Claude", "about the shot")
    claude.agent_id = "claude-acp"
    gemini = _stored("With Gemini", "about the shot")
    gemini.agent_id = "gemini"
    store.save([claude, gemini])

    current = settings_mod.load()
    current.default_agent = "claude-acp"
    current.autostart_agent = False
    settings_mod.save(current)

    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    assert widget._agent_id == "claude-acp"

    titles = [s.title for s in widget._pool.all()]
    assert titles == ["With Claude"], f"Gemini's conversation leaked in: {titles}"
    widget.shutdown()


def test_the_agent_a_conversation_belongs_to_is_written_down(qapp):
    """Saving stamps THIS tab's own agent, not the process-wide default —
    see `AgentPanel._agent_id`."""
    current = settings_mod.load()
    current.default_agent = "claude-acp"
    current.autostart_agent = False
    settings_mod.save(current)

    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    assert widget._agent_id == "claude-acp"
    state = sessions.SessionState(
        session_id="live-9", title="Rotor pyro", cwd="/tmp", created_at=0.0
    )
    widget._pool.add(state)
    widget._conversation_ids["live-9"] = "conv-9"
    widget._model("live-9").append_user("make dust")

    widget._persist_conversations()

    written = {c.id: c for c in store.load()}
    assert written["conv-9"].agent_id == "claude-acp"
    widget.shutdown()


def test_switching_agents_persists_under_the_agent_it_was_actually_had_with(qapp, monkeypatch):
    """A real bug, found while adding agent scoping: `_on_agent_chosen`
    updates `settings.default_agent` to the NEW agent before persisting the
    conversation that belongs to the OLD one. Tagging it with
    `default_agent` at that point would mislabel it — and with the new
    per-agent filter in `conversations_store.load`, that conversation would
    then never be found again under the agent it was actually had with.
    """
    current = settings_mod.load()
    current.default_agent = "claude-acp"
    current.autostart_agent = False
    settings_mod.save(current)

    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    assert widget._agent_id == "claude-acp"

    client = panel_mod.shared_client("claude-acp")
    client.session_started.emit("live-1", sessions.SessionState(
        session_id="live-1", title="With Claude", cwd="/tmp", created_at=0.0
    ))
    qapp.processEvents()
    widget._model("live-1").append_user("make dust")

    monkeypatch.setattr(widget, "_start_agent", lambda agent_id: None)
    widget._on_agent_chosen("gemini")

    written = [c for c in store.load(agent_id="claude-acp") if c.title == "With Claude"]
    assert written, "the conversation vanished from claude-acp's own history on switching away"
    assert not store.load(agent_id="gemini"), "it must not have been mislabelled as gemini's"
    widget.shutdown()


def test_the_note_about_old_history_combines_scene_and_agent_into_one_line(qapp, monkeypatch):
    """One note for both historical gaps, not two near-identical ones right
    next to each other (see `conversations_store.unscoped_count`)."""
    no_cwd = store.StoredConversation.new(title="no cwd", agent_id="claude-acp")
    no_agent = _stored("no agent", "text")  # has this fixture's default cwd, no agent_id
    store.save([no_cwd, no_agent])

    notes: list[str] = []
    widget = panel_mod.AgentPanel()
    widget._note = notes.append
    # Not `qapp.processEvents()` afterwards: `_boot()` is still queued
    # (deferred via `QTimer.singleShot`) and would call
    # `_restore_conversations()` a second time on its own, double-counting
    # the note this test is checking for exactly once.
    widget._restore_conversations()

    matching = [n for n in notes if "2 conversation" in n]
    assert len(matching) == 1, f"expected exactly one combined note, got: {notes}"
    assert "scene" in matching[0] and "agent" in matching[0]
    widget.shutdown()


def test_a_conversation_is_never_saved_without_a_scene(qapp):
    """Twenty-five of the owner's Claude conversations were saved with an
    empty `cwd` and became invisible for good — a conversation with no scene
    matches no scene.

    The cause: the scene was only written when the session was still in the
    pool, and switching agents clears the pool before persisting. Leaving the
    pool is exactly when persisting matters most.
    """
    from houdini_agent_panel.ui import panel as panel_mod

    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("claude-acp")
    widget._conversation_ids["gone-1"] = "conv-gone"
    widget._model("gone-1").append_user("what happened to my chats")
    # No SessionState for "gone-1": the pool has already been cleared, which
    # is the state an agent switch leaves behind.
    assert widget._pool.get("gone-1") is None

    widget._persist_conversations()

    saved = {c.id: c for c in store.load()}
    assert saved["conv-gone"].cwd, "saved with no scene — invisible from now on"
    assert saved["conv-gone"].cwd == panel_mod.scene.hip_dir()
    widget.shutdown()


# --- empty-scope hint, drawn from the real store ----------------------
#
# The measured incident: the owner dumped his own store and found 41
# conversations scoped to `/Users/may` — all grok-build, none claude-acp
# — with claude-acp's own 2 living under `/Users/may/BS/ship`. The drawer
# was correct both times he read it as data loss. `_compute_empty_scope_
# text` is what turns that correct absence into an explanation.


def test_empty_scope_text_reproduces_the_owners_own_numbers(qapp):
    """The exact reported shape, not a simplified stand-in: 41
    conversations in THIS folder belonging to a different agent, 2
    belonging to THIS agent in a different folder."""
    grok_ones = [
        store.StoredConversation.new(title=f"grok chat {i}", agent_id="grok-build", cwd="/tmp")
        for i in range(41)
    ]
    claude_ones = [
        store.StoredConversation.new(title="ship work", agent_id="claude-acp", cwd="/elsewhere"),
        store.StoredConversation.new(title="more ship work", agent_id="claude-acp", cwd="/elsewhere"),
    ]
    store.save(grok_ones + claude_ones)

    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("claude-acp")

    text = widget._compute_empty_scope_text()

    assert text == (
        "No conversations for Claude Agent in this scene folder — "
        "41 here for other agents, 2 for Claude Agent in other folders"
    )
    widget.shutdown()


def test_empty_scope_text_says_nothing_extra_with_no_cross_scope_history(qapp):
    """A genuinely fresh store (nothing anywhere) gets the plain sentence
    — no "0 elsewhere" inviting the same doubt this exists to close."""
    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("claude-acp")

    text = widget._compute_empty_scope_text()

    assert text == "No conversations for Claude Agent in this scene folder"
    widget.shutdown()


def test_empty_scope_text_before_an_agent_is_chosen(qapp):
    widget = panel_mod.AgentPanel()
    assert widget._agent_id == ""

    text = widget._compute_empty_scope_text()

    assert text == "No conversations here yet"
    widget.shutdown()


def test_empty_scope_text_ignores_unscoped_history(qapp):
    """Conversations saved before scoping existed (empty cwd/agent_id)
    must not count as "elsewhere" — `conversations_store.unscoped_count`
    already has its own, separate note for those; double-counting them
    here would be a second, confusing way to say the same thing."""
    store.save(
        [
            store.StoredConversation.new(title="ancient", agent_id="", cwd=""),
            store.StoredConversation.new(title="half-scoped", agent_id="claude-acp", cwd=""),
        ]
    )
    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("claude-acp")

    text = widget._compute_empty_scope_text()

    assert text == "No conversations for Claude Agent in this scene folder"
    widget.shutdown()


def test_refresh_sessions_only_computes_the_hint_when_the_pool_is_empty(qapp, monkeypatch):
    """The cost this whole design is careful about — a full store read —
    must never run on an ordinary, populated refresh, only the rare one
    where the list just went empty. `_pool.add`/`.remove` already call
    `_refresh_sessions` themselves (the pool's own `added`/`removed`
    wiring — `_wire_pool`) — no separate explicit call needed here."""
    calls: list[int] = []
    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("claude-acp")
    monkeypatch.setattr(
        widget, "_compute_empty_scope_text", lambda: (calls.append(1), "unused")[1]
    )

    widget._pool.add(sessions.SessionState("s1", "Chat", "/tmp", 1.0))
    assert calls == []  # a populated pool — no reason to read the store

    widget._pool.remove("s1")
    assert calls == [1]  # went empty — exactly once
    widget.shutdown()

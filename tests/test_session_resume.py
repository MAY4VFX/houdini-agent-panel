"""A restored conversation actually continuing, via `session/load`.

Before this, reopening a past conversation and sending a message always
opened a brand new agent session: the transcript on screen stayed the same,
but the agent itself had no memory of any of it — "the previous conversation
ended on different information" was the direct, reported consequence.
`session/load` (an optional ACP capability, `AgentInfo.supports_load_session`)
is the protocol's own way to resume a session for real; this file covers when
the panel uses it, what happens when the agent can't, and what happens when a
resume is attempted and fails.

Panel-level: signals are emitted directly on `shared_client(...)`, the same
technique `test_restore_conversations.py` uses, so nothing here needs a real
agent subprocess. `test_client.py` covers the lower-level ACP plumbing
(`AcpClient.load_session`, `session_loaded`/`session_load_failed`) against a
real one (`tests/fake_agent.py`'s ``load``/``load-fail`` scenarios).
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


def _info(**kwargs) -> client_mod.AgentInfo:
    base = dict(
        name="agent", version="1.0", protocol_version=1,
        supports_image=False, supports_audio=False, supports_embedded_context=False,
        supports_load_session=True, supports_logout=False, auth_methods=(),
    )
    base.update(kwargs)
    return client_mod.AgentInfo(**base)


def _stored(title: str, text: str, *, agent_session_id: str = "") -> store.StoredConversation:
    conversation = store.StoredConversation.new(
        title=title, agent_id="claude-acp", cwd="/tmp"
    )
    conversation.agent_session_id = agent_session_id
    conversation.entries = [{"kind": "user", "id": "e1", "text": text}]
    return conversation


def _make_widget() -> panel_mod.AgentPanel:
    current = settings_mod.load()
    current.default_agent = "claude-acp"
    current.autostart_agent = False
    settings_mod.save(current)
    widget = panel_mod.AgentPanel()
    return widget


# --- deciding whether to resume ------------------------------------------


def test_resuming_calls_session_load_when_supported_and_a_session_id_survived(qapp, monkeypatch):
    conversation = _stored("Rotor pyro", "make dust", agent_session_id="agent-sess-9")
    store.save([conversation])

    widget = _make_widget()
    qapp.processEvents()
    client = panel_mod.shared_client("claude-acp")
    client._agent_info = _info(supports_load_session=True)
    client._running = True

    calls = []
    monkeypatch.setattr(
        client, "load_session",
        lambda **kw: calls.append(kw),
    )

    key = panel_mod._RESTORED_PREFIX + conversation.id
    widget._set_current_session(key)
    widget._on_submitted([{"type": "text", "text": "and more dust"}])

    assert calls, "an agent that supports loadSession with a known session id must use it"
    assert calls[0]["session_id"] == "agent-sess-9"
    assert widget._pending_prompt, "the typed message must still be waiting to be sent"
    widget.shutdown()


def test_no_capability_falls_back_to_a_new_session(qapp, monkeypatch):
    conversation = _stored("Rotor pyro", "make dust", agent_session_id="agent-sess-9")
    store.save([conversation])

    widget = _make_widget()
    qapp.processEvents()
    client = panel_mod.shared_client("claude-acp")
    client._agent_info = _info(supports_load_session=False)
    client._running = True

    load_calls = []
    new_calls = []
    monkeypatch.setattr(client, "load_session", lambda **kw: load_calls.append(kw))
    monkeypatch.setattr(widget, "_start_new_session", lambda: new_calls.append(True))

    key = panel_mod._RESTORED_PREFIX + conversation.id
    widget._set_current_session(key)
    widget._on_submitted([{"type": "text", "text": "and more dust"}])

    assert not load_calls, "the agent never declared loadSession — must not be asked for it"
    assert new_calls, "must fall back to the old read-only-history-on-a-new-session behavior"
    widget.shutdown()


def test_no_stored_session_id_falls_back_to_a_new_session(qapp, monkeypatch):
    """An agent that supports `loadSession` today is no help for a
    conversation saved before this feature existed, or one that was never
    anything but a restored replay — there is nothing to ask it to load."""
    conversation = _stored("Rotor pyro", "make dust", agent_session_id="")
    store.save([conversation])

    widget = _make_widget()
    qapp.processEvents()
    client = panel_mod.shared_client("claude-acp")
    client._agent_info = _info(supports_load_session=True)
    client._running = True

    load_calls = []
    new_calls = []
    monkeypatch.setattr(client, "load_session", lambda **kw: load_calls.append(kw))
    monkeypatch.setattr(widget, "_start_new_session", lambda: new_calls.append(True))

    key = panel_mod._RESTORED_PREFIX + conversation.id
    widget._set_current_session(key)
    widget._on_submitted([{"type": "text", "text": "and more dust"}])

    assert not load_calls
    assert new_calls
    widget.shutdown()


# --- re-entrant adoption: typing before the first attempt resolves --------


def test_a_second_adopt_while_the_first_is_still_in_flight_does_not_call_load_twice(
    qapp, monkeypatch
):
    """Reported for real: the boot-time background resume
    (`_adopt_running_client`'s own call to `_adopt_or_resume`) is already
    in flight when the artist types and sends — `current_session()` is
    still the restored placeholder at that point, so `_on_submitted`'s
    restored branch calls `_adopt_or_resume` again for the SAME
    conversation. Before the fix this sent a second, concurrent
    `session/load` for the same session id — the same class of race §15
    already measured for `session/prompt`, one protocol method over. Only
    one call may ever reach the wire per outstanding adoption."""
    conversation = _stored("Rotor pyro", "make dust", agent_session_id="agent-sess-9")
    store.save([conversation])

    widget = _make_widget()
    qapp.processEvents()
    client = panel_mod.shared_client("claude-acp")
    client._agent_info = _info(supports_load_session=True)
    client._running = True

    calls = []
    monkeypatch.setattr(client, "load_session", lambda **kw: calls.append(kw))

    key = panel_mod._RESTORED_PREFIX + conversation.id
    widget._set_current_session(key)

    # The boot-time attempt (unrelated to any typing).
    widget._adopt_or_resume(key)
    assert len(calls) == 1

    # The artist types and sends before that attempt has resolved —
    # `current_session()` is still the restored placeholder.
    widget._on_submitted([{"type": "text", "text": "and more dust"}])

    assert len(calls) == 1, "a second session/load for the same in-flight resume must never be sent"
    assert widget._pending_prompt == [{"type": "text", "text": "and more dust"}], (
        "the newly typed message must still be waiting for the one in-flight resume"
    )
    widget.shutdown()


def test_a_second_adopt_for_a_different_key_is_unaffected(qapp, monkeypatch):
    """The guard is keyed by restored session id, not a blanket "one resume
    at a time" — a genuinely different conversation must resume normally."""
    a = _stored("Rotor pyro", "make dust", agent_session_id="agent-sess-a")
    b = _stored("Water sim", "splash more", agent_session_id="agent-sess-b")
    store.save([a, b])

    widget = _make_widget()
    qapp.processEvents()
    client = panel_mod.shared_client("claude-acp")
    client._agent_info = _info(supports_load_session=True)
    client._running = True

    calls = []
    monkeypatch.setattr(client, "load_session", lambda **kw: calls.append(kw["session_id"]))

    key_a = panel_mod._RESTORED_PREFIX + a.id
    key_b = panel_mod._RESTORED_PREFIX + b.id
    widget._adopt_or_resume(key_a)
    widget._adopt_or_resume(key_b)

    assert calls == ["agent-sess-a", "agent-sess-b"]
    widget.shutdown()


# --- a successful resume --------------------------------------------------


def test_a_successful_load_continues_the_conversation_and_sends_the_pending_prompt(
    qapp, monkeypatch
):
    """Measured live, docs/facts/acp-sdk.md §32: `session/load`'s own
    replay is not a reliable way to rebuild what the artist sees — it can
    arrive after `session_loaded` already fired (an unpredictable split,
    not just "usually fine"), and `client.py` has always dropped
    `user_message_chunk` outright, so the artist's OWN old messages never
    came from replay at all, timing aside. `_on_session_loaded` keeps the
    local, already-complete record instead of discarding it — this test
    used to assert the opposite ("the local read-only copy must not be
    stacked on top of the agent's own replay"), which was exactly the
    reasoning the measurement disproved.
    """
    conversation = _stored("Rotor pyro", "make dust", agent_session_id="agent-sess-9")
    store.save([conversation])

    widget = _make_widget()
    qapp.processEvents()
    client = panel_mod.shared_client("claude-acp")
    client._agent_info = _info(supports_load_session=True)
    client._running = True
    monkeypatch.setattr(client, "load_session", lambda **kw: None)

    prompts = []
    monkeypatch.setattr(client, "prompt", lambda session_id, blocks: prompts.append((session_id, blocks)))

    key = panel_mod._RESTORED_PREFIX + conversation.id
    widget._set_current_session(key)
    widget._on_submitted([{"type": "text", "text": "and more dust"}])

    # The agent's own replay — a real one would use the SAME message_id
    # this conversation's own stored "make dust" reply once had; this
    # placeholder id has no match in the kept-alive local model, so it
    # lands as its own, additional entry rather than merging into
    # anything — exactly like a genuinely NEW live update after resuming
    # would. Order-independence is the point: fired here BEFORE
    # session_loaded, but nothing about this test depends on that order
    # any more (see test_replay_landing_after_session_loaded_is_still_
    # fine below for the other extreme).
    client.message_chunk.emit("agent-sess-9", "replay-1", "earlier: rotor pyro setup")
    qapp.processEvents()

    live = sessions.SessionState(
        session_id="agent-sess-9", title="New chat", cwd="/tmp", created_at=0.0
    )
    client.session_loaded.emit("agent-sess-9", live)
    qapp.processEvents()

    assert widget._current_session().session_id == "agent-sess-9"
    assert widget._pool.get(key) is None, "the restored placeholder must be gone"
    texts = [e.text for e in widget._model("agent-sess-9").entries()]
    assert "make dust" in texts, (
        "the artist's own past message must survive a resume — it never came from "
        "replay to begin with (client.py drops user_message_chunk unconditionally)"
    )
    assert "earlier: rotor pyro setup" in texts, "replay content is still shown, not discarded"
    assert texts.index("make dust") < texts.index("and more dust"), (
        "the kept-alive history stays ABOVE the newly typed message"
    )
    assert "and more dust" in texts, "the message typed while resuming must still go out"
    assert prompts and prompts[0][0] == "agent-sess-9"
    widget.shutdown()


def test_replay_landing_after_session_loaded_is_still_fine(qapp, monkeypatch):
    """The other measured extreme (docs/facts/acp-sdk.md §32): replay can
    arrive AFTER `session_loaded` already fired and this handler's own
    work (transplanting the model, sending the pending prompt) already
    ran. Nothing here waits for replay any more, so this must not lose or
    duplicate anything either — it simply lands in the same, still-live
    model whenever it shows up."""
    conversation = _stored("Rotor pyro", "make dust", agent_session_id="agent-sess-9")
    store.save([conversation])

    widget = _make_widget()
    qapp.processEvents()
    client = panel_mod.shared_client("claude-acp")
    client._agent_info = _info(supports_load_session=True)
    client._running = True
    monkeypatch.setattr(client, "load_session", lambda **kw: None)
    monkeypatch.setattr(client, "prompt", lambda session_id, blocks: None)

    key = panel_mod._RESTORED_PREFIX + conversation.id
    widget._set_current_session(key)
    widget._on_submitted([{"type": "text", "text": "and more dust"}])

    live = sessions.SessionState(
        session_id="agent-sess-9", title="New chat", cwd="/tmp", created_at=0.0
    )
    client.session_loaded.emit("agent-sess-9", live)
    qapp.processEvents()

    # Only now does replay show up — after everything session_loaded
    # itself already did.
    client.message_chunk.emit("agent-sess-9", "replay-1", "earlier: rotor pyro setup")
    qapp.processEvents()

    texts = [e.text for e in widget._model("agent-sess-9").entries()]
    assert "make dust" in texts
    assert "earlier: rotor pyro setup" in texts
    assert "and more dust" in texts
    widget.shutdown()


def test_replay_redelivering_an_agent_message_this_conversation_already_has_does_not_duplicate(
    qapp, monkeypatch
):
    """The decisive question, measured live against a real six-turn
    conversation and its own real replay (docs/facts/acp-sdk.md §32):
    `message_id` for an agent reply DOES match between the original turn
    and its later replay (the same underlying Claude API message id,
    persisted and replayed verbatim) — so `apply_chunk`'s own `(kind,
    message_id)` key, now rebuilt on load by `TranscriptModel.load_
    records` (this test's own reason for existing — that rebuild is what
    makes this dedup possible for a RESTORED conversation, not just one
    that stayed in memory), absorbs the redelivery into the entry that id
    already names. One entry, not two, whichever order the two same-id
    deliveries land in.
    """
    conversation = store.StoredConversation.new(title="Rotor pyro", agent_id="claude-acp", cwd="/tmp")
    conversation.agent_session_id = "agent-sess-9"
    conversation.entries = [
        {"kind": "user", "id": "e1", "text": "make dust"},
        {"kind": "agent", "id": "agent:msg_ABC123", "text": "the dust is made and settling now."},
    ]
    store.save([conversation])

    widget = _make_widget()
    qapp.processEvents()
    client = panel_mod.shared_client("claude-acp")
    client._agent_info = _info(supports_load_session=True)
    client._running = True
    monkeypatch.setattr(client, "load_session", lambda **kw: None)

    key = panel_mod._RESTORED_PREFIX + conversation.id
    widget._set_current_session(key)
    # No pending prompt this time — reached purely by switching onto the
    # restored tab, same as `_adopt_running_client`'s own boot-time call.
    widget._adopt_or_resume(key)

    live = sessions.SessionState(
        session_id="agent-sess-9", title="New chat", cwd="/tmp", created_at=0.0
    )
    client.session_loaded.emit("agent-sess-9", live)
    qapp.processEvents()

    # The agent's own replay of the SAME message, under the SAME id.
    client.message_chunk.emit("agent-sess-9", "msg_ABC123", "the dust is made and settling now.")
    qapp.processEvents()

    entries = widget._model("agent-sess-9").entries()
    agent_entries = [e for e in entries if e.kind == "agent"]
    assert len(agent_entries) == 1, f"expected one entry, not a duplicate: {agent_entries}"
    assert agent_entries[0].text == "the dust is made and settling now.", "redelivery must not double the text either"
    widget.shutdown()


# --- a failed resume -------------------------------------------------------


def test_a_failed_load_falls_back_with_a_clear_note_and_keeps_the_old_transcript(
    qapp, monkeypatch
):
    conversation = _stored("Rotor pyro", "make dust", agent_session_id="agent-sess-9")
    store.save([conversation])

    widget = _make_widget()
    qapp.processEvents()
    client = panel_mod.shared_client("claude-acp")
    client._agent_info = _info(supports_load_session=True)
    client._running = True
    monkeypatch.setattr(client, "load_session", lambda **kw: None)

    notes = []
    widget._note = lambda text, error=False: notes.append((text, error))

    new_session_calls = []
    monkeypatch.setattr(client, "new_session", lambda **kw: new_session_calls.append(kw))

    key = panel_mod._RESTORED_PREFIX + conversation.id
    widget._set_current_session(key)
    widget._on_submitted([{"type": "text", "text": "and more dust"}])

    client.session_load_failed.emit("agent-sess-9", "resource not found")
    qapp.processEvents()

    assert notes, "a failed resume must not be silent"
    text, is_error = notes[-1]
    assert is_error, "a genuine failure, not routine commentary"
    assert "resume" in text.lower() or "resumed" in text.lower()
    assert new_session_calls, "must fall back to opening a real, new session"

    # The fallback path is the ordinary adoption `_on_session_started`
    # already does for an agent with no loadSession at all: the old local
    # transcript rides onto whatever session comes up next.
    fresh = sessions.SessionState(
        session_id="fresh-1", title="New chat", cwd="/tmp", created_at=0.0
    )
    client.session_started.emit("fresh-1", fresh)
    qapp.processEvents()

    texts = [e.text for e in widget._model("fresh-1").entries()]
    assert "make dust" in texts, "the old history must not be thrown away on a failed resume"
    widget.shutdown()


def test_a_sibling_tabs_failed_load_does_not_show_up_here(qapp, monkeypatch):
    """`session_load_failed` reaches every tab wired to this agent — only
    the tab that actually asked for THIS session id may react to it."""
    conversation = _stored("Rotor pyro", "make dust", agent_session_id="agent-sess-9")
    store.save([conversation])

    widget = _make_widget()
    qapp.processEvents()
    client = panel_mod.shared_client("claude-acp")
    client._agent_info = _info(supports_load_session=True)
    client._running = True

    notes = []
    widget._note = lambda text, error=False: notes.append((text, error))

    # This tab never called load_session for anything — a stray failure for
    # some OTHER session id (another tab's own attempt) must be ignored.
    client.session_load_failed.emit("someone-elses-session", "boom")
    qapp.processEvents()

    assert not notes
    widget.shutdown()


# --- persisting the agent's own session id --------------------------------


def test_persisting_a_live_session_writes_its_agent_session_id(qapp):
    widget = _make_widget()
    qapp.processEvents()
    state = sessions.SessionState(
        session_id="live-9", title="Rotor pyro", cwd="/tmp", created_at=0.0
    )
    widget._pool.add(state)
    widget._conversation_ids["live-9"] = "conv-9"
    widget._model("live-9").append_user("make dust")

    widget._persist_conversations()

    written = {c.id: c for c in store.load()}
    assert written["conv-9"].agent_session_id == "live-9"
    widget.shutdown()


def test_persisting_an_unadopted_restored_conversation_keeps_its_old_session_id(qapp):
    """A restored, still-unadopted conversation is keyed by our OWN id
    (`_RESTORED_PREFIX + conversation_id`), never the agent's — persisting
    it must not overwrite the one real session id a future resume needs
    with something no agent ever issued."""
    conversation = _stored("Rotor pyro", "make dust", agent_session_id="agent-sess-9")
    store.save([conversation])

    widget = _make_widget()
    qapp.processEvents()
    widget._restore_conversations()
    qapp.processEvents()

    widget._persist_conversations()

    written = {c.id: c for c in store.load()}
    assert written[conversation.id].agent_session_id == "agent-sess-9"
    widget.shutdown()

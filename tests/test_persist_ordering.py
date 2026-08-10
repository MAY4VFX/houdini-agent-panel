"""A conversation nobody touched must keep its own `updated_at`.

Reported for real: the drawer's order didn't match the order conversations
were actually worked in, and the last conversation shown open wasn't the one
last worked on either. Measured on the owner's own store (`~/Library/
Application Support/HoudiniAgentPanel/conversations.json`, 50 conversations):
eight of nine conversations under one scene folder shared the exact same
`updated_at` instant, microseconds apart, though their `created_at` spanned
five separate days.

The cause: `AgentPanel._persist_conversations` iterates `self._models`,
which is shared PROCESS-WIDE per agent (`sessions.models`) — every
conversation this agent has ever had in this run, restored ones included,
not just the one on screen. It ran on every prompt sent and every turn
finished, and stamped `conversation.updated_at = time.time()` on every one
of them that still had entries, whether or not anything about it had
actually changed. `conversations_store._ordered` sorts by `-updated_at`, so
the drawer's order ended up reflecting iteration order of a dict, not real
recency.
"""

from __future__ import annotations

import pytest

from houdini_agent_panel import conversations_store as store
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


def _stored(title: str, text: str, *, updated: float) -> store.StoredConversation:
    conversation = store.StoredConversation.new(title=title, cwd="/tmp")
    conversation.updated_at = updated
    conversation.entries = [{"kind": "user", "id": "e1", "text": text}]
    return conversation


def test_an_untouched_conversation_keeps_its_updated_at_when_a_sibling_is_saved(qapp):
    touched = _stored("Touched", "make dust", updated=1.0)
    untouched = _stored("Untouched", "old news", updated=2.0)
    store.save([touched, untouched])

    widget = panel_mod.AgentPanel()
    widget._restore_conversations()
    qapp.processEvents()

    # Real work happens in ONE of the two restored conversations — the
    # other sits in `self._models` (shared per agent) completely untouched,
    # exactly the situation a save mid-conversation always produces.
    key = panel_mod._RESTORED_PREFIX + touched.id
    widget._model(key).append_user("more dust")

    widget._persist_conversations()

    written = {c.id: c for c in store.load()}
    assert written[touched.id].updated_at > touched.updated_at, (
        "the conversation that actually changed must move forward"
    )
    assert written[untouched.id].updated_at == untouched.updated_at, (
        "a conversation nobody touched must not be re-stamped just because "
        "a DIFFERENT one in the same agent's model pool changed"
    )
    widget.shutdown()


def test_persisting_repeatedly_does_not_touch_conversations_with_no_changes(qapp):
    """The realistic shape of the bug: `_persist_conversations` runs once
    per prompt AND once per finished turn — several times per real
    exchange — while every other conversation in the pool sits still."""
    touched = _stored("Touched", "make dust", updated=1.0)
    untouched = _stored("Untouched", "old news", updated=2.0)
    store.save([touched, untouched])

    widget = panel_mod.AgentPanel()
    widget._restore_conversations()
    qapp.processEvents()

    key = panel_mod._RESTORED_PREFIX + touched.id
    widget._model(key).append_user("more dust")
    widget._persist_conversations()  # "prompt sent"
    widget._persist_conversations()  # "turn finished" — nothing changed since

    written = {c.id: c for c in store.load()}
    assert written[untouched.id].updated_at == untouched.updated_at
    widget.shutdown()


def test_the_drawer_order_reflects_which_conversation_was_actually_worked_on(qapp):
    """The user-visible half of the same bug: order in the drawer
    (`conversations_store._ordered`, sorted by `-updated_at`) must track
    real recency, not iteration order of a shared, process-wide dict."""
    a = _stored("A", "a", updated=1.0)
    b = _stored("B", "b", updated=2.0)
    c = _stored("C", "c", updated=3.0)
    store.save([a, b, c])

    widget = panel_mod.AgentPanel()
    widget._restore_conversations()
    qapp.processEvents()

    # Only A is ever actually worked on — a prompt sent, then its turn
    # finishing, both calling `_persist_conversations` the same way the
    # real UI paths do (`_persist_conversations_soon`).
    key_a = panel_mod._RESTORED_PREFIX + a.id
    widget._model(key_a).append_user("more")
    widget._persist_conversations()
    widget._model(key_a).append_user("even more")
    widget._persist_conversations()

    ordered_titles = [conv.title for conv in store.load()]
    assert ordered_titles[0] == "A", (
        f"the conversation actually worked on must sort first: {ordered_titles}"
    )
    # B and C were never touched — their relative order must be exactly
    # what it was before A's saves ran, not shuffled by them.
    assert ordered_titles[1:] == ["C", "B"]
    widget.shutdown()


def test_a_brand_new_conversation_still_gets_a_real_updated_at(qapp):
    """The fix must not stop a genuinely NEW conversation from being
    stamped — only an unchanged EXISTING one."""
    widget = panel_mod.AgentPanel()
    from houdini_agent_panel import sessions

    state = sessions.SessionState(
        session_id="live-1", title="Fresh chat", cwd="/tmp", created_at=0.0
    )
    widget._pool.add(state)
    widget._conversation_ids["live-1"] = "conv-new"
    widget._model("live-1").append_user("hello")

    before = __import__("time").time()
    widget._persist_conversations()

    written = {c.id: c for c in store.load()}
    assert written["conv-new"].updated_at >= before
    widget.shutdown()

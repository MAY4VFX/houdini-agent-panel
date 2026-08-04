"""Conversations outlive the agent and Houdini itself.

The panel used to lose every conversation the moment the agent changed, and
all of them when Houdini closed — an agent session id was being treated as
the conversation. They are different things: the id belongs to one agent
process, the conversation belongs to the artist.
"""

from __future__ import annotations

import json

from houdini_agent_panel import conversations_store as store


def _made(title: str, *, pinned: bool = False, updated: float = 0.0) -> store.StoredConversation:
    conversation = store.StoredConversation.new(title=title)
    conversation.pinned = pinned
    conversation.updated_at = updated
    return conversation


def test_roundtrip_survives_a_restart(data_dir):
    original = _made("Rotor pyro", updated=10.0)
    original.entries = [{"kind": "user", "text": "hello"}]

    store.save([original])
    restored = store.load()

    assert [c.title for c in restored] == ["Rotor pyro"]
    assert restored[0].entries == [{"kind": "user", "text": "hello"}]


def test_pinned_come_first_then_most_recent(data_dir):
    store.save([_made("old", updated=1.0), _made("new", updated=9.0), _made("kept", pinned=True, updated=2.0)])

    assert [c.title for c in store.load()] == ["kept", "new", "old"]


def test_pinned_are_never_trimmed_away(data_dir, monkeypatch):
    """Pinning is the artist saying this one matters — dropping it silently
    would make the pin a lie."""
    monkeypatch.setattr(store, "MAX_CONVERSATIONS", 2)
    pinned = _made("keep me", pinned=True, updated=0.0)

    store.save([pinned] + [_made(f"chat {i}", updated=float(i + 1)) for i in range(10)])
    restored = store.load()

    assert "keep me" in [c.title for c in restored]
    assert len(restored) <= 3


def test_entries_are_capped(data_dir, monkeypatch):
    """A transcript that grows without limit eventually costs more to load
    than it is worth."""
    monkeypatch.setattr(store, "MAX_ENTRIES", 5)
    conversation = _made("long")
    conversation.entries = [{"kind": "agent", "text": str(i)} for i in range(50)]

    store.save([conversation])

    assert len(store.load()[0].entries) == 5


def test_a_broken_file_costs_history_not_the_panel(data_dir):
    store.store_path().write_text("{ this is not json", "utf-8")

    assert store.load() == []
    assert store.store_path().with_suffix(".json.broken").exists()


def test_garbage_entries_are_skipped_not_fatal(data_dir):
    store.store_path().write_text(
        json.dumps({"version": 1, "conversations": [{"no_id": True}, None, 42]}), "utf-8"
    )

    assert store.load() == []


def test_write_is_atomic_leaving_no_temp_file(data_dir):
    store.save([_made("one")])

    leftovers = list(data_dir.glob("conversations.json.tmp"))
    assert leftovers == []


# --- active_id: which conversation to reselect on restore -------------------


def test_active_id_roundtrips(data_dir):
    old = _made("old", updated=1.0)
    current = _made("current", updated=1.0)  # same updated_at — recency alone can't tell them apart

    store.save([old, current], active_id=current.id)

    assert store.load_active_id() == current.id


def test_active_id_defaults_to_none_when_not_passed(data_dir):
    """Existing callers that don't track a current conversation yet must
    keep working exactly as before."""
    store.save([_made("solo")])

    assert store.load_active_id() is None


def test_active_id_none_when_nothing_ever_saved(data_dir):
    assert store.load_active_id() is None


def test_active_id_none_on_broken_file(data_dir):
    store.store_path().write_text("{ not json", "utf-8")

    assert store.load_active_id() is None


def test_load_scopes_by_agent_id(data_dir):
    """A conversation had with Claude has nothing to do with Gemini's own
    list, the same way it has nothing to do with a different scene's."""
    claude = store.StoredConversation.new(title="With Claude", agent_id="claude-acp")
    gemini = store.StoredConversation.new(title="With Gemini", agent_id="gemini")
    store.save([claude, gemini])

    assert [c.title for c in store.load(agent_id="claude-acp")] == ["With Claude"]
    assert [c.title for c in store.load(agent_id="gemini")] == ["With Gemini"]
    assert len(store.load()) == 2, "unfiltered load must still see everything"


def test_unscoped_count_combines_missing_scene_and_missing_agent(data_dir):
    """One count for either historical gap, not two — see the docstring:
    a single combined note is what the panel shows for both at once."""
    no_cwd = store.StoredConversation.new(title="no cwd", agent_id="claude-acp")
    no_agent = store.StoredConversation.new(title="no agent", cwd="/tmp")
    both = store.StoredConversation.new(title="both", cwd="/tmp", agent_id="claude-acp")
    store.save([no_cwd, no_agent, both])

    assert store.unscoped_count() == 2, "a conversation scoped to both fields must not count"

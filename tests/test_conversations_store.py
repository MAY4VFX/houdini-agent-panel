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

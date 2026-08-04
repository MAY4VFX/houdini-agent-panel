"""Conversations that outlive the agent and Houdini itself.

The panel used to lose every conversation the moment the agent changed, and
all of them when Houdini closed. That came from conflating two different
things:

* an **agent session id** belongs to one specific agent process. Claude's id
  means nothing to Codex, and nothing at all after that process exits;
* a **conversation** is what the artist wrote and read. It belongs to them.

So a conversation gets its own id, kept here, and the agent session id is
merely the transport it is currently riding on. Switching agents drops the
transport and keeps the conversation; the next message opens a fresh agent
session under the same conversation.

Continuing a conversation with a DIFFERENT agent does not carry the model's
memory across — no protocol can do that. The transcript stays readable, and
the new agent starts from what it is told. Pretending otherwise would be
worse than losing it.

Storage is one JSON file, written atomically, capped in both count and size:
this sits in an artist's data folder, not a database, and a transcript that
grows without limit would eventually cost more to load than it is worth.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths

STORE_FILE_NAME = "conversations.json"
STORE_VERSION = 1

#: How many conversations to keep. Older ones fall off the end.
MAX_CONVERSATIONS = 50
#: How many feed entries to keep per conversation.
MAX_ENTRIES = 400


@dataclass
class StoredConversation:
    """One conversation as it survives on disk."""

    id: str
    title: str = "New chat"
    created_at: float = 0.0
    updated_at: float = 0.0
    pinned: bool = False
    #: Which agent it was last spoken to. Shown to the artist, never used to
    #: resume: a session id from a dead process is not a resource.
    agent_id: str = ""
    #: The scene directory this conversation belongs to (`$HIP`). Talking to
    #: an agent about one shot has nothing to do with the next shot, so the
    #: drawer shows only the conversations of the scene that is open — the
    #: same scoping a terminal agent gets for free by being started in a
    #: directory. Empty means it predates this field; see `load`.
    cwd: str = ""
    entries: list[dict] = field(default_factory=list)

    @staticmethod
    def new(
        title: str = "New chat", agent_id: str = "", cwd: str = ""
    ) -> "StoredConversation":
        now = time.time()
        return StoredConversation(
            id=uuid.uuid4().hex,
            title=title,
            created_at=now,
            updated_at=now,
            agent_id=agent_id,
            cwd=cwd,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "pinned": self.pinned,
            "agent_id": self.agent_id,
            "cwd": self.cwd,
            "entries": self.entries[-MAX_ENTRIES:],
        }

    @classmethod
    def from_dict(cls, payload: Any) -> "StoredConversation | None":
        if not isinstance(payload, dict):
            return None
        conversation_id = payload.get("id")
        if not isinstance(conversation_id, str) or not conversation_id:
            return None
        entries = payload.get("entries")
        return cls(
            id=conversation_id,
            title=str(payload.get("title") or "New chat"),
            created_at=float(payload.get("created_at") or 0.0),
            updated_at=float(payload.get("updated_at") or 0.0),
            pinned=bool(payload.get("pinned")),
            agent_id=str(payload.get("agent_id") or ""),
            cwd=str(payload.get("cwd") or ""),
            entries=[e for e in (entries or []) if isinstance(e, dict)][-MAX_ENTRIES:],
        )


def store_path() -> Path:
    return paths.data_dir() / STORE_FILE_NAME


def load(cwd: str | None = None) -> list[StoredConversation]:
    """Read the conversations. A broken file costs history, never the panel.

    Same discipline as `settings.load`: the file moves aside and the artist
    gets an empty list instead of a stack trace on open.

    `cwd` narrows the result to one scene directory. Everything is kept in
    one file — the scoping is a filter, not a separate store — so `save`
    can still write the whole set back without needing to know about the
    conversations belonging to scenes that aren't open.

    Conversations written before `cwd` existed have none, and are left out
    of every scoped result rather than shown in all of them: showing them
    everywhere is exactly the behaviour being fixed. They stay in the file,
    and `unscoped_count` says how many, so the panel can tell an artist
    where their old history went instead of appearing to have eaten it.
    """
    payload = _read_payload()
    if payload is None:
        return []
    raw = payload.get("conversations") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    result = [c for c in (StoredConversation.from_dict(item) for item in raw) if c is not None]
    if cwd is not None:
        result = [c for c in result if c.cwd == cwd]
    return _ordered(result)


def unscoped_count() -> int:
    """How many stored conversations predate scene scoping (no `cwd`)."""
    return sum(1 for c in load() if not c.cwd)


def load_active_id() -> str | None:
    """Which conversation was the current one (the open tab) when this was
    last saved, so the panel can restore the SAME conversation on top,
    rather than just something-or-other from the ordered list.

    Ordering alone (`load()`) can't stand in for this: `_persist_conversations`
    in `ui/panel.py` bumps `updated_at` for every live conversation with
    unsaved changes, not just the one on screen, so more than one entry can
    tie for "most recent". `None` means either nothing was ever recorded, or
    the file is unreadable — the caller's fallback is the first entry of
    `load()`'s already-ordered list, not an error.
    """
    payload = _read_payload()
    if payload is None:
        return None
    active_id = payload.get("active_id") if isinstance(payload, dict) else None
    return active_id if isinstance(active_id, str) and active_id else None


def _read_payload() -> dict | None:
    target = store_path()
    if not target.exists():
        return None
    try:
        payload = json.loads(target.read_text("utf-8"))
    except (OSError, ValueError):
        try:
            target.replace(target.with_suffix(target.suffix + ".broken"))
        except OSError:
            pass
        return None
    return payload if isinstance(payload, dict) else None


def save(conversations: list[StoredConversation], *, active_id: str | None = None) -> None:
    """Write atomically, keeping the newest `MAX_CONVERSATIONS`.

    Pinned ones are never trimmed away: pinning is the artist saying this one
    matters, and silently dropping it would make the pin a lie.

    `active_id` is the conversation currently on screen (the open tab), if
    any — kept alongside the list so a later `load_active_id()` can restore
    the SAME conversation rather than guessing from recency. Omitted (the
    default `None`) simply means "nothing to remember this time", not "there
    is no active conversation" — callers that don't track a current
    conversation yet can keep calling `save()` exactly as before.
    """
    ordered = _ordered(list(conversations))
    pinned = [c for c in ordered if c.pinned]
    rest = [c for c in ordered if not c.pinned][: max(0, MAX_CONVERSATIONS - len(pinned))]
    keep = _ordered(pinned + rest)

    target = store_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    payload = {
        "version": STORE_VERSION,
        "active_id": active_id,
        "conversations": [c.to_dict() for c in keep],
    }
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
    os.replace(tmp, target)


def _ordered(conversations: list[StoredConversation]) -> list[StoredConversation]:
    """Pinned first, then most recently touched."""
    return sorted(conversations, key=lambda c: (not c.pinned, -c.updated_at))

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
    title: str = "New conversation"
    created_at: float = 0.0
    updated_at: float = 0.0
    pinned: bool = False
    #: Which agent it was last spoken to. Shown to the artist, never used to
    #: resume: a session id from a dead process is not a resource.
    agent_id: str = ""
    entries: list[dict] = field(default_factory=list)

    @staticmethod
    def new(title: str = "New conversation", agent_id: str = "") -> "StoredConversation":
        now = time.time()
        return StoredConversation(
            id=uuid.uuid4().hex, title=title, created_at=now, updated_at=now, agent_id=agent_id
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "pinned": self.pinned,
            "agent_id": self.agent_id,
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
            title=str(payload.get("title") or "New conversation"),
            created_at=float(payload.get("created_at") or 0.0),
            updated_at=float(payload.get("updated_at") or 0.0),
            pinned=bool(payload.get("pinned")),
            agent_id=str(payload.get("agent_id") or ""),
            entries=[e for e in (entries or []) if isinstance(e, dict)][-MAX_ENTRIES:],
        )


def store_path() -> Path:
    return paths.data_dir() / STORE_FILE_NAME


def load() -> list[StoredConversation]:
    """Read the conversations. A broken file costs history, never the panel.

    Same discipline as `settings.load`: the file moves aside and the artist
    gets an empty list instead of a stack trace on open.
    """
    target = store_path()
    if not target.exists():
        return []
    try:
        payload = json.loads(target.read_text("utf-8"))
    except (OSError, ValueError):
        try:
            target.replace(target.with_suffix(target.suffix + ".broken"))
        except OSError:
            pass
        return []

    raw = payload.get("conversations") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    result = [c for c in (StoredConversation.from_dict(item) for item in raw) if c is not None]
    return _ordered(result)


def save(conversations: list[StoredConversation]) -> None:
    """Write atomically, keeping the newest `MAX_CONVERSATIONS`.

    Pinned ones are never trimmed away: pinning is the artist saying this one
    matters, and silently dropping it would make the pin a lie.
    """
    ordered = _ordered(list(conversations))
    pinned = [c for c in ordered if c.pinned]
    rest = [c for c in ordered if not c.pinned][: max(0, MAX_CONVERSATIONS - len(pinned))]
    keep = _ordered(pinned + rest)

    target = store_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    payload = {"version": STORE_VERSION, "conversations": [c.to_dict() for c in keep]}
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
    os.replace(tmp, target)


def _ordered(conversations: list[StoredConversation]) -> list[StoredConversation]:
    """Pinned first, then most recently touched."""
    return sorted(conversations, key=lambda c: (not c.pinned, -c.updated_at))

"""The feed model — plain Python, no Qt.

Kept separate from rendering (`ui/transcript.py`) so the logic that
assembles the feed from the `session/update` stream can be tested without a
`QApplication` (see docs/architecture.md §8, §11). Nothing here imports
either Qt or `acp`: input objects (chunks, `ToolCall*`, `PlanEntry`) are
read via `getattr`, so the model works identically with the agent's real
pydantic models and with plain stubs in tests.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

EntryKind = Literal[
    "user", "activity", "agent", "thought", "tool", "plan", "permission", "error"
]

#: The feed has one plan per session — the protocol sends it in full on
#: every update (`Plan.entries` is the complete list, not a delta), so the
#: feed entry gets one and the same fixed id.
_PLAN_ENTRY_ID = "plan"


@dataclass
class PlanEntry:
    content: str
    priority: str
    status: str


@dataclass
class ToolCallView:
    tool_call_id: str
    title: str
    kind: str  # ToolKind, "other" if the agent didn't send one
    status: str  # pending | in_progress | completed | failed
    content: list[dict] = field(default_factory=list)
    locations: list[dict] = field(default_factory=list)


@dataclass
class PermissionView:
    request_key: str
    tool_title: str
    options: list[tuple[str, str, str]]  # (option_id, name, kind)
    answered: str | None = None


@dataclass
class ActivityView:
    started_at: float
    finished_at: float | None = None


@dataclass
class Entry:
    kind: EntryKind
    id: str  # message_id / tool_call_id / uuid
    text: str = ""
    tool: ToolCallView | None = None
    plan: list[PlanEntry] = field(default_factory=list)
    permission: PermissionView | None = None
    activity: ActivityView | None = None
    #: The non-text blocks the artist sent along with this line — the same
    #: dicts that went to the agent (`image`/`resource`/`audio`). Only ever
    #: set on a `user` entry: an attachment belongs to the message it was
    #: attached to, and showing it anywhere else would be a lie about what
    #: was sent.
    attachments: list[dict] = field(default_factory=list)


def _plain(value: Any) -> Any:
    """The agent's pydantic model -> dict, everything else passes through as-is.

    `ToolCallContent` variants (`ContentToolCallContent`/`FileEditToolCallContent`/
    `TerminalToolCallContent`) arrive as pydantic objects; a plain dict is
    enough for the feed to render, there's no reason to tie this to
    specific ACP classes.
    """
    if value is None:
        return None
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(exclude_none=True)
    return value


def _plain_list(values: Any) -> list[dict]:
    return [_plain(item) for item in (values or [])]


def _attachment_record(block: dict) -> dict:
    """An attachment as it goes to disk — everything except the payload.

    A sent image is a base64 blob of the whole file; a scene reference can
    be tens of megabytes. Writing that into `conversations.json` on every
    autosave would turn a folder of chat history into a folder of pictures,
    and a restored conversation is a read-only replay — nothing can resend
    those bytes anyway. What survives is what the artist needs to recognise
    the message later: what kind of thing it was and what it was called.
    """
    record = {"type": str(block.get("type") or "")}
    uri = block.get("uri") or (block.get("resource") or {}).get("uri")
    if uri:
        record["uri"] = str(uri)
    mime = block.get("mimeType") or (block.get("resource") or {}).get("mimeType")
    if mime:
        record["mimeType"] = str(mime)
    return record


#: Marks an entry built from chunks that carried no `messageId`, so a
#: following chunk can tell "keep appending to this" from "this belongs to
#: a message the agent actually named".
_UNKEYED_PREFIX = "unkeyed:"


class TranscriptModel:
    """Folds the session/update stream into a list of Entry.

    Chunks sharing a message_id are stitched into a single entry —
    otherwise the feed turns into a hundred one-letter paragraphs.
    tool_call_update finds the entry by tool_call_id and patches only the
    fields that arrived (None = "unchanged"). plan replaces the previous
    plan wholesale (the protocol sends the full list).
    """

    def __init__(self) -> None:
        self._entries: list[Entry] = []
        # Indexes by id — so streaming stitches together and updates find
        # their entry in O(1), instead of scanning the whole feed on every
        # chunk. The message index is keyed by `(kind, message_id)`: see
        # `apply_chunk` for the agent that reuses one id for both streams.
        self._by_message_id: dict[tuple[str, str], Entry] = {}
        self._by_tool_call_id: dict[str, Entry] = {}
        self._by_request_key: dict[str, Entry] = {}
        self._plan_entry: Entry | None = None
        self._active_activity: Entry | None = None

    # --- appending -----------------------------------------------------

    def append_user(self, text: str, attachments: list[dict] | None = None) -> Entry:
        entry = Entry(
            kind="user",
            id=str(uuid.uuid4()),
            text=text,
            attachments=[a for a in (attachments or []) if isinstance(a, dict)],
        )
        self._entries.append(entry)
        return entry

    def start_activity(self) -> Entry:
        activity = ActivityView(started_at=time.monotonic())
        entry = Entry(kind="activity", id=str(uuid.uuid4()), activity=activity)
        self._active_activity = entry
        self._entries.append(entry)
        return entry

    def finish_activity(self) -> Entry | None:
        entry = self._active_activity
        if entry is None or entry.activity is None:
            return None
        entry.activity.finished_at = time.monotonic()
        self._active_activity = None
        return entry

    def apply_chunk(self, message_id: str, text: str, *, thought: bool = False) -> Entry:
        kind: EntryKind = "thought" if thought else "agent"

        if message_id:
            # Keyed by KIND AND id, and the entry's own id carries the kind
            # too. `messageId` identifies the agent's message, not one
            # stream within it: opencode sends its reasoning and its answer
            # under the SAME id (measured — reasoning arrived, the answer
            # never appeared on screen). Two entries then shared one id, and
            # `TranscriptView._refresh_one` resolves an id by taking the
            # FIRST entry that carries it — so every chunk of the answer was
            # rendered into the thought's row and the answer itself stayed
            # invisible until the feed happened to be rebuilt from scratch.
            key = (kind, message_id)
            existing = self._by_message_id.get(key)
            if existing is not None:
                existing.text += text
                return existing
            entry = Entry(kind=kind, id=f"{kind}:{message_id}", text=text)
            self._by_message_id[key] = entry
        else:
            # No message_id at all. `messageId` is optional in ACP, and Grok
            # omits it on every chunk — which used to mean one entry per
            # word, an answer shredded down the page one line at a time.
            #
            # Consecutive chunks of the same kind with no id belong to the
            # same message: nothing else could have come between them,
            # because anything else (a tool call, a plan, the artist's own
            # line) appends its own entry and ends the run.
            last = self._entries[-1] if self._entries else None
            if last is not None and last.kind == kind and last.id.startswith(_UNKEYED_PREFIX):
                last.text += text
                return last
            entry = Entry(kind=kind, id=_UNKEYED_PREFIX + str(uuid.uuid4()), text=text)

        self._entries.append(entry)
        return entry

    def apply_tool_call(self, call: Any) -> Entry:
        tool_call_id = call.tool_call_id
        view = ToolCallView(
            tool_call_id=tool_call_id,
            title=call.title,
            kind=getattr(call, "kind", None) or "other",
            status=getattr(call, "status", None) or "pending",
            content=_plain_list(getattr(call, "content", None)),
            locations=_plain_list(getattr(call, "locations", None)),
        )
        entry = Entry(kind="tool", id=tool_call_id, tool=view)
        self._by_tool_call_id[tool_call_id] = entry
        self._entries.append(entry)
        return entry

    def apply_tool_update(self, update: Any) -> Entry | None:
        entry = self._by_tool_call_id.get(update.tool_call_id)
        if entry is None or entry.tool is None:
            # An update for a call the feed never saw (e.g. we picked up a
            # session midway through) — nothing to patch.
            return None

        view = entry.tool
        if getattr(update, "title", None) is not None:
            view.title = update.title
        if getattr(update, "kind", None) is not None:
            view.kind = update.kind
        if getattr(update, "status", None) is not None:
            view.status = update.status
        if getattr(update, "content", None) is not None:
            view.content = _plain_list(update.content)
        if getattr(update, "locations", None) is not None:
            view.locations = _plain_list(update.locations)
        return entry

    def apply_plan(self, entries: Any) -> Entry:
        plan = [
            PlanEntry(content=item.content, priority=item.priority, status=item.status)
            for item in entries
        ]
        if self._plan_entry is None:
            self._plan_entry = Entry(kind="plan", id=_PLAN_ENTRY_ID, plan=plan)
            self._entries.append(self._plan_entry)
        else:
            self._plan_entry.plan = plan
        return self._plan_entry

    def apply_permission(self, view: PermissionView) -> Entry:
        entry = Entry(kind="permission", id=view.request_key, permission=view)
        self._by_request_key[view.request_key] = entry
        self._entries.append(entry)
        return entry

    def resolve_permission(self, request_key: str, option_id: str | None) -> Entry | None:
        entry = self._by_request_key.get(request_key)
        if entry is None or entry.permission is None:
            return None
        # "" means cancelled (the same contract as
        # ui/transcript.py::permission_answered), distinct from None, which
        # means "not answered yet".
        entry.permission.answered = option_id if option_id is not None else ""
        return entry

    def append_error(self, text: str) -> Entry:
        entry = Entry(kind="error", id=str(uuid.uuid4()), text=text)
        self._entries.append(entry)
        return entry

    # --- persistence ------------------------------------------------------
    #
    # Only text survives a restart. Tool calls, plans and permission requests
    # are live state belonging to an agent process that no longer exists —
    # restoring a permission prompt nobody can answer, or a tool call frozen
    # at "in progress", would be worse than not restoring it. What the artist
    # reads back is the conversation, which is the part that was theirs.

    def to_records(self) -> list[dict]:
        records: list[dict] = []
        for entry in self._entries:
            if entry.kind not in ("user", "agent", "error"):
                continue
            if not entry.text and not entry.attachments:
                continue
            record = {"kind": entry.kind, "id": entry.id, "text": entry.text}
            if entry.attachments:
                record["attachments"] = [_attachment_record(a) for a in entry.attachments]
            records.append(record)
        return records

    def load_records(self, records: list[dict]) -> None:
        self._entries = [
            Entry(
                kind=record.get("kind", "agent"),
                id=str(record.get("id") or uuid.uuid4()),
                text=str(record.get("text") or ""),
                attachments=[
                    a for a in (record.get("attachments") or []) if isinstance(a, dict)
                ],
            )
            for record in records or []
            if isinstance(record, dict) and (record.get("text") or record.get("attachments"))
        ]
        self._by_message_id.clear()
        self._by_tool_call_id.clear()

    def entries(self) -> list[Entry]:
        return list(self._entries)

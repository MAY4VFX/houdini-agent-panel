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

from .logbook import logger as _logbook_logger

_log = _logbook_logger("houdini_agent_panel.transcript_model")

EntryKind = Literal[
    "user", "activity", "agent", "thought", "tool", "plan", "permission",
    "error", "note", "queued",
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
    #: set on a `user` entry, including its `queued` stage before the turn
    #: comes (`queue_message` sets it too; `promote_queued` flips the kind
    #: in place on the same `Entry`, so the field carries straight through)
    #: — an attachment belongs to the message it was attached to, and
    #: showing it anywhere else would be a lie about what was sent.
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

#: Below this length, a chunk that happens to start with the text
#: accumulated so far is more likely a coincidence than a resend — see
#: `_is_repeated_message`.
_REPEAT_GUARD_MIN_LEN = 12


def _is_repeated_message(accumulated: str, chunk: str) -> bool:
    """True when `chunk` looks like the WHOLE message so far, sent again,
    rather than the next delta to append.

    Measured cause: `@agentclientprotocol/claude-agent-acp` streams chunks
    as usual, then re-emits the full consolidated message under the SAME
    `message_id` once its own streamed-content bookkeeping (`streamedBlocks`
    in the adapter's own source) is reset on activation — see
    docs/facts/acp-sdk.md. The panel used to append the resend onto what it
    already had, so the feed showed the tail of the message glued onto a
    duplicate of its own start.

    A real delta only ever adds characters at the end — it has no reason to
    restate the beginning — so "the new chunk starts with everything
    accumulated so far" is a reliable tell once the accumulated text is
    long enough that matching it by coincidence is implausible. Below
    `_REPEAT_GUARD_MIN_LEN` that stops being true: a short accumulated
    fragment (a letter or two) is often the literal start of a perfectly
    ordinary, unrelated next word — e.g. accumulated "Да" is a genuine
    prefix of the real continuation "Давай посмотрим" — so the guard sits
    out and lets the normal append happen instead of misfiring and
    swallowing real content.
    """
    if len(accumulated) < _REPEAT_GUARD_MIN_LEN:
        return False
    return chunk.startswith(accumulated)


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

    def queue_message(
        self, entry_id: str, text: str, attachments: list[dict] | None = None
    ) -> Entry:
        """A message the artist sent while a turn was already running.

        Its own entry, not a deferred `append_user` — the feed has to show
        "this is waiting" as a distinct fact, at the position it will
        actually occupy once sent (`ui/panel.py::_on_enqueue_requested`
        appends it right after whatever entry exists when it's typed, same
        as a live send would). `promote_queued` is what turns it into an
        ordinary sent message once its turn comes — same `Entry`, so
        `attachments` set here carries straight through: the artist attached
        a picture, the message is waiting to go out, and it has to look like
        what it is about to become.
        """
        entry = Entry(
            kind="queued",
            id=entry_id,
            text=text,
            attachments=[a for a in (attachments or []) if isinstance(a, dict)],
        )
        self._entries.append(entry)
        return entry

    def promote_queued(self, entry_id: str) -> Entry | None:
        """A queued message's turn has come — same entry, same position in
        the feed, just no longer waiting. Mutated in place rather than
        removed and re-appended: with more than one message queued, the
        others are still sitting right after this one in send order, and
        re-appending would jump this one past them."""
        for entry in self._entries:
            if entry.id == entry_id and entry.kind == "queued":
                entry.kind = "user"
                return entry
        return None

    def remove_entry(self, entry_id: str) -> bool:
        """Drop an entry outright — today, only ever a queued message the
        artist pulled back before it was sent (`ui/panel.py::_on_queue_
        remove_requested`). Nothing else in the feed is ever taken back."""
        for index, entry in enumerate(self._entries):
            if entry.id == entry_id:
                del self._entries[index]
                return True
        return False

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
            # under the SAME id (measured — the reasoning arrived, the
            # answer never appeared on screen). Two entries then shared one
            # id, and `TranscriptView._refresh_one` resolves an id by taking
            # the FIRST entry that carries it — so every chunk of the answer
            # was rendered into the thought's row and the answer itself
            # stayed invisible until the feed happened to be rebuilt from
            # scratch.
            key = (kind, message_id)
            existing = self._by_message_id.get(key)
            if existing is not None:
                if _is_repeated_message(existing.text, text):
                    _log.info(
                        "chunk repeated the accumulated text, replaced instead of "
                        "appended (kind=%s, accumulated=%d chars, chunk=%d chars)",
                        kind, len(existing.text), len(text),
                    )
                    existing.text = text
                else:
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
        """A genuine problem — something failed, or needs the artist's
        attention to move forward. For the panel's own routine status
        commentary ("Agent stopped.", a connection banner, "Signed in.")
        use `append_note` instead — see its own docstring for why the two
        must not share a kind."""
        entry = Entry(kind="error", id=str(uuid.uuid4()), text=text)
        self._entries.append(entry)
        return entry

    def append_note(self, text: str) -> Entry:
        """The panel talking about itself — a connection banner, "Agent
        stopped.", "Signed in.", "N conversation(s) aren't shown here" —
        never something that failed.

        Reported for real, from an owner's own store: 408 of 570 persisted
        entries across 43 conversations were `kind="error"`, and the ones
        sampled were exactly this shape ("Preparing Claude Agent…", "Agent
        stopped.") — `ui/panel.py::_note` used to route EVERY one of its
        37 call sites through `append_error`, with no separate kind to
        route the merely informational ones to instead. Restored history
        rendered every one of them bold, identically to a genuine failure
        sitting right next to it. `append_error` stays for the call sites
        that really are reporting a failure — this is the other half.
        """
        entry = Entry(kind="note", id=str(uuid.uuid4()), text=text)
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
        # "queued" is in here on purpose: a message the artist typed while
        # busy is exactly as much theirs as one they typed while idle, and
        # a hang that loses it is the same bug as the one that motivated
        # persisting a prompt the instant it exists at all (`ui/panel.py::
        # _persist_conversations_soon`). Only the text round-trips in full,
        # same as "user"/"agent"/"error"/"note" — an attachment survives as
        # its stripped record (`_attachment_record`: kind and name, never
        # the payload), not as the block needed to actually resend it;
        # `ui/panel.py::_restore_conversations` rebuilds a plain-text-only
        # block from this for whatever was still queued.
        records: list[dict] = []
        for entry in self._entries:
            if entry.kind not in ("user", "agent", "error", "note", "queued"):
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

"""Модель ленты — чистый Python, без Qt.

Отделено от отрисовки (`ui/transcript.py`), чтобы логику сборки ленты из
потока `session/update` можно было тестировать без `QApplication` (см.
docs/architecture.md §8, §11). Ничего отсюда не импортирует ни Qt, ни `acp`:
входные объекты (чанки, `ToolCall*`, `PlanEntry`) читаются через `getattr`,
так что модель одинаково работает и с настоящими pydantic-моделями агента, и
с простыми заглушками в тестах.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

EntryKind = Literal[
    "user", "activity", "agent", "thought", "tool", "plan", "permission", "error"
]

#: У ленты один план на сессию — протокол шлёт его целиком при каждом
#: обновлении (`Plan.entries` — полный список, не дельта), так что и у записи
#: в ленте один и тот же фиксированный id.
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
    kind: str  # ToolKind, "other" если агент не прислал
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


def _plain(value: Any) -> Any:
    """Pydantic-модель агента -> dict, всё остальное — как есть.

    `ToolCallContent`-варианты (`ContentToolCallContent`/`FileEditToolCallContent`/
    `TerminalToolCallContent`) приходят как pydantic-объекты; ленте достаточно
    произвольного dict для отрисовки, привязываться к конкретным классам ACP
    здесь незачем.
    """
    if value is None:
        return None
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(exclude_none=True)
    return value


def _plain_list(values: Any) -> list[dict]:
    return [_plain(item) for item in (values or [])]


class TranscriptModel:
    """Складывает поток session/update в список Entry.

    Чанки с одним message_id склеиваются в одну запись — иначе лента
    превращается в сотню однобуквенных абзацев. tool_call_update находит
    запись по tool_call_id и патчит только пришедшие поля (None = «не
    менялось»). plan заменяет предыдущий план целиком (протокол шлёт полный
    список).
    """

    def __init__(self) -> None:
        self._entries: list[Entry] = []
        # Индексы по id — чтобы стриминг склеивался и обновления находили
        # свою запись за O(1), не сканируя всю ленту на каждый чанк.
        self._by_message_id: dict[str, Entry] = {}
        self._by_tool_call_id: dict[str, Entry] = {}
        self._by_request_key: dict[str, Entry] = {}
        self._plan_entry: Entry | None = None
        self._active_activity: Entry | None = None

    # --- добавление -----------------------------------------------------

    def append_user(self, text: str) -> Entry:
        entry = Entry(kind="user", id=str(uuid.uuid4()), text=text)
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
            existing = self._by_message_id.get(message_id)
            if existing is not None and existing.kind == kind:
                existing.text += text
                return existing
            entry = Entry(kind=kind, id=message_id, text=text)
            self._by_message_id[message_id] = entry
        else:
            # Без message_id склеивать не с чем — агент прислал одноразовый
            # чанк, каждый такой становится своей записью.
            entry = Entry(kind=kind, id=str(uuid.uuid4()), text=text)

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
            # Обновление на вызов, которого лента не видела (например, начало
            # сессии подхватили с середины) — патчить нечего.
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
        # "" — отменено (тот же контракт, что у ui/transcript.py::permission_answered),
        # отличимо от None, означающего "ещё не отвечено".
        entry.permission.answered = option_id if option_id is not None else ""
        return entry

    def append_error(self, text: str) -> Entry:
        entry = Entry(kind="error", id=str(uuid.uuid4()), text=text)
        self._entries.append(entry)
        return entry

    def entries(self) -> list[Entry]:
        return list(self._entries)

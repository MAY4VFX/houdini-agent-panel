"""Пул сессий на одном ACP-соединении.

Один `SessionPool` на процесс Houdini (модуль-синглтон `pool()`) — второй таб
панели обязан видеть тот же список сессий и тот же живой процесс агента: два
таба, один `AcpClient`, один процесс агента, разные `current` (см.
docs/architecture.md §7).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .transcript_model import Entry
from .ui.qt import QtCore, Signal


@dataclass
class SessionMode:
    id: str
    name: str
    description: str = ""


@dataclass
class AvailableCommand:
    name: str
    description: str = ""
    hint: str = ""


@dataclass
class Usage:
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class SessionState:
    session_id: str
    title: str  # первая строка первого промпта, иначе «Новый разговор»
    cwd: str
    created_at: float
    current_mode_id: str | None = None
    available_modes: list[SessionMode] = field(default_factory=list)
    available_commands: list[AvailableCommand] = field(default_factory=list)
    entries: list[Entry] = field(default_factory=list)  # лента, см. §8
    usage: Usage | None = None
    busy: bool = False


class SessionPool(QtCore.QObject):
    """Живёт на главном потоке, хранит состояния всех открытых сессий.

    Сам по себе ничего не знает про ACP — `AcpClient` наполняет его через
    сигналы (`session_started`, `message_chunk`, ...), панель читает через
    `get`/`all`/`current`. Разделение так же, как `transcript_model.py`:
    здесь только состояние, отрисовка — в `ui/`.
    """

    added = Signal(str)
    removed = Signal(str)
    changed = Signal(str)
    current_changed = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._states: dict[str, SessionState] = {}
        # Порядок вставки важен для UI (список сессий сверху) — обычный dict
        # в Python 3.7+ его и так хранит, но полагаться на это неявно не
        # хочется, поэтому дублируем явным списком id.
        self._order: list[str] = []
        self._current_id: str | None = None

    def add(self, state: SessionState) -> None:
        is_new = state.session_id not in self._states
        self._states[state.session_id] = state
        if is_new:
            self._order.append(state.session_id)
        if self._current_id is None:
            self._current_id = state.session_id
        if is_new:
            self.added.emit(state.session_id)
        else:
            self.changed.emit(state.session_id)

    def get(self, session_id: str) -> SessionState | None:
        return self._states.get(session_id)

    def all(self) -> list[SessionState]:
        return [self._states[sid] for sid in self._order]

    def current(self) -> SessionState | None:
        if self._current_id is None:
            return None
        return self._states.get(self._current_id)

    def set_current(self, session_id: str) -> None:
        if session_id not in self._states or session_id == self._current_id:
            return
        self._current_id = session_id
        self.current_changed.emit(session_id)

    def remove(self, session_id: str) -> None:
        if session_id not in self._states:
            return
        del self._states[session_id]
        self._order.remove(session_id)
        if self._current_id == session_id:
            self._current_id = self._order[-1] if self._order else None
        self.removed.emit(session_id)
        if self._current_id is not None:
            self.current_changed.emit(self._current_id)

    def mark_changed(self, session_id: str) -> None:
        """Состояние сессии поменяли снаружи (та же ссылка) — просто уведомить."""
        if session_id in self._states:
            self.changed.emit(session_id)


_pool: SessionPool | None = None


def pool() -> SessionPool:
    """Синглтон на процесс Houdini.

    Не потокобезопасно намеренно: `SessionPool` — `QObject`, живёт и
    создаётся на главном потоке, как и весь остальной UI-код панели.
    """
    global _pool
    if _pool is None:
        _pool = SessionPool()
    return _pool


def reset_pool_for_tests() -> None:
    """Только для тестов: синглтон переживает между тестами иначе."""
    global _pool
    _pool = None

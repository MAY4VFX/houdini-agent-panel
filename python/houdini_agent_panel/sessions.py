"""Session pool on top of a single ACP connection.

One `SessionPool` per Houdini process (the module-level singleton `pool()`)
— a second panel tab must see the same session list and the same live agent
process: two tabs, one `AcpClient`, one agent process, different `current`
(see docs/architecture.md §7).
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
    title: str  # the first line of the first prompt, otherwise "New conversation"
    cwd: str
    created_at: float
    current_mode_id: str | None = None
    available_modes: list[SessionMode] = field(default_factory=list)
    available_commands: list[AvailableCommand] = field(default_factory=list)
    entries: list[Entry] = field(default_factory=list)  # the feed, see §8
    usage: Usage | None = None
    busy: bool = False


class SessionPool(QtCore.QObject):
    """Lives on the main thread, holds the state of every open session.

    Knows nothing about ACP by itself — `AcpClient` fills it in via signals
    (`session_started`, `message_chunk`, ...), the panel reads through
    `get`/`all`/`current`. The split is the same as `transcript_model.py`:
    only state lives here, rendering lives in `ui/`.
    """

    added = Signal(str)
    removed = Signal(str)
    changed = Signal(str)
    current_changed = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._states: dict[str, SessionState] = {}
        # Insertion order matters for the UI (session list on top) — a
        # plain dict in Python 3.7+ preserves it anyway, but we don't want
        # to rely on that implicitly, so we duplicate it with an explicit
        # id list.
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
        """The session's state was changed externally (same reference) — just notify."""
        if session_id in self._states:
            self.changed.emit(session_id)


_pool: SessionPool | None = None


def pool() -> SessionPool:
    """Singleton per Houdini process.

    Deliberately not thread-safe: `SessionPool` is a `QObject`, created and
    living on the main thread, like the rest of the panel's UI code.
    """
    global _pool
    if _pool is None:
        _pool = SessionPool()
    return _pool


def reset_pool_for_tests() -> None:
    """Tests only: the singleton would otherwise survive between tests."""
    global _pool
    _pool = None

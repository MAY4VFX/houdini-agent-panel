"""Session pool on top of an ACP connection.

One `SessionPool` per agent id (the module-level `pool(agent_id)`), not one
for the whole Houdini process — a second panel tab on the SAME agent must
see the same session list and the same live agent process, but a tab on a
DIFFERENT agent gets its own: two tabs on one agent, one `AcpClient`, one
process, different `current` (see docs/architecture.md §7 and
`ui/panel.py::AgentPanel._agent_id`).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .transcript_model import Entry, TranscriptModel
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


@dataclass
class QueuedMessage:
    """A message typed while this session's turn was already running.

    Lives on the `SessionState` it was typed into, not on the panel or the
    agent process — a queue is a fact about ONE conversation, the same way
    `entries`/`busy` already are. `blocks` are the real ACP content blocks,
    ready to send unchanged once this conversation's turn comes
    (`ui/panel.py::_drain_queue`, which sends everything still queued at
    that moment together, in one `session/prompt` call — not one call per
    queued message); `id` matches the `queued`-kind `transcript_model.
    Entry` shown for it, so promoting or removing one finds the other.
    Also read outside a drain, by the arrow-key-history feature
    (`ui/panel.py::_build_history_candidates`) — an Up press in an empty
    field pulls the most recently queued one back out for editing.

    Only alive for as long as this process is: `blocks` (attachments
    included — a pasted image or a drag-and-dropped file queued behind a
    busy turn) is never written to `conversations_store` at all, only
    referenced by `ui/panel.py::_drain_queue` while the process is up.
    `transcript_model.Entry` — the thing that DOES get persisted — has no
    field for a block list, only `text`; a restart restores a queued
    message's TEXT (if it had any) as a plain `queued`-kind entry with
    nothing attached, and an attachment-only queued message (no text)
    leaves no record at all. Confirmed by reading, not assumed: `.blocks`
    has exactly one reader in the whole codebase
    (`ui/panel.py::_drain_queue`), never a writer into the store.
    """

    id: str
    blocks: list[dict]


@dataclass
class SessionState:
    session_id: str
    title: str  # the first line of the first prompt, otherwise "New chat"
    cwd: str
    created_at: float
    current_mode_id: str | None = None
    available_modes: list[SessionMode] = field(default_factory=list)
    available_commands: list[AvailableCommand] = field(default_factory=list)
    #: The agent's own settings for this session (model, reasoning effort,
    #: fast mode…) as `client.ConfigOption`. Kept per session because that's
    #: the scope ACP gives them, and because switching conversations has to
    #: restore the chips the artist saw last time.
    config_options: list = field(default_factory=list)
    entries: list[Entry] = field(default_factory=list)  # the feed, see §8
    usage: Usage | None = None
    busy: bool = False
    #: Something arrived while this conversation wasn't the one on screen.
    #: Cleared the moment it becomes current again (`AgentPanel._show_session`)
    #: — "read" means "the artist had it open," nothing more elaborate.
    unread: bool = False
    #: Messages typed while `busy` was already true, waiting their turn —
    #: drained together, oldest first, in one `session/prompt` call the
    #: moment the running turn finishes (`ui/panel.py::_drain_queue`). Per
    #: conversation like everything else here: switching to a different one
    #: must never show or send another conversation's still-typed words.
    queued: list[QueuedMessage] = field(default_factory=list)


class SessionPool(QtCore.QObject):
    """Lives on the main thread, holds the state of every open session.

    Knows nothing about ACP by itself — `AcpClient` fills it in via signals
    (`session_started`, `message_chunk`, ...), the panel reads through
    `get`/`all`. The split is the same as `transcript_model.py`: only state
    lives here, rendering lives in `ui/`.

    Deliberately has no notion of "current" session. That used to live here
    as one shared `_current_id`/`set_current()`/`current_changed` — which
    is exactly backwards from what this module's own docstring promises
    ("two tabs... different current"): a pool-wide field can only ever hold
    ONE current session, so picking a different conversation in one tab's
    drawer silently dragged every other open tab onto that same
    conversation too (issue #21). Which session is on screen is a fact
    about a *tab*, not about the pool — it lives on `AgentPanel` now
    (`_current_session_id`/`_set_current_session`), one per instance. The
    pool stays exactly what its docstring already said it should be: the
    session list and data every tab shares, nothing about which one any of
    them happens to be looking at.
    """

    added = Signal(str)
    removed = Signal(str)
    changed = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._states: dict[str, SessionState] = {}
        # Insertion order matters for the UI (session list on top) — a
        # plain dict in Python 3.7+ preserves it anyway, but we don't want
        # to rely on that implicitly, so we duplicate it with an explicit
        # id list.
        self._order: list[str] = []

    def add(self, state: SessionState) -> None:
        is_new = state.session_id not in self._states
        self._states[state.session_id] = state
        if is_new:
            self._order.append(state.session_id)
        if is_new:
            self.added.emit(state.session_id)
        else:
            self.changed.emit(state.session_id)

    def get(self, session_id: str) -> SessionState | None:
        return self._states.get(session_id)

    def all(self) -> list[SessionState]:
        return [self._states[sid] for sid in self._order]

    def clear(self) -> None:
        """Drop every session. Called when the agent process goes away.

        A session id is issued by one specific agent process and means
        nothing to any other. Keeping the list across an agent switch used to
        leave the panel convinced it still had a live conversation: it
        skipped creating a new session and then sent prompts carrying an id
        the new agent had never issued, which simply hung.
        """
        removed = list(self._order)
        self._states.clear()
        self._order.clear()
        for session_id in removed:
            self.removed.emit(session_id)

    def remove(self, session_id: str) -> None:
        if session_id not in self._states:
            return
        del self._states[session_id]
        self._order.remove(session_id)
        self.removed.emit(session_id)

    def mark_changed(self, session_id: str) -> None:
        """The session's state was changed externally (same reference) — just notify."""
        if session_id in self._states:
            self.changed.emit(session_id)


#: One pool per agent id, not one for the whole Houdini process. Two tabs
#: both talking to Claude share a session list and a process; a tab that
#: switches to Gemini gets Gemini's own list, not Claude's wiped out from
#: under a sibling tab still using it — the bug this replaced (see
#: `ui/panel.py`'s own docstring and `AgentPanel._agent_id`).
_pools: dict[str, SessionPool] = {}


def pool(agent_id: str) -> SessionPool:
    """The session pool for this one agent id, process-wide.

    Deliberately not thread-safe: `SessionPool` is a `QObject`, created and
    living on the main thread, like the rest of the panel's UI code.
    """
    if agent_id not in _pools:
        _pools[agent_id] = SessionPool()
    return _pools[agent_id]


#: Transcript content per session id, one dict per agent id — process-wide,
#: same reasoning as `_pools` immediately above, and not a smaller thing:
#: a session's transcript is a fact about the SESSION, exactly like the
#: `SessionState` in `_pools` is. Living on `AgentPanel` instead (as
#: `self._models`, before this) meant every tab attached to an agent built
#: its OWN copy from the client's broadcast signals — `session_started`,
#: `message_chunk`, ... reach every tab wired to that agent, not just
#: whichever one happens to be showing a given session (`ui/panel.py::
#: AgentPanel._wire_client`). A second, otherwise idle tab reacted to a
#: session it never asked for exactly like the tab that opened it, so two
#: tabs on one agent ended up with two different, incomplete transcripts
#: for the one live session — and, once both persisted, two different
#: `StoredConversation`s on disk for what was a single conversation.
#: Reported for real: 6 of an owner's 49 saved conversations were exactly
#: this, in three duplicate pairs.
_model_pools: dict[str, dict[str, TranscriptModel]] = {}

#: Which stored conversation id a session id belongs to, one dict per
#: agent id — same reasoning as `_model_pools` immediately above: this is
#: also a fact about the session, not the tab. Keeping it per-tab used to
#: let a second tab mint its OWN, different conversation id for a session
#: the first tab had already given one, which is the other half of the
#: duplicate-conversation bug `_model_pools` describes: even a session
#: whose transcript happened to end up identical in both tabs still got
#: saved twice, under two different ids.
_conversation_id_pools: dict[str, dict[str, str]] = {}


def models(agent_id: str) -> dict[str, TranscriptModel]:
    """This one agent id's transcripts, process-wide — see `_model_pools`."""
    return _model_pools.setdefault(agent_id, {})


def conversation_ids(agent_id: str) -> dict[str, str]:
    """This one agent id's session-id -> stored-conversation-id map,
    process-wide — see `_conversation_id_pools`."""
    return _conversation_id_pools.setdefault(agent_id, {})


def reset_pool_for_tests() -> None:
    """Tests only: the singletons would otherwise survive between tests."""
    global _pools, _model_pools, _conversation_id_pools
    _pools = {}
    _model_pools = {}
    _conversation_id_pools = {}

"""A conversation is a fact about a SESSION, not about a tab.

Reported for real, from the owner's own store: 6 of 49 conversations were
exact duplicates in three pairs — same title, overlapping message ids, one
copy with the full transcript and one with only part of it. The cause: a
live session is shared by every tab attached to the same agent (one
process, one `SessionPool`, per `docs/design.md`'s "One agent process per
agent id, many sessions") — but `AgentPanel._models`/`_conversation_ids`
used to live on the TAB (`self._models`, `self._conversation_ids`), not on
the session. `client.session_started`/`message_chunk`/... are Qt signals
the shared client broadcasts to EVERY tab wired to that agent, not just
whichever tab happens to be showing the session — so a second, otherwise
idle tab reacted to a session it never asked for exactly like the tab that
opened it: minting its OWN conversation id
(`AgentPanel._on_session_started`'s `self._conversation_ids.setdefault(...)`)
and building its OWN, separate `TranscriptModel` for the very same
session id. Persisting from both tabs then wrote two `StoredConversation`s
for one live conversation — the drawer's duplicate rows — and each tab's
copy could disagree about what the session actually contains, which is the
"selecting a conversation shows the wrong/empty transcript" report.
"""

from __future__ import annotations

import pytest

from houdini_agent_panel import conversations_store as store
from houdini_agent_panel import sessions
from houdini_agent_panel import settings as settings_mod
from houdini_agent_panel.ui import panel as panel_mod


@pytest.fixture(autouse=True)
def isolated(qapp, monkeypatch):
    monkeypatch.setattr(panel_mod.scene, "hip_dir", lambda: "/tmp")
    monkeypatch.setattr(
        panel_mod.scene, "mcp_servers",
        lambda: [{"name": "fxhoudini", "command": "python", "args": [], "env": []}],
    )
    monkeypatch.setattr(panel_mod._RefreshWorker, "start", lambda self: None)
    current = settings_mod.load()
    current.default_agent = "claude-acp"
    current.autostart_agent = False  # tests fake the live session themselves
    settings_mod.save(current)
    panel_mod.reset_shared_state_for_tests()
    yield
    panel_mod.reset_shared_state_for_tests()


def _state(session_id: str, title: str = "Conversation") -> sessions.SessionState:
    return sessions.SessionState(session_id=session_id, title=title, cwd="/tmp", created_at=0.0)


def _two_tabs_on_claude(qapp):
    first = panel_mod.AgentPanel()
    qapp.processEvents()
    second = panel_mod.AgentPanel()
    qapp.processEvents()
    assert first._agent_id == second._agent_id == "claude-acp"
    return first, second


def test_a_second_idle_tab_does_not_grow_its_own_copy_of_another_tabs_session(qapp):
    """Both tabs are attached to `claude-acp` (its one shared process) before
    the conversation starts — the ordinary "two panels open" case
    `docs/design.md` promises is safe. Only the first tab acts; the second
    never touches session "s1" itself, yet it still receives every signal
    about it because the client is shared."""
    first, second = _two_tabs_on_claude(qapp)
    client = panel_mod.shared_client(first._agent_id)

    client.session_started.emit("s1", _state("s1", "Rotor pyro"))
    qapp.processEvents()
    client.message_chunk.emit("s1", "m1", "first reply")
    qapp.processEvents()

    assert [e.text for e in second._model("s1").entries()] == \
        [e.text for e in first._model("s1").entries()], (
            "the idle second tab built its own, separate copy of the first tab's session"
        )
    first.shutdown()
    second.shutdown()


def test_two_tabs_on_the_same_agent_do_not_duplicate_the_conversation_on_disk(qapp):
    first, second = _two_tabs_on_claude(qapp)
    client = panel_mod.shared_client(first._agent_id)

    client.session_started.emit("s1", _state("s1", "Rotor pyro"))
    qapp.processEvents()
    client.message_chunk.emit("s1", "m1", "first reply")
    qapp.processEvents()

    first._persist_conversations()
    second._persist_conversations()

    saved = store.load()
    matching = [c for c in saved if c.title == "Rotor pyro"]
    assert len(matching) == 1, f"expected one saved conversation, found {len(matching)}: {matching}"
    first.shutdown()
    second.shutdown()


def test_selecting_a_session_a_tab_joined_late_shows_its_history(qapp):
    """The literal owner report: clicking an existing conversation selects
    it (highlights the row — the session is right there in the shared
    pool) but the feed comes up empty. Reproduced by a tab that starts
    AFTER the session already has messages in it: the session is visible
    in the shared `SessionPool` from the moment it exists, but a tab that
    never itself received the events building it up must still be able to
    show it, not silently default to a blank `TranscriptModel`."""
    first = panel_mod.AgentPanel()
    qapp.processEvents()
    client = panel_mod.shared_client(first._agent_id)

    client.session_started.emit("s1", _state("s1", "Rotor pyro"))
    qapp.processEvents()
    client.message_chunk.emit("s1", "m1", "first reply")
    qapp.processEvents()

    second = panel_mod.AgentPanel()
    qapp.processEvents()
    assert second._pool.get("s1") is not None, "the session is shared — it must already be visible"

    second._set_current_session("s1")

    assert [e.text for e in second._model("s1").entries()] == ["first reply"], (
        "selecting the conversation in the second tab showed an empty transcript"
    )
    first.shutdown()
    second.shutdown()

"""Switching agents starts a clean conversation.

A session id is issued by one specific agent process and means nothing to
any other. The pool used to survive the switch, so the panel believed it
still had a live conversation, skipped creating a new session, and sent
prompts carrying an id the new agent had never issued — which just hung.
"""

from __future__ import annotations

import pytest

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
    panel_mod.reset_shared_state_for_tests()
    yield
    panel_mod.reset_shared_state_for_tests()


def _state(session_id: str) -> sessions.SessionState:
    return sessions.SessionState(
        session_id=session_id, title="Conversation", cwd="/tmp", created_at=0.0
    )


def test_pool_clear_drops_everything_and_announces_it():
    pool = sessions.SessionPool()
    removed: list[str] = []
    pool.removed.connect(removed.append)
    pool.add(_state("a"))
    pool.add(_state("b"))

    pool.clear()

    assert pool.all() == []
    assert sorted(removed) == ["a", "b"]


def test_single_tab_full_lifecycle_stays_correct(qapp, monkeypatch):
    """The single tab, single agent case — the overwhelming majority of real
    usage, never switching, never opening a second tab. Guarded explicitly:
    per-agent-id clients/pools (`shared_client`/`sessions.pool`) must not
    change what this ordinary case looks like, only what multiple tabs on
    DIFFERENT agents no longer do to each other. Covers picking an agent, a
    conversation, the agent restarting (an update, or any other stop then
    start — `AcpClient._spawn_worker` is what makes the SAME client object
    survive that, not a new one — team-lead's condition #4: this must keep
    working), and closing the tab.
    """
    agent_id = "claude-acp"
    monkeypatch.setattr(panel_mod.AgentPanel, "_start_agent", lambda self, agent_id: None)

    # 1. Pick an agent.
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    widget._on_agent_chosen(agent_id)
    assert widget._agent_id == agent_id
    assert settings_mod.load().default_agent == agent_id

    client = panel_mod.shared_client(agent_id)

    # 2. A conversation.
    state = sessions.SessionState(
        session_id="s1", title="New conversation", cwd="/tmp", created_at=0.0
    )
    client.session_started.emit("s1", state)
    qapp.processEvents()
    widget._set_current_session("s1")
    client.message_chunk.emit("s1", "m1", "hi there")
    qapp.processEvents()
    assert widget._current_session() is not None
    assert any("hi there" in e.text for e in widget._model("s1").entries())

    # 3. The agent restarts (e.g. after an update) — same client object,
    # `_spawn_worker` rebuilds only the worker thread inside it. A second
    # `connected` on the SAME object is what a real restart looks like.
    from houdini_agent_panel.client import AgentInfo

    info = AgentInfo(
        name="Claude Agent", version="2", protocol_version=1,
        supports_image=False, supports_audio=False, supports_embedded_context=False,
        supports_load_session=False, supports_logout=False, auth_methods=(),
    )
    client.disconnected.emit("restarting for an update")
    qapp.processEvents()
    client.connected.emit(info)
    qapp.processEvents()
    assert panel_mod.shared_client(agent_id) is client, (
        "a same-agent restart must not spawn a second client object"
    )

    # 4. Close the tab — the only one left on this agent, so its client
    # actually stops and is dropped, not just disconnected from.
    widget.shutdown()
    assert agent_id not in panel_mod._shared_clients


def test_switching_agents_does_not_carry_the_old_session(qapp, monkeypatch):
    widget = panel_mod.AgentPanel()
    qapp.processEvents()

    client = panel_mod.shared_client(widget._agent_id)
    client.session_started.emit("claude-session", _state("claude-session"))
    qapp.processEvents()
    assert widget._current_session() is not None

    monkeypatch.setattr(widget, "_start_agent", lambda agent_id: None)
    widget._on_agent_chosen("codex-acp")

    assert widget._current_session() is None, (
        "a session issued by the previous agent must not survive the switch"
    )
    assert widget._pool.all() == []
    widget.shutdown()


def test_switching_one_tabs_agent_does_not_disturb_another_tabs_agent(qapp, monkeypatch):
    """The owner's exact live report: tab 1 has Claude with a live
    conversation. Tab 2 switches to a different agent — tab 1's Claude
    session and connection must survive untouched. One agent process (and
    session list) per agent id, shared only among tabs actually using that
    same agent, not one process for the whole Houdini session.

    Currently fails: `shared_client()`/`sessions.pool()` are single,
    process-wide singletons, so `_on_agent_chosen` in ANY tab stops THE
    client and clears THE pool — the only ones there are, regardless of
    which tab is asking or which agent it is switching away from.
    """
    current = settings_mod.load()
    current.default_agent = "claude-acp"
    current.autostart_agent = False  # the test fakes the live session itself
    settings_mod.save(current)

    first = panel_mod.AgentPanel()
    qapp.processEvents()
    assert first._agent_id == "claude-acp"

    client = panel_mod.shared_client(first._agent_id)
    client.session_started.emit("claude-session", _state("claude-session"))
    qapp.processEvents()
    assert first._current_session() is not None

    stopped = []
    monkeypatch.setattr(client, "stop", lambda: stopped.append(True))

    second = panel_mod.AgentPanel()
    qapp.processEvents()
    assert second._agent_id == "claude-acp", "both tabs start on the same default agent"
    monkeypatch.setattr(second, "_start_agent", lambda agent_id: None)

    second._on_agent_chosen("gemini")

    assert stopped == [], "tab 2 switching agents stopped tab 1's own connection"
    assert first._current_session() is not None, (
        "tab 1's Claude session was wiped by tab 2 switching to a different agent"
    )

    first.shutdown()
    second.shutdown()


def test_conversation_survives_an_agent_switch(qapp, monkeypatch):
    """The artist's words are not the agent's property.

    Wiping the transcript on every switch was the bug, not the feature: an
    agent session id dies with its process, but what was written and read
    belongs to the person who wrote it.
    """
    from houdini_agent_panel import conversations_store as store

    widget = panel_mod.AgentPanel()
    qapp.processEvents()

    client = panel_mod.shared_client(widget._agent_id)
    client.session_started.emit("claude-session", _state("claude-session"))
    qapp.processEvents()
    widget._pool.get("claude-session").title = "Rotor pyro"
    client.message_chunk.emit("claude-session", "m1", "answer from Claude")
    qapp.processEvents()

    monkeypatch.setattr(widget, "_start_agent", lambda agent_id: None)
    widget._on_agent_chosen("codex-acp")

    # The dead session id is gone — it means nothing to the new agent.
    assert widget._pool.all() == []
    # The conversation is not.
    saved = store.load()
    assert [c.title for c in saved] == ["Rotor pyro"]
    assert any("answer from Claude" in e["text"] for e in saved[0].entries)
    widget.shutdown()



def test_stop_releases_the_input_even_if_the_agent_never_answers(qapp, monkeypatch):
    """`session/cancel` is a notification — the agent may ignore it. The panel
    must not stay locked forever because of that."""
    monkeypatch.setattr(panel_mod, "_CANCEL_GRACE_MS", 10)

    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    client = panel_mod.shared_client(widget._agent_id)
    state = _state("live")
    client.session_started.emit(state.session_id, state)
    qapp.processEvents()

    pooled = widget._pool.get("live")
    pooled.busy = True
    widget._composer.set_busy(True)
    monkeypatch.setattr(client, "cancel", lambda _sid: None)  # agent stays silent

    widget._on_cancelled()

    from houdini_agent_panel.ui.qt import QtCore

    deadline = QtCore.QElapsedTimer()
    deadline.start()
    while deadline.elapsed() < 3000 and pooled.busy:
        qapp.processEvents()
        QtCore.QThread.msleep(5)

    assert not pooled.busy, "the panel trapped the artist with a dead stop button"
    assert not widget._composer._busy
    widget.shutdown()


def test_stop_without_a_session_still_unlocks(qapp):
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    widget._composer.set_busy(True)

    widget._on_cancelled()

    assert not widget._composer._busy
    widget.shutdown()

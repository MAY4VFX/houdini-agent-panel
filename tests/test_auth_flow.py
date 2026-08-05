"""Signing in has to visibly finish.

From a live session: Grok asked for a login, the browser opened, the artist
approved the code — and the panel stayed on the sign-in screen. Nothing had
told it the agent had let them in, because a successful `authenticate`
returned in silence.
"""

from __future__ import annotations

import pytest

from houdini_agent_panel import sessions
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


def test_successful_sign_in_leaves_the_sign_in_screen(qapp, monkeypatch):
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    client = panel_mod.shared_client(widget._agent_id)

    from houdini_agent_panel.client import AuthMethod

    client.auth_required.emit([AuthMethod(id="grok.com", name="Grok")])
    qapp.processEvents()
    assert widget._pages.currentIndex() == panel_mod.AgentPanel.PAGE_AUTH

    started: list[bool] = []
    monkeypatch.setattr(widget, "_start_new_session", lambda: started.append(True))

    client.authenticated.emit("grok.com")
    qapp.processEvents()

    assert widget._pages.currentIndex() == panel_mod.AgentPanel.PAGE_TRANSCRIPT
    assert started, "a fresh sign-in must open a conversation, not leave a blank panel"
    widget.shutdown()


def test_sign_in_does_not_reopen_a_conversation_that_already_exists(qapp, monkeypatch):
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    client = panel_mod.shared_client(widget._agent_id)

    state = sessions.SessionState(
        session_id="live", title="New conversation", cwd="/tmp", created_at=0.0
    )
    client.session_started.emit("live", state)
    qapp.processEvents()

    started: list[bool] = []
    monkeypatch.setattr(widget, "_start_new_session", lambda: started.append(True))

    client.authenticated.emit("grok.com")
    qapp.processEvents()

    assert started == []
    widget.shutdown()


def test_choosing_a_method_tells_the_artist_to_check_the_browser(qapp, monkeypatch):
    """The agent's login opens a browser; a panel that says nothing looks
    like a button that did nothing."""
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    monkeypatch.setattr(panel_mod.shared_client(widget._agent_id), "authenticate", lambda _m: None)

    notes: list[str] = []
    monkeypatch.setattr(widget, "_note", notes.append)

    widget._on_auth_method_chosen("grok.com")

    assert notes and "browser" in notes[0].lower()
    widget.shutdown()


def test_sign_in_is_not_offered_to_an_agent_already_working(qapp, monkeypatch):
    """Reported for Codex: the Sign in button sat in Settings while the agent
    was answering questions, and pressing it led to a sign-in screen with a
    Sign out at the bottom.

    `authMethods` says which methods EXIST, not whether they have been used —
    every agent lists them signed in or out. A session that opened is the
    only proof the protocol offers, so that is what decides.
    """
    from houdini_agent_panel import client as client_mod
    from houdini_agent_panel import sessions
    from houdini_agent_panel.ui import panel as panel_mod

    info = client_mod.AgentInfo(
        name="codex", version="1.1.9", protocol_version=1,
        supports_image=False, supports_audio=False, supports_embedded_context=False,
        supports_load_session=False, supports_logout=True,
        auth_methods=(client_mod.AuthMethod(id="chatgpt", name="ChatGPT"),),
    )
    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("codex-acp")
    offered: list[bool] = []
    monkeypatch.setattr(
        widget._settings_view, "set_current_agent_auth",
        lambda _agent, can: offered.append(can), raising=False,
    )

    widget._sync_agent_auth_row(info)
    assert offered[-1] is True, "no session yet — signing in is exactly what's needed"

    widget._pool.add(sessions.SessionState(
        session_id="live-1", title="chat", cwd="/tmp", created_at=0.0))
    widget._sync_agent_auth_row(info)
    assert offered[-1] is False, (
        "a session is open, so the agent is signed in — offering sign-in is noise"
    )
    widget.shutdown()

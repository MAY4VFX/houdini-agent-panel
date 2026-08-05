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

    # An open session used to stand in for this, and the measurement on the
    # Linux machine refuted it: two agents out of three open a session while
    # signed out. An answered prompt is what they all agree on.
    widget._on_turn_finished("live-1", "end_turn")
    widget._sync_agent_auth_row(info)
    assert offered[-1] is False, (
        "the agent has answered a prompt, so it is signed in — offering sign-in is noise"
    )
    widget.shutdown()


def test_sign_out_is_not_offered_to_an_agent_never_signed_into(qapp, monkeypatch):
    """Reported on the Linux machine: a fresh Codex showed API Key, ChatGPT
    *and* Sign out. It came from `supports_logout`, which says the method is
    implemented, not that anyone has used it — the same confusion of
    capability with state as the Sign in row, one screen along."""
    from houdini_agent_panel import client as client_mod
    from houdini_agent_panel import sessions
    from houdini_agent_panel.ui import panel as panel_mod

    info = client_mod.AgentInfo(
        name="codex", version="1.1.9", protocol_version=1,
        supports_image=False, supports_audio=False, supports_embedded_context=False,
        supports_load_session=False, supports_logout=True,
        auth_methods=(
            client_mod.AuthMethod(id="apikey", name="API Key"),
            client_mod.AuthMethod(id="chatgpt", name="ChatGPT"),
        ),
    )
    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("codex-acp")

    assert widget._can_sign_out(info) is False, "offered a way out of a door never entered"

    widget._on_turn_finished("live-1", "end_turn")
    assert widget._can_sign_out(info) is True, (
        "a working agent must still be able to switch accounts"
    )
    widget.shutdown()


def test_an_agent_with_no_methods_is_sent_to_its_login_command(qapp, monkeypatch):
    """Reported on the Linux machine: a fresh Claude Agent, asked a question,
    landed on a screen headed "Sign in" that read "The agent offered no
    sign-in methods" with a Sign out button beneath it. Three untruths in
    one screen, and no way forward from any of them.

    An agent advertising no `authMethods` is not an agent without a login —
    it is one whose login is a slash command inside the session."""
    from houdini_agent_panel import client as client_mod
    from houdini_agent_panel.ui import panel as panel_mod

    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("claude-acp")
    notes: list[str] = []
    monkeypatch.setattr(widget, "_note", notes.append)
    monkeypatch.setattr(
        panel_mod.shared_client(widget._agent_id),
        "agent_info",
        lambda: client_mod.AgentInfo(
            name="claude", version="1.0", protocol_version=1,
            supports_image=False, supports_audio=False, supports_embedded_context=False,
            supports_load_session=False, supports_logout=True, auth_methods=(),
        ),
        raising=False,
    )

    widget._on_auth_required([])

    assert widget._pages.currentIndex() == widget.PAGE_TRANSCRIPT, (
        "sent to a sign-in screen with nothing on it"
    )
    assert widget._composer._text_edit.toPlainText() == "/login"
    assert notes and "/login" in notes[-1]
    widget.shutdown()


def _info(**kwargs):
    from houdini_agent_panel import client as client_mod

    base = dict(
        name="agent", version="1.0", protocol_version=1,
        supports_image=False, supports_audio=False, supports_embedded_context=False,
        supports_load_session=False, supports_logout=True, auth_methods=(),
    )
    base.update(kwargs)
    return client_mod.AgentInfo(**base)


def test_an_open_session_is_not_evidence_of_being_signed_in(qapp, monkeypatch):
    """Measured on a machine where no agent had ever been configured:
    `claude-acp` advertises no methods and opens a session happily (it fails
    at the first prompt instead), `opencode` advertises one and also opens a
    session, and only `codex-acp` refuses `session/new` with "Authentication
    required". So a session proves nothing on two agents out of three — and
    that is exactly how a never-configured Claude came to be shown a Sign
    out button."""
    from houdini_agent_panel import sessions
    from houdini_agent_panel.ui import panel as panel_mod

    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("claude-acp")
    widget._pool.add(sessions.SessionState("live-1", "chat", "/tmp", 0.0))

    assert widget._is_signed_in() is False
    assert widget._can_sign_out(_info(auth_methods=())) is False
    widget.shutdown()


def test_a_completed_turn_is_what_proves_it(qapp, monkeypatch):
    """The one signal all three agree on: none of them answers a prompt for
    an account that is not signed in."""
    from houdini_agent_panel import client as client_mod
    from houdini_agent_panel.ui import panel as panel_mod

    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("codex-acp")
    monkeypatch.setattr(widget, "_note", lambda *_: None)
    method = client_mod.AuthMethod(id="chatgpt", name="ChatGPT")
    offered: list[bool] = []
    monkeypatch.setattr(
        widget._settings_view, "set_current_agent_auth",
        lambda _agent, can: offered.append(can), raising=False,
    )

    widget._sync_agent_auth_row(_info(auth_methods=(method,)))
    assert offered[-1] is True, "nothing proved yet — offering the way in is right"

    widget._on_turn_finished("s1", "end_turn")
    widget._sync_agent_auth_row(_info(auth_methods=(method,)))

    assert offered[-1] is False
    assert widget._can_sign_out(_info(auth_methods=(method,))) is True
    widget.shutdown()


def test_an_auth_error_takes_the_evidence_back(qapp, monkeypatch):
    """Signed in yesterday, token expired overnight — the agent says so, and
    the panel must believe the agent over its own record."""
    from houdini_agent_panel.ui import panel as panel_mod

    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("codex-acp")
    monkeypatch.setattr(widget, "_note", lambda *_: None)
    widget._on_turn_finished("s1", "end_turn")
    assert widget._is_signed_in() is True

    widget._on_auth_required([])

    assert widget._is_signed_in() is False
    widget.shutdown()


def test_the_evidence_survives_a_restart(qapp, monkeypatch):
    """Otherwise the Sign in row comes back on every Houdini start, until
    the artist types something — which is the complaint this began with."""
    from houdini_agent_panel import settings as settings_mod
    from houdini_agent_panel.ui import panel as panel_mod

    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("codex-acp")
    monkeypatch.setattr(widget, "_note", lambda *_: None)
    widget._on_turn_finished("s1", "end_turn")
    widget.shutdown()

    assert "codex-acp" in settings_mod.load().signed_in_agents


def test_each_sign_in_method_says_what_it_actually_does(qapp, monkeypatch):
    """Measured on a clean HOME (facts/acp-sdk.md §12): `api-key` fails at
    once with "CODEX_API_KEY or OPENAI_API_KEY is not set" — it is an
    environment variable, not something the panel can ask for — while
    `chat-gpt` does not return at all, staying open while a browser window
    takes its time to appear. The artist reported both as "login doesn't
    work"; one of them was working and said nothing."""
    from houdini_agent_panel.ui import panel as panel_mod

    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("codex-acp")
    notes: list[str] = []
    monkeypatch.setattr(widget, "_note", notes.append)
    monkeypatch.setattr(
        panel_mod.shared_client(widget._agent_id), "authenticate",
        lambda *_: None, raising=False,
    )

    widget._on_auth_method_chosen("chat-gpt")
    assert "few seconds" in notes[-1], "no warning that the browser is slow to appear"

    widget._on_auth_method_chosen("api-key")
    assert "CODEX_API_KEY" in notes[-1], "the variable it actually needs went unnamed"
    widget.shutdown()

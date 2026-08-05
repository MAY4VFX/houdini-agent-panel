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


def test_sign_in_capability_is_cached_regardless_of_believed_sign_in_state(qapp, monkeypatch):
    """Used to gate the Settings row's Sign in on "no session has opened
    yet" — reported for Codex: the button sat in Settings while the agent
    was already answering questions. That traded one wrong belief for
    another: whether a session is open, or whether a turn has completed,
    is the panel's own GUESS about account state (`_is_signed_in`,
    docs/facts/acp-sdk.md §11), and issue #33 is the guess going wrong the
    other way — an artist stuck on a broken login with the panel convinced
    they were already signed in, and the button that would let them retry
    nowhere to be found. So caching is now unconditional: `authMethods` is
    what decides, not a guess about whether it's currently needed.
    """
    from houdini_agent_panel import client as client_mod
    from houdini_agent_panel import settings as settings_mod
    from houdini_agent_panel.ui import panel as panel_mod

    info = client_mod.AgentInfo(
        name="codex", version="1.1.9", protocol_version=1,
        supports_image=False, supports_audio=False, supports_embedded_context=False,
        supports_load_session=False, supports_logout=True,
        auth_methods=(client_mod.AuthMethod(id="chatgpt", name="ChatGPT"),),
    )
    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("codex-acp")

    widget._sync_agent_auth_row(info)
    cached = settings_mod.load().agent_auth_info["codex-acp"]
    assert [m.id for m in cached.methods] == ["chatgpt"]
    assert cached.supports_logout is True

    # An answered prompt is what all three measured agents agree means
    # "signed in" — but it must not erase what's cached, or the Settings
    # row for a codex-acp that ISN'T the one connected right now would lose
    # its Sign in button the moment THIS one happens to finish a turn.
    widget._on_turn_finished("live-1", "end_turn")
    widget._sync_agent_auth_row(info)
    cached = settings_mod.load().agent_auth_info["codex-acp"]
    assert [m.id for m in cached.methods] == ["chatgpt"]
    widget.shutdown()


def test_sign_out_reflects_capability_not_a_signed_in_guess(qapp, monkeypatch):
    """Reported on the Linux machine: a fresh Codex showed API Key, ChatGPT
    *and* Sign out — that used `supports_logout`, which says the method is
    IMPLEMENTED, gated on a further guess about whether anyone had used it.
    Issue #33 asks for the opposite of that gate: Sign out reachable
    whenever the agent can do it, not only when the panel guesses it's
    needed — the guess is exactly what stranded people the other way."""
    from houdini_agent_panel import client as client_mod
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

    assert widget._can_sign_out(info) is True, "the agent CAN log out — that's all this checks"

    widget._on_turn_finished("live-1", "end_turn")
    assert widget._can_sign_out(info) is True

    # Still false with no methods at all — nothing to sign out OF.
    no_methods = client_mod.AgentInfo(
        name="codex", version="1.1.9", protocol_version=1,
        supports_image=False, supports_audio=False, supports_embedded_context=False,
        supports_load_session=False, supports_logout=True, auth_methods=(),
    )
    assert widget._can_sign_out(no_methods) is False
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
    # NOT prefilled with /login any more: this agent answered "/login isn't
    # available in this environment" when it was. What must be there is a
    # route out, named.
    assert notes and ("terminal" in notes[-1] or "/login" in notes[-1])
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
    an account that is not signed in. `_is_signed_in()` still tracks this
    (issue #33 only stopped USING it to gate Sign in/out reachability, it
    didn't remove the underlying record — see `_sync_agent_auth_row`'s own
    docstring)."""
    from houdini_agent_panel import client as client_mod
    from houdini_agent_panel.ui import panel as panel_mod

    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("codex-acp")
    monkeypatch.setattr(widget, "_note", lambda *_: None)
    method = client_mod.AuthMethod(id="chatgpt", name="ChatGPT")

    assert widget._is_signed_in() is False, "nothing proved yet"

    widget._on_turn_finished("s1", "end_turn")

    assert widget._is_signed_in() is True
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


def test_the_agents_own_description_beats_the_guessed_advice(qapp, monkeypatch):
    """Kimi's `login` method describes itself: "Run `kimi login` command in
    the terminal, then follow the instructions to finish login."
    (docs/facts/acp-sdk.md §13/§14) — more precise than this repo's own
    guess, and it can't go stale the way a hardcoded id-keyed table would if
    Kimi ever changed how its login works. The agent's word wins whenever
    it bothers to give one (design.md: the agent decides what exists)."""
    from houdini_agent_panel import client as client_mod
    from houdini_agent_panel.ui import panel as panel_mod

    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("kimi")
    client = panel_mod.shared_client(widget._agent_id)
    monkeypatch.setattr(client, "authenticate", lambda *_: None, raising=False)
    description = "Run `kimi login` command in the terminal, then follow the instructions to finish login."
    monkeypatch.setattr(
        client, "agent_info",
        lambda: client_mod.AgentInfo(
            name="kimi", version="1.49.0", protocol_version=1,
            supports_image=False, supports_audio=False, supports_embedded_context=False,
            supports_load_session=False, supports_logout=False,
            auth_methods=(client_mod.AuthMethod(id="login", name="Login with Kimi account", description=description),),
        ),
    )

    widget._on_auth_method_chosen("login")

    assert widget._auth_view._pending_label.text() == description
    widget.shutdown()


def test_login_is_only_suggested_when_the_agent_has_it(qapp, monkeypatch):
    """The panel told a signed-out Claude Agent to type `/login`, and the
    agent answered "/login isn't available in this environment". The
    measurement that predicted it was already in hand and went unused:
    `claude-acp` returns an EMPTY `availableCommands`.

    So the question gets asked of the session instead of assumed."""
    from houdini_agent_panel import client as client_mod
    from houdini_agent_panel import sessions
    from houdini_agent_panel.ui import panel as panel_mod

    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("claude-acp")
    notes: list[str] = []
    monkeypatch.setattr(widget, "_note", notes.append)
    state = sessions.SessionState("s1", "chat", "/tmp", 0.0)
    widget._pool.add(state)
    widget._current_session_id = "s1"
    info = client_mod.AgentInfo(
        name="claude", version="1.0", protocol_version=1,
        supports_image=False, supports_audio=False, supports_embedded_context=False,
        supports_load_session=False, supports_logout=False, auth_methods=(),
    )

    widget._offer_login_command(info)

    assert widget._composer._text_edit.toPlainText() != "/login", (
        "still putting a command the agent does not have into the input"
    )
    assert "terminal" in notes[-1], "no route out was named at all"
    widget.shutdown()


def test_choosing_a_method_shows_a_pending_state_on_the_sign_in_screen(qapp, monkeypatch):
    """Issue #33: the screen used to go quiet the instant a method was
    picked, and a working Codex login (browser opens, artist finishes it
    there) looked identical to a stuck one. The wait itself now shows on
    the screen the artist is looking at, not only in the transcript."""
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    monkeypatch.setattr(panel_mod.shared_client(widget._agent_id), "authenticate", lambda _m: None)
    monkeypatch.setattr(widget, "_note", lambda *_: None)

    widget._on_auth_method_chosen("chat-gpt")

    assert not widget._auth_view._pending_label.isHidden()
    assert "browser" in widget._auth_view._pending_label.text()
    widget.shutdown()


def test_cancelling_the_wait_only_clears_the_screen_not_the_pending_call(qapp, monkeypatch):
    """There is no protocol call to cancel `authenticate()` itself
    (docs/facts/acp-sdk.md §12) — Cancel is UI-only, and a success arriving
    later must still be honored even if the artist gave up watching."""
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    widget._rejoin_agent("codex-acp")
    monkeypatch.setattr(panel_mod.shared_client(widget._agent_id), "authenticate", lambda _m: None)
    monkeypatch.setattr(widget, "_note", lambda *_: None)

    widget._on_auth_method_chosen("chat-gpt")
    widget._on_auth_cancel_pending()
    assert widget._auth_view._pending_label.isHidden()

    # A late `authenticated` must still work — cancelling the UI wait did
    # not cancel the underlying request.
    client = panel_mod.shared_client(widget._agent_id)
    client.authenticated.emit("chat-gpt")
    qapp.processEvents()
    assert widget._pages.currentIndex() == panel_mod.AgentPanel.PAGE_TRANSCRIPT
    widget.shutdown()


def test_successful_sign_in_is_recorded_as_the_last_attempt(qapp, monkeypatch):
    """"Show the last attempt's result beside the method" (issue #33) —
    persisted so a Settings row can say it even for an agent that isn't
    the one connected right now."""
    from houdini_agent_panel import settings as settings_mod

    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    widget._rejoin_agent("codex-acp")
    client = panel_mod.shared_client(widget._agent_id)

    widget._on_auth_method_chosen("chat-gpt")
    client.authenticated.emit("chat-gpt")
    qapp.processEvents()

    attempt = settings_mod.load().auth_attempts[widget._agent_id]
    assert attempt.action == "sign_in"
    assert attempt.ok is True
    widget.shutdown()


def test_failed_sign_in_is_recorded_as_the_last_attempt(qapp, monkeypatch):
    from houdini_agent_panel import settings as settings_mod

    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    widget._rejoin_agent("codex-acp")
    client = panel_mod.shared_client(widget._agent_id)

    widget._on_auth_method_chosen("api-key")
    widget._show_page(widget.PAGE_AUTH)
    client.error.emit("", "CODEX_API_KEY or OPENAI_API_KEY is not set")
    qapp.processEvents()

    attempt = settings_mod.load().auth_attempts[widget._agent_id]
    assert attempt.action == "sign_in"
    assert attempt.ok is False
    assert "CODEX_API_KEY" in attempt.message
    widget.shutdown()


def test_sign_out_from_settings_logs_out_the_current_agent_directly(qapp, monkeypatch):
    """Issue #33: Sign out reachable from Settings at any time, not only
    from the sign-in screen's own button."""
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    widget._rejoin_agent("codex-acp")
    logged_out = []
    monkeypatch.setattr(
        panel_mod.shared_client(widget._agent_id), "logout", lambda: logged_out.append(True)
    )

    widget._on_agent_row_sign_out(widget._agent_id)

    assert logged_out == [True]
    assert widget._pending_logout_agent == widget._agent_id
    widget.shutdown()


def test_sign_out_success_from_settings_is_recorded(qapp, monkeypatch):
    from houdini_agent_panel import settings as settings_mod

    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    widget._rejoin_agent("codex-acp")
    client = panel_mod.shared_client(widget._agent_id)
    monkeypatch.setattr(client, "logout", lambda: None)

    widget._on_agent_row_sign_out(widget._agent_id)
    client.auth_required.emit([])
    qapp.processEvents()

    attempt = settings_mod.load().auth_attempts[widget._agent_id]
    assert attempt.action == "sign_out"
    assert attempt.ok is True
    assert widget._pending_logout_agent is None
    widget.shutdown()


def test_sign_out_failure_from_settings_is_noted_not_lost(qapp, monkeypatch):
    """No sign-in screen is guaranteed to be open when a Settings-triggered
    logout's answer arrives — it must still reach the artist, and still be
    recorded for the row that asked."""
    from houdini_agent_panel import settings as settings_mod

    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    widget._rejoin_agent("codex-acp")
    client = panel_mod.shared_client(widget._agent_id)
    monkeypatch.setattr(client, "logout", lambda: None)
    notes: list[str] = []
    monkeypatch.setattr(widget, "_note", notes.append)

    widget._on_agent_row_sign_out(widget._agent_id)
    client.error.emit("", "the agent refused to log out")
    qapp.processEvents()

    assert any("Sign out failed" in n for n in notes)
    attempt = settings_mod.load().auth_attempts[widget._agent_id]
    assert attempt.action == "sign_out"
    assert attempt.ok is False
    widget.shutdown()


def test_a_terminal_auth_method_spawns_a_worker_instead_of_authenticating(qapp, monkeypatch):
    """Kimi's `login` never answers `authenticate()` at all (docs/facts/
    acp-sdk.md §13-14) — calling it anyway would just hang for no reason.
    Measured: this method wants a SEPARATE process, not the ACP channel."""
    from houdini_agent_panel import client as client_mod
    from houdini_agent_panel.ui import terminal_login as terminal_login_mod

    monkeypatch.setattr(terminal_login_mod.TerminalLoginWorker, "start", lambda self: None)

    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    widget._rejoin_agent("kimi")
    client = panel_mod.shared_client(widget._agent_id)
    authenticated: list[str] = []
    monkeypatch.setattr(client, "authenticate", lambda mid: authenticated.append(mid))
    ta = client_mod.TerminalAuth(command="/bin/fake-kimi", args=["login"], env={})
    monkeypatch.setattr(
        client, "agent_info",
        lambda: client_mod.AgentInfo(
            name="kimi", version="1.49.0", protocol_version=1,
            supports_image=False, supports_audio=False, supports_embedded_context=False,
            supports_load_session=False, supports_logout=False,
            auth_methods=(
                client_mod.AuthMethod(id="login", name="Login with Kimi account", terminal_auth=ta),
            ),
        ),
    )

    widget._on_auth_method_chosen("login")

    assert authenticated == [], "authenticate() was called for a method that never answers it"
    assert widget._terminal_login_worker is not None
    widget.shutdown()


def test_the_sdks_unresolved_terminal_shape_falls_back_to_authenticate(qapp, monkeypatch):
    """The SDK's own `TerminalAuthMethod` shape has no `command` field at
    all (`client.TerminalAuth.command is None`) — unmeasured in practice,
    no agent probed uses it (docs/facts/acp-sdk.md §13). Resolving "the
    agent's own binary" isn't implemented, so this degrades to the
    ordinary wait rather than attempting a spawn with nothing to run."""
    from houdini_agent_panel import client as client_mod

    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    widget._rejoin_agent("some-agent")
    client = panel_mod.shared_client(widget._agent_id)
    authenticated: list[str] = []
    monkeypatch.setattr(client, "authenticate", lambda mid: authenticated.append(mid))
    ta = client_mod.TerminalAuth(command=None, args=["--login"], env={})
    monkeypatch.setattr(
        client, "agent_info",
        lambda: client_mod.AgentInfo(
            name="some-agent", version="1.0", protocol_version=1,
            supports_image=False, supports_audio=False, supports_embedded_context=False,
            supports_load_session=False, supports_logout=False,
            auth_methods=(
                client_mod.AuthMethod(id="terminal", name="Terminal login", terminal_auth=ta),
            ),
        ),
    )

    widget._on_auth_method_chosen("terminal")

    assert authenticated == ["terminal"]
    assert widget._terminal_login_worker is None
    widget.shutdown()


def test_terminal_login_url_found_shows_a_link_on_the_sign_in_screen(qapp):
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    widget._show_page(widget.PAGE_AUTH)

    widget._on_terminal_login_url(
        "https://www.kimi.com/code/authorize_device?user_code=14OI-AX7F", "14OI-AX7F"
    )

    text = widget._auth_view._pending_label.text()
    assert "14OI-AX7F" in text
    assert "kimi.com" in text
    widget.shutdown()


def test_leaving_the_sign_in_screen_stops_a_running_terminal_login(qapp):
    """Real `kimi login` polls indefinitely on its own (docs/facts/acp-sdk.md
    §14) — walking away from the screen must not leave it running."""
    from types import SimpleNamespace

    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    widget._show_page(widget.PAGE_AUTH)
    stopped: list[bool] = []
    widget._terminal_login_worker = SimpleNamespace(stop=lambda: stopped.append(True))

    widget._show_page(widget.PAGE_TRANSCRIPT)

    assert stopped == [True]
    assert widget._terminal_login_worker is None
    widget.shutdown()


def test_cancel_pending_stops_a_running_terminal_login(qapp):
    from types import SimpleNamespace

    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    stopped: list[bool] = []
    widget._terminal_login_worker = SimpleNamespace(stop=lambda: stopped.append(True))

    widget._on_auth_cancel_pending()

    assert stopped == [True]
    assert widget._terminal_login_worker is None
    widget.shutdown()


def test_gemini_style_stderr_while_pending_is_shown_on_the_sign_in_screen(qapp):
    """Gemini's `oauth-personal` never returns and never emits anything but
    stderr retry text (docs/facts/acp-sdk.md §13) — today that line matches
    none of `_FATAL_STDERR_MARKERS`, so without this it went nowhere the
    artist could see."""
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    widget._show_page(widget.PAGE_AUTH)
    widget._auth_pending = True
    client = panel_mod.shared_client(widget._agent_id)

    client.log_line.emit("Failed to authenticate with authorization code:invalid_grant")
    qapp.processEvents()

    assert "invalid_grant" in widget._auth_view._pending_detail_label.text()
    widget.shutdown()


def test_stderr_is_not_surfaced_when_not_actually_pending(qapp):
    """Ordinary stderr noise from a running conversation must not start
    showing up on the sign-in screen just because it happens to be open —
    only while a sign-in is actually in flight."""
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    widget._show_page(widget.PAGE_AUTH)
    client = panel_mod.shared_client(widget._agent_id)

    client.log_line.emit("some ordinary line")
    qapp.processEvents()

    assert widget._auth_view._pending_detail_label.text() == ""
    widget.shutdown()

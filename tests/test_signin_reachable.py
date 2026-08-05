"""Signing in must not depend on the agent asking first.

From a live session with Grok: it accepted a session as if all was well and
only wrote "Auth(AuthorizationRequired)" to its own stderr. `auth_required`
never came, so the sign-in screen was unreachable and the conversation
answered nothing — with no way in from the UI at all.
"""

from __future__ import annotations

import pytest

from houdini_agent_panel.client import AgentInfo, AuthMethod
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


def _info(**overrides) -> AgentInfo:
    base = dict(
        name="Grok Build", version="1", protocol_version=1,
        supports_image=False, supports_audio=False, supports_embedded_context=True,
        supports_load_session=False, supports_logout=False,
        auth_methods=(AuthMethod(id="grok.com", name="Grok"),),
    )
    base.update(overrides)
    return AgentInfo(**base)


def test_authorization_error_on_stderr_opens_the_sign_in_screen(qapp, monkeypatch):
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    client = panel_mod.shared_client(widget._agent_id)
    monkeypatch.setattr(client, "agent_info", lambda: _info())

    client.log_line.emit(
        "ERROR worker quit with fatal: Transport channel closed, when Auth(AuthorizationRequired)"
    )
    qapp.processEvents()

    assert widget._pages.currentIndex() == panel_mod.AgentPanel.PAGE_AUTH
    widget.shutdown()


def test_sign_in_is_reachable_from_the_settings_agents_row(qapp, monkeypatch):
    """The manual entry point moved from the header chip's switcher menu to
    the Settings screen's agent row (an artist's complaint: "Claude is
    already signed in, why offer it there, and why not next to the agent
    itself" — both fair, see `ui/agents.py::_AgentRow`'s `has_auth`).
    `_offer_sign_in` itself — and the forced `auth_required` screen — are
    unchanged; only who can reach it moved. The signal now carries the
    agent id (issue #33: any installed agent's row can send it, not only
    the one this tab happens to be connected to) — this test's own agent
    IS the current one, so `_on_agent_row_sign_in` routes it straight to
    `_offer_sign_in` with no detour through switching agents.
    """
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    client = panel_mod.shared_client(widget._agent_id)
    monkeypatch.setattr(client, "agent_info", lambda: _info())

    widget._settings_view.sign_in_requested.emit(widget._agent_id)
    qapp.processEvents()

    assert widget._pages.currentIndex() == panel_mod.AgentPanel.PAGE_AUTH
    widget.shutdown()


def test_sign_in_for_a_different_agent_switches_to_it_first(qapp, monkeypatch):
    """Issue #33: Sign in is reachable from Settings for ANY installed
    agent, not only the one this tab happens to be connected to. There is
    no way to hold a second live connection open per tab (`_agent_id`), so
    `_on_agent_row_sign_in` switches this tab onto the requested agent —
    driven here directly rather than through a real subprocess launch,
    same as `_on_agent_chosen` itself is exercised elsewhere in this
    suite — and opens its sign-in screen the moment it actually connects.
    """
    widget = panel_mod.AgentPanel()
    qapp.processEvents()

    switched: list[str] = []
    monkeypatch.setattr(widget, "_on_agent_chosen", switched.append)

    widget._settings_view.sign_in_requested.emit("codex-acp")
    qapp.processEvents()

    assert switched == ["codex-acp"]
    assert widget._pending_auth_target == "codex-acp"

    # The switch this test stubbed out would normally end with THIS tab's
    # `_agent_id` now being "codex-acp" and a live `agent_info()` for it —
    # simulate exactly that much, then let the connect flow's own tail run.
    widget._agent_id = "codex-acp"
    client = panel_mod.shared_client("codex-acp")
    monkeypatch.setattr(client, "agent_info", lambda: _info())
    widget._complete_pending_auth_switch()

    assert widget._pending_auth_target is None
    assert widget._pages.currentIndex() == panel_mod.AgentPanel.PAGE_AUTH
    widget.shutdown()


def test_agent_switcher_menu_no_longer_offers_sign_in(qapp):
    """A real complaint from the artist: "Claude is already signed in, why
    is there a Sign in button in the switcher menu" — that control used to
    show for ANY agent that had declared auth methods, whether or not the
    artist was already signed in, in the one menu meant to answer "which
    agent do I want to talk to", not "manage this agent". It's gone from
    the header entirely now — moved to the agent's own row in Settings.
    """
    widget = panel_mod.AgentPanel()
    qapp.processEvents()

    assert not hasattr(widget._header, "sign_in_clicked")
    assert not hasattr(widget._header, "set_can_sign_in")
    widget.shutdown()


def test_agent_without_auth_methods_still_gets_a_way_in(qapp, monkeypatch):
    """Used to do nothing at all — reported for real on Claude Agent
    (zero methods): the Settings row could be clicked, and clicking it
    was a dead end. Zero methods is not the same as nothing to do
    (`AgentPanel._no_methods_advice`); the sign-in screen now shows
    whatever real, agent-specific instructions exist instead of silently
    staying wherever the artist already was."""
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    client = panel_mod.shared_client(widget._agent_id)
    monkeypatch.setattr(client, "agent_info", lambda: _info(auth_methods=()))

    widget._offer_sign_in()
    qapp.processEvents()

    assert widget._pages.currentIndex() == panel_mod.AgentPanel.PAGE_AUTH
    assert widget._auth_view._empty_label.text() != "The agent offered no sign-in methods."
    widget.shutdown()


def test_ordinary_stderr_noise_is_not_shouted_at_the_artist(qapp):
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    notes: list[str] = []
    widget._note = notes.append

    panel_mod.shared_client(widget._agent_id).log_line.emit("ExperimentalWarning: Importing JSON modules")
    qapp.processEvents()

    assert notes == []
    widget.shutdown()

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
    client = panel_mod.shared_client()
    monkeypatch.setattr(client, "agent_info", lambda: _info())

    client.log_line.emit(
        "ERROR worker quit with fatal: Transport channel closed, when Auth(AuthorizationRequired)"
    )
    qapp.processEvents()

    assert widget._pages.currentIndex() == panel_mod.AgentPanel.PAGE_AUTH
    widget.shutdown()


def test_sign_in_is_offered_in_the_menu_whenever_the_agent_has_methods(qapp, monkeypatch):
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    client = panel_mod.shared_client()
    monkeypatch.setattr(client, "agent_info", lambda: _info())

    widget._header.sign_in_clicked.emit()
    qapp.processEvents()

    assert widget._pages.currentIndex() == panel_mod.AgentPanel.PAGE_AUTH
    widget.shutdown()


def test_agent_without_auth_methods_gets_no_sign_in(qapp, monkeypatch):
    """The rule holds: the agent doesn't offer it, the control isn't drawn."""
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    client = panel_mod.shared_client()
    monkeypatch.setattr(client, "agent_info", lambda: _info(auth_methods=()))

    widget._offer_sign_in()
    qapp.processEvents()

    assert widget._pages.currentIndex() != panel_mod.AgentPanel.PAGE_AUTH
    widget.shutdown()


def test_ordinary_stderr_noise_is_not_shouted_at_the_artist(qapp):
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    notes: list[str] = []
    widget._note = notes.append

    panel_mod.shared_client().log_line.emit("ExperimentalWarning: Importing JSON modules")
    qapp.processEvents()

    assert notes == []
    widget.shutdown()

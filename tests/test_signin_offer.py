"""Offer sign-in the moment the agent connects, not after a wasted turn.

The owner's own report: he picked Claude Agent, typed "hi", waited 1m41s,
and got a five-line explanation of how to sign in — while already signed
in through the desktop app. `claude-acp` advertises no auth methods and
opens a session happily either way (docs/facts/acp-sdk.md §11); it only
fails at the first prompt, so "connected" alone proves nothing and neither
does a session existing.

These tests pin the two failure directions equally: never nag an artist
who is already signed in (checked via `signin_evidence`, real credential
evidence — not just this install's own incomplete `signed_in_agents`
record), and never stay silent forever once genuinely needed just because
it was quiet and dismissible, per the owner's own ask for the shape.
"""

from __future__ import annotations

import pytest

from houdini_agent_panel import signin_evidence
from houdini_agent_panel.client import AgentInfo
from houdini_agent_panel.ui import panel as panel_mod


@pytest.fixture(autouse=True)
def isolated(qapp, monkeypatch):
    monkeypatch.setattr(panel_mod.scene, "hip_dir", lambda: "/tmp")
    monkeypatch.setattr(
        panel_mod.scene, "mcp_servers",
        lambda: [{"name": "fxhoudini", "command": "python", "args": [], "env": []}],
    )
    monkeypatch.setattr(panel_mod._RefreshWorker, "start", lambda self: None)
    # This suite controls the answer explicitly in every test (that IS the
    # feature under test) — never let it spawn a real login shell or read
    # this machine's actual credentials via the real `shellenv.merged`.
    monkeypatch.setattr(panel_mod.shellenv, "merged", lambda base, overrides=None: dict(base))
    panel_mod.reset_shared_state_for_tests()
    yield
    panel_mod.reset_shared_state_for_tests()


def _info(name: str = "Claude Agent") -> AgentInfo:
    return AgentInfo(
        name=name, version="1", protocol_version=1,
        supports_image=False, supports_audio=False, supports_embedded_context=False,
        supports_load_session=False, supports_logout=False, auth_methods=(),
    )


def _connected_widget(qapp, monkeypatch, agent_id: str = "claude-acp"):
    monkeypatch.setattr(panel_mod.AgentPanel, "_start_agent", lambda self, agent_id: None)
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    widget._on_agent_chosen(agent_id)
    client = panel_mod.shared_client(agent_id)
    return widget, client


def test_no_evidence_shows_a_quiet_offer(qapp, monkeypatch):
    monkeypatch.setattr(signin_evidence, "has_credential_evidence", lambda *a, **k: False)
    widget, client = _connected_widget(qapp, monkeypatch)

    client.connected.emit(_info())
    qapp.processEvents()

    assert widget._notice.isHidden() is False
    title = widget._notice._label.text()
    assert "Claude Agent" in title
    # One sentence, per the owner's own ask — not the five-line explanation
    # the after-a-failed-prompt fallback still gives.
    assert title.count(".") <= 1
    assert widget._notice._buttons_row is not None
    widget.shutdown()


def test_credential_evidence_present_shows_nothing(qapp, monkeypatch):
    """The other direction matters just as much: a wrong guess here is the
    "you're already signed in and it nagged you anyway" bug, reported
    twice against this panel in one week."""
    monkeypatch.setattr(signin_evidence, "has_credential_evidence", lambda *a, **k: True)
    widget, client = _connected_widget(qapp, monkeypatch)

    client.connected.emit(_info())
    qapp.processEvents()

    assert widget._notice.isHidden() is True
    widget.shutdown()


def test_our_own_signed_in_record_also_shows_nothing(qapp, monkeypatch):
    """`_is_signed_in()` (a completed turn, this install's own history)
    still counts — checked ahead of the credential evidence, not instead
    of it."""
    monkeypatch.setattr(signin_evidence, "has_credential_evidence", lambda *a, **k: False)
    widget, client = _connected_widget(qapp, monkeypatch)
    widget._settings.signed_in_agents = ["claude-acp"]

    client.connected.emit(_info())
    qapp.processEvents()

    assert widget._notice.isHidden() is True
    widget.shutdown()


def test_credential_check_is_never_called_before_deciding_our_own_record(qapp, monkeypatch):
    """Cheap fact checked first: no reason to touch disk/env at all once
    our own record already answers the question."""
    calls: list[str] = []
    monkeypatch.setattr(
        signin_evidence,
        "has_credential_evidence",
        lambda agent_id, **k: calls.append(agent_id) or False,
    )
    widget, client = _connected_widget(qapp, monkeypatch)
    widget._settings.signed_in_agents = ["claude-acp"]

    client.connected.emit(_info())
    qapp.processEvents()

    assert calls == []
    widget.shutdown()


def test_clicking_sign_in_reaches_the_existing_flow_and_hides_the_offer(qapp, monkeypatch):
    """The sign-in flow itself already exists (`_offer_sign_in`, used from
    Settings) — this only has to reach it sooner, not rebuild it."""
    monkeypatch.setattr(signin_evidence, "has_credential_evidence", lambda *a, **k: False)
    widget, client = _connected_widget(qapp, monkeypatch)
    client.connected.emit(_info())
    qapp.processEvents()
    identifier = widget._notice._id

    reached: list[bool] = []
    monkeypatch.setattr(widget, "_offer_sign_in", lambda: reached.append(True))
    widget._notice.action_clicked.emit(identifier, "")

    assert reached == [True]
    assert widget._notice.isHidden() is True
    widget.shutdown()


def test_dismissing_suppresses_it_for_the_rest_of_this_tab(qapp, monkeypatch):
    monkeypatch.setattr(signin_evidence, "has_credential_evidence", lambda *a, **k: False)
    widget, client = _connected_widget(qapp, monkeypatch)
    client.connected.emit(_info())
    qapp.processEvents()
    identifier = widget._notice._id

    widget._notice.action_clicked.emit("", "")  # not this — sanity: wrong id changes nothing
    widget._notice._on_close()  # the artist's own ✕

    assert "claude-acp" in widget._dismissed_signin_offers
    # "Not now," never "never": nothing about this is written to disk.
    from houdini_agent_panel import settings as settings_mod

    assert identifier not in settings_mod.load().seen_announcements

    # Reconnecting (a restart, a switch back) must not raise it again in
    # THIS tab.
    client.connected.emit(_info())
    qapp.processEvents()
    assert widget._notice.isHidden() is True
    widget.shutdown()


def test_a_second_tab_still_gets_the_offer(qapp, monkeypatch):
    """Dismissal is per-tab, not global — an artist with two panels open
    dismissing it in one must not silence the other."""
    monkeypatch.setattr(signin_evidence, "has_credential_evidence", lambda *a, **k: False)
    first, client = _connected_widget(qapp, monkeypatch)
    client.connected.emit(_info())
    qapp.processEvents()
    first._notice._on_close()
    first.shutdown()

    second, client2 = _connected_widget(qapp, monkeypatch)
    client2.connected.emit(_info())
    qapp.processEvents()

    assert second._notice.isHidden() is False
    second.shutdown()

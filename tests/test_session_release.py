"""Sessions are handed back, not abandoned.

Measured on a real machine, not imagined: after an afternoon in Houdini, one
agent process held three agent-SDK instances, each running the artist's whole
MCP server fleet — twenty-odd node processes, ~300 MB — for conversations
that had long since been closed or switched away from. ACP has
`session/close` and the panel simply never called it.
"""

from __future__ import annotations

import pytest

from houdini_agent_panel import client as client_mod
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


def _panel_with_session(qapp, session_id: str = "live-1"):
    widget = panel_mod.AgentPanel()
    state = sessions.SessionState(
        session_id=session_id, title="Rotor pyro", cwd="/tmp", created_at=0.0
    )
    widget._pool.add(state)
    return widget


def _record_closes(monkeypatch, agent_id: str) -> list[str]:
    closed: list[str] = []
    monkeypatch.setattr(
        panel_mod.shared_client(agent_id), "close_session", closed.append, raising=False
    )
    return closed


def test_deleting_a_conversation_gives_its_session_back(qapp, monkeypatch):
    widget = _panel_with_session(qapp)
    closed = _record_closes(monkeypatch, widget._agent_id)

    widget._on_session_removed("live-1")

    assert closed == ["live-1"]
    widget.shutdown()


def test_switching_agents_gives_every_session_back(qapp, monkeypatch):
    """The sessions are about to become unreachable anyway — but unreachable
    to US is not the same as released by the agent."""
    widget = _panel_with_session(qapp, "live-1")
    widget._pool.add(
        sessions.SessionState(session_id="live-2", title="Other", cwd="/tmp", created_at=1.0)
    )
    closed = _record_closes(monkeypatch, widget._agent_id)
    monkeypatch.setattr(widget, "_start_agent", lambda _id: None)

    widget._on_agent_chosen("codex-acp")

    assert sorted(closed) == ["live-1", "live-2"]
    widget.shutdown()


def test_a_restored_conversation_has_nothing_to_close(qapp, monkeypatch):
    """Its id is ours, not the agent's. Asking the agent to close a session
    it never opened would be a lie about what exists."""
    widget = _panel_with_session(qapp, panel_mod._RESTORED_PREFIX + "abc")
    closed = _record_closes(monkeypatch, widget._agent_id)

    widget._on_session_removed(panel_mod._RESTORED_PREFIX + "abc")

    assert closed == []
    widget.shutdown()


def test_close_is_skipped_when_the_agent_does_not_offer_it(qapp):
    """`supports_close_session` is read from `sessionCapabilities.close`,
    on the same contract as `auth.logout`: absent means unsupported."""
    info = client_mod.AgentInfo(
        name="x", version="1", protocol_version=1,
        supports_image=False, supports_audio=False, supports_embedded_context=False,
        supports_load_session=False, supports_logout=False, auth_methods=(),
    )
    assert info.supports_close_session is False

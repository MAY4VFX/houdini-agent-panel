"""The model picker, fed by what the agent actually offers.

ACP keeps model selection in session config options rather than as a concept
of its own, so nothing reached the chip and it stayed hidden forever. These
tests pin the wiring end to end: options arrive, the chip appears, picking
one tells the agent.
"""

from __future__ import annotations

import pytest

from houdini_agent_panel import sessions
from houdini_agent_panel.client import ConfigChoice, ConfigOption
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


def _model_option() -> ConfigOption:
    return ConfigOption(
        id="model",
        name="Model",
        current_value="opus[1m]",
        choices=(
            ConfigChoice("default", "Default (recommended)", "Best for everyday tasks"),
            ConfigChoice("opus[1m]", "Opus (1M context)", "Opus 5 with 1M context"),
        ),
        category="model_config",
    )


def _panel_with_session(qapp):
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    client = panel_mod.shared_client()
    state = sessions.SessionState(
        session_id="s1", title="New conversation", cwd="/tmp", created_at=0.0
    )
    client.session_started.emit(state.session_id, state)
    qapp.processEvents()
    return widget, client


def test_model_options_show_the_chip(qapp):
    widget, client = _panel_with_session(qapp)

    client.config_options_changed.emit("s1", [_model_option()])
    qapp.processEvents()

    chip = widget._composer.model_chip
    assert not chip.isHidden()
    assert chip.currentData() == "opus[1m]", "the chip must open on what the agent is using"
    widget.shutdown()


def test_descriptions_become_tooltips(qapp):
    """Agents put genuinely useful text there — dropping it would leave the
    artist choosing between opaque model names."""
    widget, client = _panel_with_session(qapp)

    client.config_options_changed.emit("s1", [_model_option()])
    qapp.processEvents()

    assert "1M context" in widget._composer.model_chip.toolTip()
    widget.shutdown()


def test_no_model_option_means_no_chip(qapp):
    """The rule holds: the agent doesn't offer it, the control isn't drawn."""
    widget, client = _panel_with_session(qapp)

    other = ConfigOption(
        id="effort", name="Effort", current_value="high",
        choices=(ConfigChoice("low", "Low"), ConfigChoice("high", "High")),
    )
    client.config_options_changed.emit("s1", [other])
    qapp.processEvents()

    assert widget._composer.model_chip.isHidden()
    widget.shutdown()


def test_picking_a_model_tells_the_agent(qapp, monkeypatch):
    widget, client = _panel_with_session(qapp)
    client.config_options_changed.emit("s1", [_model_option()])
    qapp.processEvents()

    sent: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        client, "set_config_option",
        lambda session_id, config_id, value: sent.append((session_id, config_id, value)),
    )

    widget._composer.model_selected.emit("default")
    qapp.processEvents()

    assert sent == [("s1", "model", "default")]
    widget.shutdown()


def test_options_are_remembered_per_session(qapp):
    """Two conversations can run different models; switching between them
    must not show the other one's choice."""
    widget, client = _panel_with_session(qapp)
    client.config_options_changed.emit("s1", [_model_option()])
    qapp.processEvents()

    state = widget._pool.get("s1")
    assert state is not None
    assert [o.id for o in state.config_options] == ["model"]
    widget.shutdown()

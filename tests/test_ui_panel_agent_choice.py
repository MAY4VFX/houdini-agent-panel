"""Add a custom agent, then pick it right away.

A regression found only by testing live in Houdini: the panel holds a
snapshot of the settings from the moment it opened, while the "Agents" screen
writes an added agent straight to the file. Saving the snapshot on top erased
the fresh record, and launching failed with "the agent isn't in the registry
or among custom agents" — while everything worked after restarting Houdini,
which is what made the bug so treacherous.

A separate file from test_ui_panel.py: that one is being edited in parallel by
another session.
"""

from __future__ import annotations

import pytest

from houdini_agent_panel import settings as settings_mod
from houdini_agent_panel.ui import panel as panel_mod


@pytest.fixture(autouse=True)
def isolated_panel_state(qapp, monkeypatch):
    monkeypatch.setattr(panel_mod.scene, "hip_dir", lambda: "/tmp")
    monkeypatch.setattr(
        panel_mod.scene,
        "mcp_servers",
        lambda: [{"name": "fxhoudini", "command": "python", "args": [], "env": []}],
    )
    monkeypatch.setattr(panel_mod._RefreshWorker, "start", lambda self: None)
    panel_mod.reset_shared_state_for_tests()
    yield
    panel_mod.reset_shared_state_for_tests()


def test_choosing_a_just_added_custom_agent_does_not_erase_it(qapp, monkeypatch):
    widget = panel_mod.AgentPanel()
    qapp.processEvents()

    # The "Agents" screen writes to the file directly, past the panel's
    # snapshot — exactly as
    # AgentsView._on_add_custom.
    fresh = settings_mod.load()
    fresh.custom_agents.append(
        settings_mod.CustomAgent(
            id="custom:my", name="Mine", command="/usr/bin/env", args=["python3"]
        )
    )
    settings_mod.save(fresh)

    # The panel must never reach a real process launch.
    started: list[str] = []
    monkeypatch.setattr(widget, "_start_agent", lambda agent_id: started.append(agent_id))

    widget._on_agent_chosen("custom:my")

    on_disk = settings_mod.load()
    assert [a.id for a in on_disk.custom_agents] == ["custom:my"], (
        "picking an agent wiped the record that had just been added"
    )
    assert on_disk.default_agent == "custom:my"
    assert started == ["custom:my"]

    # And the panel's own snapshot is fresh now — _launch_spec will find the agent.
    assert [a.id for a in widget._settings.custom_agents] == ["custom:my"]

    widget.shutdown()


def test_remember_seen_does_not_clobber_concurrent_settings_writes(qapp):
    widget = panel_mod.AgentPanel()
    qapp.processEvents()

    fresh = settings_mod.load()
    fresh.custom_agents.append(
        settings_mod.CustomAgent(id="custom:other", name="Other", command="/bin/true")
    )
    settings_mod.save(fresh)

    widget._remember_seen("ann-1")

    on_disk = settings_mod.load()
    assert "ann-1" in on_disk.seen_announcements
    assert [a.id for a in on_disk.custom_agents] == ["custom:other"]

    widget.shutdown()

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

from pathlib import Path

import pytest

from houdini_agent_panel import registry, runtime
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


def test_launching_an_npx_agent_writes_its_manifest(monkeypatch):
    """Root cause behind a real "the Update button does nothing" report:
    an npx agent runs fine on nothing but npx's own on-demand fetch, so the
    old `_LaunchPrepWorker` — which called `runtime.launch_spec` directly —
    could leave it running for a long time without ever writing a manifest.
    The Settings screen's agent row and the update banner both end up
    trusting the manifest (`ui/agents.py::_installed_record`,
    `updates.py::check`), so "not installed" while it's plainly running was
    the visible result. Preparing the launch through `runtime.install_agent`
    instead fixes this — cheap/no-op for anything already installed, since
    `install_agent`'s own first line is `if is_installed(entry): return
    launch_spec(entry)`.
    """
    entry = registry.AgentEntry(
        id="npx-agent",
        name="Npx Agent",
        version="1.0.0",
        npx=registry.NpxDistribution(package="@test/agent@1.0.0", args=["--acp"]),
    )
    monkeypatch.setattr(registry, "fetch_registry", lambda **k: [entry])
    monkeypatch.setattr("houdini_agent_panel.node.ensure_node", lambda **k: Path("/fake/node"))
    monkeypatch.setattr(
        "houdini_agent_panel.node.npx_argv",
        lambda node_bin, package, args: [str(node_bin), "/fake/npx-cli.js", "--yes", package, *args],
    )

    assert runtime.installed_version("npx-agent") is None  # never installed, same as the real bug

    worker = panel_mod._LaunchPrepWorker("npx-agent", settings_mod.Settings())
    ready_calls = []
    prep_failed_calls = []
    worker.ready.connect(lambda spec, name: ready_calls.append((spec, name)))
    worker.prep_failed.connect(prep_failed_calls.append)
    worker.run()  # synchronous — this is testing the worker's logic, not real threading

    assert prep_failed_calls == []
    assert len(ready_calls) == 1
    assert runtime.installed_version("npx-agent") == "1.0.0", (
        "launching it must leave the same manifest an explicit Install/Update would"
    )


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


def test_the_chip_names_the_chosen_agent_even_before_it_runs(qapp, monkeypatch):
    """Opening with autostart off showed a bare dot and no name, while the
    menu behind that same chip correctly showed the agent as selected. The
    chip answers "which agent is chosen", and settings know that before
    anything is launched.
    """
    from houdini_agent_panel import settings as settings_mod
    from houdini_agent_panel.ui import panel as panel_mod

    current = settings_mod.load()
    current.default_agent = "gemini"
    current.autostart_agent = False
    settings_mod.save(current)

    widget = panel_mod.AgentPanel()
    started: list[str] = []
    monkeypatch.setattr(widget, "_start_agent", lambda agent_id: started.append(agent_id))
    widget._boot()
    qapp.processEvents()

    assert not started, "autostart is off — nothing may be launched"
    assert widget._header._agent_button.text() == "Gemini CLI", (
        f"the chip is blank while the menu shows Gemini selected: "
        f"{widget._header._agent_button.text()!r}"
    )
    widget.shutdown()

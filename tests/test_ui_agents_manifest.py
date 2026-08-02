"""Installed state comes from the manifest, not from settings.

The artist's report: "I click Install, it instantly says installed — so it's
already there, but then why doesn't it remember?" Two sources of truth
disagreed. An agent installed by the CLI (`--agents opencode`) writes a
manifest and no settings entry, so the row read "not installed"; clicking
Install found the manifest, returned with no download at all, and only then
wrote settings.
"""

from __future__ import annotations

import json

from houdini_agent_panel import paths
from houdini_agent_panel import settings as settings_module
from houdini_agent_panel.registry import AgentEntry, BinaryDistribution
from houdini_agent_panel.ui.agents import AgentsView


def _entry(agent_id: str = "opencode") -> AgentEntry:
    return AgentEntry(
        id=agent_id,
        name="OpenCode",
        version="1.18.11",
        binaries={
            "darwin-aarch64": BinaryDistribution(
                archive="https://example.test/a.zip", cmd="./opencode", sha256="0" * 64
            )
        },
    )


def _write_manifest(agent_id: str, version: str) -> None:
    (paths.agent_dir(agent_id) / "manifest.json").write_text(
        json.dumps({"agent_id": agent_id, "version": version, "kind": "binary"}), "utf-8"
    )


def _button_labels(view: AgentsView) -> set[str]:
    from houdini_agent_panel.ui.qt import QtWidgets

    return {b.text() for b in view.findChildren(QtWidgets.QPushButton) if b.text()}


def test_agent_installed_by_cli_is_recognised_without_a_settings_entry(qapp, monkeypatch):
    """Exactly the artist's case: the CLI installed it, settings know nothing."""
    monkeypatch.setattr(
        "houdini_agent_panel.registry.platform_key", lambda: "darwin-aarch64"
    )
    _write_manifest("opencode", "1.18.11")
    assert settings_module.load().installed_agents == {}, "settings must stay empty here"

    view = AgentsView()
    view.set_agents([_entry()])

    labels = _button_labels(view)
    assert "Remove" in labels, f"a CLI-installed agent must read as installed: {labels}"
    assert "Install" not in labels


def test_settings_entry_without_a_manifest_does_not_claim_installed(qapp, monkeypatch):
    """The mirror case: a stale settings entry must not pretend an agent that
    was wiped from disk is still there."""
    monkeypatch.setattr(
        "houdini_agent_panel.registry.platform_key", lambda: "darwin-aarch64"
    )
    current = settings_module.load()
    current.installed_agents["opencode"] = settings_module.InstalledAgent(
        agent_id="opencode", version="1.18.11", kind="binary"
    )
    settings_module.save(current)

    view = AgentsView()
    view.set_agents([_entry()])

    assert "Install" in _button_labels(view)

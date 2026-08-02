"""The panel offers the design's six, not the whole registry.

Regression: `FEATURED_AGENT_IDS` had existed from the start, but nothing
filtered by it — the "Agents" screen got the entire registry and showed the
artist close to forty entries like Cursor, Devin, and Cline, which aren't in
the design. Caught not by tests, but by looking at the live panel in
Houdini.
"""

from __future__ import annotations

from houdini_agent_panel import registry
from houdini_agent_panel.registry import AgentEntry, NpxDistribution


def _entry(agent_id: str) -> AgentEntry:
    return AgentEntry(
        id=agent_id,
        name=agent_id,
        version="1.0.0",
        npx=NpxDistribution(package=f"{agent_id}@1.0.0"),
    )


def test_featured_keeps_only_the_designed_six():
    everything = [_entry(i) for i in ("cursor", "devin", "claude-acp", "cline", "opencode")]

    chosen = registry.featured(everything)

    assert [e.id for e in chosen] == ["claude-acp", "opencode"]


def test_featured_uses_the_declared_order_not_the_registry_order():
    """The registry is sorted by id, and that order means nothing to a
    human — it should be shown in the order the design specifies."""
    shuffled = [_entry(i) for i in ("opencode", "gemini", "claude-acp", "kimi")]

    chosen = registry.featured(shuffled)

    assert [e.id for e in chosen] == ["claude-acp", "opencode", "gemini", "kimi"]


def test_featured_survives_an_agent_missing_from_the_registry():
    """An agent got renamed or removed from the registry — the panel just
    doesn't show it, instead of drawing a blank row with a name it has
    nowhere to get."""
    chosen = registry.featured([_entry("opencode")])

    assert [e.id for e in chosen] == ["opencode"]


def test_all_six_from_the_design_are_listed():
    assert set(registry.FEATURED_AGENT_IDS) == {
        "claude-acp", "codex-acp", "grok-build", "opencode", "gemini", "kimi",
    }

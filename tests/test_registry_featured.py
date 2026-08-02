"""Панель предлагает шестёрку из дизайна, а не весь реестр.

Регрессия: `FEATURED_AGENT_IDS` существовал с самого начала, но никто им не
фильтровал — экран «Агенты» получал весь реестр целиком и показывал художнику
под сорок записей вроде Cursor, Devin и Cline, которых в дизайне нет. Поймано
не тестами, а взглядом на живую панель в Houdini.
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
    """Реестр отсортирован по идентификатору, и для человека этот порядок не
    значит ничего — показывать надо в том порядке, что задан в дизайне."""
    shuffled = [_entry(i) for i in ("opencode", "gemini", "claude-acp", "kimi")]

    chosen = registry.featured(shuffled)

    assert [e.id for e in chosen] == ["claude-acp", "opencode", "gemini", "kimi"]


def test_featured_survives_an_agent_missing_from_the_registry():
    """Агента переименовали или убрали из реестра — панель просто его не
    показывает, а не рисует пустую строку с именем, которое ей неоткуда взять."""
    chosen = registry.featured([_entry("opencode")])

    assert [e.id for e in chosen] == ["opencode"]


def test_all_six_from_the_design_are_listed():
    assert set(registry.FEATURED_AGENT_IDS) == {
        "claude-acp", "codex-acp", "grok-build", "opencode", "gemini", "kimi",
    }

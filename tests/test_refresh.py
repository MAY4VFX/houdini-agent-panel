from __future__ import annotations

from houdini_agent_panel import announcements, refresh, updates
from houdini_agent_panel.registry import AgentEntry
from houdini_agent_panel.settings import InstalledAgent, Settings


def _feed(*items: dict) -> dict:
    return {"version": 1, "announcements": list(items)}


def _item(**overrides) -> dict:
    base = {"id": "ann-1", "severity": "info", "title": "Title"}
    base.update(overrides)
    return base


def test_both_toggles_off_makes_no_network_call(fetcher):
    settings = Settings(check_updates=False, show_announcements=False)

    result = refresh.daily_refresh(settings=settings, panel_version="0.1.0", fetch=fetcher)

    assert result.updates == []
    assert result.announcements == []
    assert result.checked is False
    assert fetcher.calls == []


def test_only_updates_enabled_checks_only_updates(fetcher):
    fetcher.add_json(
        updates.PYPI_URL.format(name="houdini-agent-panel"), {"info": {"version": "9.9.9"}}
    )
    fetcher.add_json(updates.PYPI_URL.format(name="fxhoudinimcp"), {"info": {"version": "2.10.0"}})
    settings = Settings(check_updates=True, show_announcements=False)

    result = refresh.daily_refresh(
        settings=settings,
        panel_version="0.1.0",
        fetch=fetcher,
        entries=[],
    )

    assert result.checked is True
    assert any(u.kind == "panel" for u in result.updates)
    assert result.announcements == []
    assert announcements.FEED_URL not in fetcher.calls


def test_only_announcements_enabled_checks_only_announcements(fetcher):
    fetcher.add_json(announcements.FEED_URL, _feed(_item(id="a")))
    settings = Settings(check_updates=False, show_announcements=True)

    result = refresh.daily_refresh(settings=settings, panel_version="0.1.0", fetch=fetcher)

    assert result.checked is True
    assert result.updates == []
    assert [a.id for a in result.announcements] == ["a"]


def test_both_enabled_combines_results(fetcher):
    fetcher.add_json(
        updates.PYPI_URL.format(name="houdini-agent-panel"), {"info": {"version": "9.9.9"}}
    )
    fetcher.add_json(updates.PYPI_URL.format(name="fxhoudinimcp"), {"info": {"version": "2.10.0"}})
    fetcher.add_json(announcements.FEED_URL, _feed(_item(id="a")))
    settings = Settings(check_updates=True, show_announcements=True)

    entry = AgentEntry(id="claude-acp", name="Claude Agent", version="2.0.0")
    settings.installed_agents["claude-acp"] = InstalledAgent(
        agent_id="claude-acp", version="1.0.0", kind="npx"
    )

    result = refresh.daily_refresh(
        settings=settings, panel_version="0.1.0", fetch=fetcher, entries=[entry]
    )

    assert result.checked is True
    assert {u.kind for u in result.updates} == {"agent", "panel"}
    assert [a.id for a in result.announcements] == ["a"]


def test_network_error_on_one_side_does_not_break_the_other(fetcher):
    # The feed has no response registered in FakeFetcher at all -
    # announcements.check() will raise NetworkError, but updates must
    # remain available.
    fetcher.add_json(
        updates.PYPI_URL.format(name="houdini-agent-panel"), {"info": {"version": "9.9.9"}}
    )
    fetcher.add_json(updates.PYPI_URL.format(name="fxhoudinimcp"), {"info": {"version": "2.10.0"}})
    settings = Settings(check_updates=True, show_announcements=True)

    result = refresh.daily_refresh(settings=settings, panel_version="0.1.0", fetch=fetcher, entries=[])

    assert result.announcements == []  # network unavailable - quietly empty, not a crash
    assert any(u.kind == "panel" for u in result.updates)
    assert result.checked is True  # at least one real request still went out


def test_total_network_failure_never_raises(fetcher):
    settings = Settings(check_updates=True, show_announcements=True)

    # No address has a response - both check() calls will raise NetworkError.
    result = refresh.daily_refresh(settings=settings, panel_version="0.1.0", fetch=fetcher, entries=[])

    assert result.updates == []
    assert result.announcements == []
    assert result.checked is True  # attempts were made - none of them just succeeded


def test_force_is_passed_through_to_both_checks(fetcher):
    fetcher.add_json(announcements.FEED_URL, _feed(_item(id="a")))
    fetcher.add_json(
        updates.PYPI_URL.format(name="houdini-agent-panel"), {"info": {"version": "9.9.9"}}
    )
    fetcher.add_json(updates.PYPI_URL.format(name="fxhoudinimcp"), {"info": {"version": "2.10.0"}})
    settings = Settings(check_updates=True, show_announcements=True)

    refresh.daily_refresh(settings=settings, panel_version="0.1.0", fetch=fetcher, entries=[])
    calls_after_first = len(fetcher.calls)

    refresh.daily_refresh(
        settings=settings, panel_version="0.1.0", fetch=fetcher, entries=[], force=True
    )

    assert len(fetcher.calls) > calls_after_first

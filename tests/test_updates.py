from __future__ import annotations

from datetime import datetime, timedelta, timezone

from houdini_agent_panel import updates
from houdini_agent_panel.registry import AgentEntry
from houdini_agent_panel.settings import InstalledAgent, Settings


# --- is_newer / version comparison ----------------------------------------


def test_is_newer_basic():
    assert updates.is_newer("1.2.0", "1.1.9") is True
    assert updates.is_newer("1.1.9", "1.2.0") is False


def test_is_newer_equal_is_not_newer():
    assert updates.is_newer("1.0.0", "1.0.0") is False


def test_is_newer_trailing_zero_is_equal():
    assert updates.is_newer("1.2.0", "1.2") is False
    assert updates.is_newer("1.2", "1.2.0") is False


def test_is_newer_final_beats_prerelease():
    assert updates.is_newer("1.0.0", "1.0.0rc1") is True
    assert updates.is_newer("1.0.0rc1", "1.0.0") is False


def test_is_newer_prerelease_order():
    assert updates.is_newer("1.0.0b1", "1.0.0a2") is True
    assert updates.is_newer("1.0.0rc2", "1.0.0rc1") is True


def test_is_newer_dev_before_prerelease():
    # a dev release with no pre-release and no post comes before any pre-release at all.
    assert updates.is_newer("1.0.0a1", "1.0.0.dev1") is True


def test_is_newer_post_after_final():
    assert updates.is_newer("1.0.0.post1", "1.0.0") is True


def test_is_newer_garbage_is_false_in_either_position():
    assert updates.is_newer("not-a-version", "1.0.0") is False
    assert updates.is_newer("1.0.0", "not-a-version") is False
    assert updates.is_newer("garbage", "also garbage") is False


def test_compare_versions_none_on_garbage():
    assert updates.compare_versions("garbage", "1.0.0") is None
    assert updates.compare_versions("1.0.0", "1.0.0") == 0
    assert updates.compare_versions("2.0.0", "1.0.0") == 1
    assert updates.compare_versions("1.0.0", "2.0.0") == -1


# --- check(): the toggle -----------------------------------------------------


def test_check_disabled_makes_no_network_call(fetcher):
    settings = Settings(check_updates=False)
    result = updates.check(settings=settings, entries=[], fetch=fetcher)
    assert result == []
    assert fetcher.calls == []


# --- check(): agents -------------------------------------------------------


def test_check_detects_agent_update(fetcher):
    settings = Settings(check_updates=True)
    settings.installed_agents["claude-acp"] = InstalledAgent(
        agent_id="claude-acp", version="1.0.0", kind="npx"
    )
    entry = AgentEntry(id="claude-acp", name="Claude Agent", version="1.2.0")

    result = updates.check(
        settings=settings,
        entries=[entry],
        fetch=fetcher,
        panel_version="0.1.0",
        fx_version="2.10.0",
    )

    agent_updates = [u for u in result if u.kind == "agent"]
    assert len(agent_updates) == 1
    assert agent_updates[0].target == "claude-acp"
    assert agent_updates[0].current == "1.0.0"
    assert agent_updates[0].latest == "1.2.0"


def test_check_skips_not_installed_agent(fetcher):
    settings = Settings(check_updates=True)
    entry = AgentEntry(id="claude-acp", name="Claude Agent", version="1.2.0")

    result = updates.check(
        settings=settings, entries=[entry], fetch=fetcher, panel_version="0.1.0", fx_version="2.10.0"
    )

    assert [u for u in result if u.kind == "agent"] == []


# --- check(): panel/fx via PyPI ----------------------------------------


def test_check_detects_panel_update(fetcher):
    fetcher.add_json(
        updates.PYPI_URL.format(name="houdini-agent-panel"), {"info": {"version": "9.9.9"}}
    )
    fetcher.add_json(updates.PYPI_URL.format(name="fxhoudinimcp"), {"info": {"version": "2.10.0"}})
    settings = Settings(check_updates=True)

    result = updates.check(
        settings=settings, entries=[], fetch=fetcher, panel_version="0.1.0", fx_version="2.10.0"
    )

    panel_updates = [u for u in result if u.kind == "panel"]
    assert len(panel_updates) == 1
    assert panel_updates[0].latest == "9.9.9"
    assert panel_updates[0].current == "0.1.0"
    assert [u for u in result if u.kind == "fx"] == []  # fx is already on the latest version


def test_check_pypi_failure_for_one_package_does_not_hide_the_other(fetcher):
    # There's no response registered for houdini-agent-panel at all - FakeFetcher will raise NetworkError.
    fetcher.add_json(updates.PYPI_URL.format(name="fxhoudinimcp"), {"info": {"version": "9.9.9"}})
    settings = Settings(check_updates=True)

    result = updates.check(
        settings=settings, entries=[], fetch=fetcher, panel_version="0.1.0", fx_version="2.10.0"
    )

    assert [u for u in result if u.kind == "panel"] == []
    fx_updates = [u for u in result if u.kind == "fx"]
    assert len(fx_updates) == 1
    assert fx_updates[0].latest == "9.9.9"


# --- check(): the once-a-day cache --------------------------------------------------


def test_check_uses_cache_within_a_day(fetcher):
    fetcher.add_json(
        updates.PYPI_URL.format(name="houdini-agent-panel"), {"info": {"version": "9.9.9"}}
    )
    fetcher.add_json(updates.PYPI_URL.format(name="fxhoudinimcp"), {"info": {"version": "2.10.0"}})
    settings = Settings(check_updates=True)
    now = datetime(2026, 8, 2, tzinfo=timezone.utc)

    first = updates.check(
        settings=settings, entries=[], fetch=fetcher, panel_version="0.1.0", fx_version="2.10.0", now=now
    )
    calls_after_first = len(fetcher.calls)
    assert calls_after_first > 0

    second = updates.check(
        settings=settings,
        entries=[],
        fetch=fetcher,
        panel_version="0.1.0",
        fx_version="2.10.0",
        now=now + timedelta(hours=1),
    )

    assert second == first
    assert len(fetcher.calls) == calls_after_first  # not a single new request


def test_check_force_bypasses_cache(fetcher):
    fetcher.add_json(
        updates.PYPI_URL.format(name="houdini-agent-panel"), {"info": {"version": "9.9.9"}}
    )
    fetcher.add_json(updates.PYPI_URL.format(name="fxhoudinimcp"), {"info": {"version": "2.10.0"}})
    settings = Settings(check_updates=True)
    now = datetime(2026, 8, 2, tzinfo=timezone.utc)

    updates.check(
        settings=settings, entries=[], fetch=fetcher, panel_version="0.1.0", fx_version="2.10.0", now=now
    )
    calls_after_first = len(fetcher.calls)

    updates.check(
        settings=settings,
        entries=[],
        fetch=fetcher,
        panel_version="0.1.0",
        fx_version="2.10.0",
        now=now + timedelta(minutes=1),
        force=True,
    )

    assert len(fetcher.calls) > calls_after_first


def test_check_refreshes_after_a_day(fetcher):
    fetcher.add_json(
        updates.PYPI_URL.format(name="houdini-agent-panel"), {"info": {"version": "9.9.9"}}
    )
    fetcher.add_json(updates.PYPI_URL.format(name="fxhoudinimcp"), {"info": {"version": "2.10.0"}})
    settings = Settings(check_updates=True)
    now = datetime(2026, 8, 2, tzinfo=timezone.utc)

    updates.check(
        settings=settings, entries=[], fetch=fetcher, panel_version="0.1.0", fx_version="2.10.0", now=now
    )
    calls_after_first = len(fetcher.calls)

    updates.check(
        settings=settings,
        entries=[],
        fetch=fetcher,
        panel_version="0.1.0",
        fx_version="2.10.0",
        now=now + timedelta(days=1, minutes=1),
    )

    assert len(fetcher.calls) > calls_after_first

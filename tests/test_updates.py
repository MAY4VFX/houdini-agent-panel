from __future__ import annotations

from datetime import datetime, timedelta, timezone

from houdini_agent_panel import runtime, updates
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
    # The manifest is what `check()` now reads (see updates.py) — the
    # settings record is set too, matching what a real install/update
    # leaves behind, but it's the manifest write that actually drives this.
    installed_entry = AgentEntry(id="claude-acp", name="Claude Agent", version="1.0.0")
    runtime._write_manifest(installed_entry, kind="npx")
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


def test_check_reads_the_manifest_not_settings_when_they_disagree(fetcher):
    """A real bug, found live: an npx agent can run for a long time on
    nothing but npx's own on-demand fetch, with `settings.installed_agents`
    remembering one version while the manifest — the thing the Settings
    screen's own row already trusts (`ui/agents.py::_installed_record`) —
    disagrees or is missing outright. Whichever this reads becomes what the
    update banner claims, and reading a different source than the Settings
    row is exactly how they end up disagreeing about the same agent.
    """
    settings = Settings(check_updates=True)
    # settings.installed_agents says nothing at all about this agent — the
    # exact state found on a real machine (settings record gone, manifest
    # gone too) — yet the manifest is what decides here.
    installed_entry = AgentEntry(id="claude-acp", name="Claude Agent", version="1.0.0")
    runtime._write_manifest(installed_entry, kind="npx")
    entry = AgentEntry(id="claude-acp", name="Claude Agent", version="1.2.0")

    result = updates.check(
        settings=settings, entries=[entry], fetch=fetcher, panel_version="0.1.0", fx_version="2.10.0"
    )

    agent_updates = [u for u in result if u.kind == "agent"]
    assert len(agent_updates) == 1
    assert agent_updates[0].current == "1.0.0"
    assert agent_updates[0].latest == "1.2.0"


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


def test_check_uses_cache_within_the_fresh_start_window(fetcher):
    """`fresh_start=True` (the default — a panel that just opened) trusts
    the cache for `_FRESH_START_MAX_AGE`, not a day — see that constant's
    own comment for why a full day stopped being right."""
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
        now=now + timedelta(minutes=5),
    )

    assert second == first
    assert len(fetcher.calls) == calls_after_first  # not a single new request


def test_a_fresh_start_past_the_short_window_checks_again(fetcher):
    """The exact report this fixes: the owner restarted Houdini repeatedly
    while several versions shipped in an hour, and a day-long cache said
    nothing every time. An hour is well past `_FRESH_START_MAX_AGE`
    (minutes) — a fresh panel start that old must trigger a real check,
    not silently reuse an answer from before the newer releases existed.
    """
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
        now=now + timedelta(hours=1),
        fresh_start=True,
    )

    assert len(fetcher.calls) > calls_after_first


def test_a_session_recheck_trusts_the_cache_longer_than_a_fresh_start_would(fetcher):
    """`fresh_start=False` — a periodic re-check from a panel that has
    already been open for a while (`ui/panel.py`'s own recurring timer) —
    must not re-hit PyPI every time it fires just because an hour is well
    past the SHORT window; it has its own, longer one
    (`_SESSION_MAX_AGE`), so a panel left open all day doesn't poll PyPI
    every few minutes."""
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

    second = updates.check(
        settings=settings,
        entries=[],
        fetch=fetcher,
        panel_version="0.1.0",
        fx_version="2.10.0",
        now=now + timedelta(hours=1),
        fresh_start=False,
    )

    assert second == first
    assert len(fetcher.calls) == calls_after_first  # not a single new request


def test_a_session_recheck_still_refreshes_once_its_own_longer_window_passes(fetcher):
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
        now=now + updates._SESSION_MAX_AGE + timedelta(minutes=1),
        fresh_start=False,
    )

    assert len(fetcher.calls) > calls_after_first


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


def test_the_cache_is_not_trusted_across_a_panel_upgrade(tmp_path, monkeypatch):
    """Results are cached, and the panel is the thing that updates most
    often — so after an upgrade the old build's answer is still there,
    telling a freshly-updated panel to upgrade to the version it just left.
    Reported as 0.1.7 being offered 0.1.5.
    """
    from datetime import datetime, timezone

    from houdini_agent_panel import updates

    now = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(updates, "_current_panel_version", lambda: "0.1.4")
    updates._write_cache(now, [
        updates.Update(kind="panel", target="houdini-agent-panel",
                       label="houdini-agent-panel 0.1.5", current="0.1.4", latest="0.1.5")
    ])
    assert updates._read_cache(now, fresh_start=True) is not None, (
        "same version, same moment — should be reused"
    )

    monkeypatch.setattr(updates, "_current_panel_version", lambda: "0.1.7")
    assert updates._read_cache(now, fresh_start=True) is None, (
        "a newer panel reused an older build's answer about itself"
    )

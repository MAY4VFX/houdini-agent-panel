from __future__ import annotations

from datetime import datetime, timedelta, timezone

from houdini_agent_panel import announcements
from houdini_agent_panel.settings import Settings


def _feed(*items: dict) -> dict:
    return {"version": 1, "announcements": list(items)}


def _item(**overrides) -> dict:
    base = {
        "id": "ann-1",
        "severity": "info",
        "title": "Title",
        "body": "Body text",
        "buttons": [{"label": "OK", "url": "https://example.com"}],
        "panel_versions": "",
        "expires": "",
    }
    base.update(overrides)
    return base


# --- parse_feed: tolerance for broken records --------------------------------


def test_parse_feed_empty():
    assert announcements.parse_feed({"version": 1, "announcements": []}) == []


def test_parse_feed_not_a_dict():
    assert announcements.parse_feed("garbage") == []
    assert announcements.parse_feed(None) == []


def test_parse_feed_skips_broken_record_keeps_the_rest():
    payload = _feed(
        {"id": "", "title": "doesn't count without an id"},  # empty id - broken record
        {"title": "no id at all"},  # no id
        {"id": "ok-1"},  # no title
        _item(id="ok-2", title="A valid record"),
    )
    result = announcements.parse_feed(payload)
    assert [a.id for a in result] == ["ok-2"]


def test_parse_feed_unknown_severity_becomes_info():
    payload = _feed(_item(id="a", severity="critical-not-a-real-level"))
    result = announcements.parse_feed(payload)
    assert result[0].severity == "info"


def test_parse_feed_missing_severity_defaults_to_info():
    item = _item(id="a")
    del item["severity"]
    result = announcements.parse_feed(_feed(item))
    assert result[0].severity == "info"


def test_parse_feed_known_severities_kept():
    result = announcements.parse_feed(_feed(_item(id="a", severity="blocking")))
    assert result[0].severity == "blocking"


def test_parse_feed_buttons_with_bad_entries_are_dropped():
    payload = _feed(
        _item(
            id="a",
            buttons=[
                {"label": "good", "url": "https://x"},
                {"url": "https://y"},  # no label
                "garbage",
            ],
        )
    )
    result = announcements.parse_feed(payload)
    assert [b.label for b in result[0].buttons] == ["good"]


# --- applicable(): expiration, targeting, "already seen" -------------------


def test_applicable_hides_seen():
    items = announcements.parse_feed(_feed(_item(id="a")))
    result = announcements.applicable(items, panel_version="0.1.0", seen=["a"])
    assert result == []


def test_applicable_hides_expired():
    items = announcements.parse_feed(_feed(_item(id="a", expires="2020-01-01T00:00:00Z")))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = announcements.applicable(items, panel_version="0.1.0", seen=[], now=now)
    assert result == []


def test_applicable_keeps_not_yet_expired():
    items = announcements.parse_feed(_feed(_item(id="a", expires="2030-01-01T00:00:00Z")))
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = announcements.applicable(items, panel_version="0.1.0", seen=[], now=now)
    assert [a.id for a in result] == ["a"]


def test_applicable_garbage_expires_is_unlimited():
    items = announcements.parse_feed(_feed(_item(id="a", expires="not-a-date")))
    result = announcements.applicable(items, panel_version="0.1.0", seen=[])
    assert [a.id for a in result] == ["a"]


def test_applicable_panel_versions_range_matches():
    items = announcements.parse_feed(_feed(_item(id="a", panel_versions=">=0.2,<0.4")))
    assert [a.id for a in announcements.applicable(items, panel_version="0.3.0", seen=[])] == ["a"]
    assert announcements.applicable(items, panel_version="0.5.0", seen=[]) == []
    assert announcements.applicable(items, panel_version="0.1.0", seen=[]) == []


def test_applicable_empty_panel_versions_matches_everyone():
    items = announcements.parse_feed(_feed(_item(id="a", panel_versions="")))
    assert [a.id for a in announcements.applicable(items, panel_version="99.0.0", seen=[])] == ["a"]


def test_applicable_broken_specifier_excludes_announcement():
    # A deliberate choice: a targeting error must not show the message to everyone.
    items = announcements.parse_feed(_feed(_item(id="a", panel_versions=">= garbage")))
    assert announcements.applicable(items, panel_version="0.3.0", seen=[]) == []


# --- check(): toggle, network, cache --------------------------------------


def test_check_disabled_makes_no_network_call(fetcher):
    settings = Settings(show_announcements=False)
    result = announcements.check(settings=settings, panel_version="0.1.0", fetch=fetcher)
    assert result == []
    assert fetcher.calls == []


def test_check_fetches_and_filters(fetcher):
    fetcher.add_json(announcements.FEED_URL, _feed(_item(id="a")))
    settings = Settings(show_announcements=True)

    result = announcements.check(settings=settings, panel_version="0.1.0", fetch=fetcher)

    assert [a.id for a in result] == ["a"]
    assert fetcher.calls == [announcements.FEED_URL]


def test_check_does_not_show_already_seen(fetcher):
    fetcher.add_json(announcements.FEED_URL, _feed(_item(id="a")))
    settings = Settings(show_announcements=True, seen_announcements=["a"])

    result = announcements.check(settings=settings, panel_version="0.1.0", fetch=fetcher)

    assert result == []


def test_check_uses_cache_within_a_day(fetcher):
    fetcher.add_json(announcements.FEED_URL, _feed(_item(id="a")))
    settings = Settings(show_announcements=True)
    now = datetime(2026, 8, 2, tzinfo=timezone.utc)

    announcements.check(settings=settings, panel_version="0.1.0", fetch=fetcher, now=now)
    calls_after_first = len(fetcher.calls)
    assert calls_after_first == 1

    announcements.check(
        settings=settings, panel_version="0.1.0", fetch=fetcher, now=now + timedelta(hours=2)
    )

    assert len(fetcher.calls) == calls_after_first


def test_check_cache_still_reapplies_seen_filter(fetcher):
    # The cache isn't refreshed, but "already seen" must be recomputed every
    # time - otherwise a banner just dismissed would pop back up from the
    # stale cache.
    fetcher.add_json(announcements.FEED_URL, _feed(_item(id="a")))
    settings = Settings(show_announcements=True)
    now = datetime(2026, 8, 2, tzinfo=timezone.utc)

    first = announcements.check(settings=settings, panel_version="0.1.0", fetch=fetcher, now=now)
    assert [a.id for a in first] == ["a"]

    settings.seen_announcements.append("a")
    second = announcements.check(
        settings=settings, panel_version="0.1.0", fetch=fetcher, now=now + timedelta(hours=1)
    )
    assert second == []


def test_check_force_bypasses_cache(fetcher):
    fetcher.add_json(announcements.FEED_URL, _feed(_item(id="a")))
    settings = Settings(show_announcements=True)
    now = datetime(2026, 8, 2, tzinfo=timezone.utc)

    announcements.check(settings=settings, panel_version="0.1.0", fetch=fetcher, now=now)
    calls_after_first = len(fetcher.calls)

    announcements.check(
        settings=settings, panel_version="0.1.0", fetch=fetcher, now=now + timedelta(minutes=5), force=True
    )

    assert len(fetcher.calls) > calls_after_first


def test_check_refreshes_after_a_day(fetcher):
    fetcher.add_json(announcements.FEED_URL, _feed(_item(id="a")))
    settings = Settings(show_announcements=True)
    now = datetime(2026, 8, 2, tzinfo=timezone.utc)

    announcements.check(settings=settings, panel_version="0.1.0", fetch=fetcher, now=now)
    calls_after_first = len(fetcher.calls)

    announcements.check(
        settings=settings,
        panel_version="0.1.0",
        fetch=fetcher,
        now=now + timedelta(days=1, minutes=1),
    )

    assert len(fetcher.calls) > calls_after_first

"""The feed address.

Kept as a separate file because this isn't about parsing announcements, but
about where the panel reaches out to at all — and that only came to light
through a live request: on a private repository, `raw.githubusercontent.com`
returns 404 to an anonymous client, meaning the feed silently doesn't work.
"""

from __future__ import annotations

from houdini_agent_panel import announcements


def test_feed_url_defaults_to_the_repository(monkeypatch):
    monkeypatch.delenv(announcements.FEED_URL_ENV, raising=False)
    assert announcements.feed_url() == announcements.DEFAULT_FEED_URL


def test_feed_url_can_be_overridden(monkeypatch):
    """A studio behind an intercepting proxy, a mirror inside the perimeter,
    or the developer themself before the repository goes public — all of
    them need to be able to point at their own address without rebuilding
    the package."""
    monkeypatch.setenv(announcements.FEED_URL_ENV, "https://studio.example/feed.json")
    assert announcements.feed_url() == "https://studio.example/feed.json"


def test_check_uses_the_override(monkeypatch, fetcher):
    from houdini_agent_panel.settings import Settings

    monkeypatch.setenv(announcements.FEED_URL_ENV, "https://studio.example/feed.json")
    fetcher.add_json("https://studio.example/feed.json", {"version": 1, "announcements": []})

    announcements.check(settings=Settings(), panel_version="0.1.0", force=True, fetch=fetcher)

    assert fetcher.calls == ["https://studio.example/feed.json"]

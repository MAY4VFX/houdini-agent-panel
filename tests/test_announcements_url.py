"""Адрес фида.

Отдельным файлом, потому что это не про разбор оповещений, а про то, куда
панель вообще стучится — и это выяснилось только живым запросом: на
приватном репозитории `raw.githubusercontent.com` отдаёт анонимному
клиенту 404, то есть фид молча не работает.
"""

from __future__ import annotations

from houdini_agent_panel import announcements


def test_feed_url_defaults_to_the_repository(monkeypatch):
    monkeypatch.delenv(announcements.FEED_URL_ENV, raising=False)
    assert announcements.feed_url() == announcements.DEFAULT_FEED_URL


def test_feed_url_can_be_overridden(monkeypatch):
    """Студия за перехватывающим прокси, зеркало внутри периметра или сам
    разработчик до публикации репозитория — все они должны уметь указать
    свой адрес, не пересобирая пакет."""
    monkeypatch.setenv(announcements.FEED_URL_ENV, "https://studio.example/feed.json")
    assert announcements.feed_url() == "https://studio.example/feed.json"


def test_check_uses_the_override(monkeypatch, fetcher):
    from houdini_agent_panel.settings import Settings

    monkeypatch.setenv(announcements.FEED_URL_ENV, "https://studio.example/feed.json")
    fetcher.add_json("https://studio.example/feed.json", {"version": 1, "announcements": []})

    announcements.check(settings=Settings(), panel_version="0.1.0", force=True, fetch=fetcher)

    assert fetcher.calls == ["https://studio.example/feed.json"]

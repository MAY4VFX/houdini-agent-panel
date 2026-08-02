"""The announcements feed — a communication channel to the studio that bypasses package updates.

The source is a static JSON file at a fixed address in this same
repository (``feed/announcements.json``), but from the code's point of
view it's someone else's response from the internet: a human edits it by
hand, so it will have typos, missing fields, and values of the wrong type.
A broken ENTRY is skipped — the whole feed isn't lost because of it (see
``_parse_one``).

Severity (`severity`) decides the UI (a quiet banner vs a blocking popup
over the input field, see design.md), but that's not this module's
concern: this module only hands back the list of applicable announcements
— which widget draws them is ``ui/announcement.py``'s business.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Collection

from . import paths
from .network import Fetcher, fetch_json
from .settings import Settings
from .updates import compare_versions

#: The default feed address.
#:
#: This only works while the repository is public: `raw.githubusercontent.com`
#: returns 404 to anonymous requests on private repositories, and the panel
#: reaches it without a token and shouldn't have one. Verified by an actual
#: request — on a private repository this is exactly a 404, not an access
#: error, so there's nothing for the panel to diagnose either.
DEFAULT_FEED_URL = (
    "https://raw.githubusercontent.com/MAY4VFX/houdini-agent-panel/main/feed/announcements.json"
)

#: How a studio (or the developer themself, before the repository goes
#: public) overrides the feed address without rebuilding the package.
FEED_URL_ENV = "HAP_FEED_URL"


def feed_url() -> str:
    return os.environ.get(FEED_URL_ENV) or DEFAULT_FEED_URL


#: Kept for backward compatibility with code and tests that read the constant.
FEED_URL = DEFAULT_FEED_URL

_KNOWN_SEVERITIES = ("info", "blocking")
_CACHE_FILE_NAME = "announcements.json"
_MAX_AGE = timedelta(days=1)


@dataclass(frozen=True)
class Button:
    label: str
    url: str = ""


@dataclass(frozen=True)
class Announcement:
    id: str
    severity: str  # "info" | "blocking"
    title: str
    body: str = ""
    buttons: tuple[Button, ...] = ()
    panel_versions: str = ""  # a version specifier, "" — everyone
    expires: str = ""  # ISO 8601, "" — never expires


# --- parsing the feed -------------------------------------------------


def parse_feed(payload: Any) -> list[Announcement]:
    if not isinstance(payload, dict):
        return []
    raw_items = payload.get("announcements")
    if not isinstance(raw_items, list):
        return []
    result: list[Announcement] = []
    for raw in raw_items:
        parsed = _parse_one(raw)
        if parsed is not None:
            result.append(parsed)
    return result


def _parse_one(raw: Any) -> Announcement | None:
    if not isinstance(raw, dict):
        return None
    ann_id = raw.get("id")
    title = raw.get("title")
    # id and title are the only things without which there's nothing to
    # show and no point showing it (without an id there's nowhere to
    # record the fact "already seen").
    if not isinstance(ann_id, str) or not ann_id:
        return None
    if not isinstance(title, str) or not title:
        return None

    severity = raw.get("severity")
    # An unknown future severity (e.g. the studio invents "critical" in a
    # newer panel version, while an artist still has an old one) must NOT
    # block input BY DEFAULT — a quiet banner is safer than an erroneous
    # block.
    if severity not in _KNOWN_SEVERITIES:
        severity = "info"

    body = raw.get("body")
    body = body if isinstance(body, str) else ""

    buttons: list[Button] = []
    raw_buttons = raw.get("buttons")
    if isinstance(raw_buttons, list):
        for raw_button in raw_buttons:
            if not isinstance(raw_button, dict):
                continue
            label = raw_button.get("label")
            if not isinstance(label, str) or not label:
                continue
            url = raw_button.get("url")
            buttons.append(Button(label=label, url=url if isinstance(url, str) else ""))

    panel_versions = raw.get("panel_versions")
    panel_versions = panel_versions if isinstance(panel_versions, str) else ""

    expires = raw.get("expires")
    expires = expires if isinstance(expires, str) else ""

    return Announcement(
        id=ann_id,
        severity=severity,
        title=title,
        body=body,
        buttons=tuple(buttons),
        panel_versions=panel_versions,
        expires=expires,
    )


# --- targeting by panel version -----------------------------------------

_CLAUSE_RE = re.compile(r"^(>=|<=|==|!=|>|<)\s*(.+)$")


def _panel_version_matches(specifier: str, panel_version: str) -> bool:
    """A specifier like ``">=0.2,<0.4"``; comma-separated conditions must all match.

    An empty string means the announcement is for every version. Any
    unreadable part (an unfamiliar operator, a version that isn't PEP 440,
    our own version failing to parse) excludes the announcement rather than
    showing it to everyone: an error in someone else's feed's targeting
    must not SHOW something that was meant for a different panel version —
    the same "silence is better" principle as in ``updates.is_newer``.
    """
    if not specifier.strip():
        return True
    for clause in specifier.split(","):
        clause = clause.strip()
        if not clause:
            return False
        match = _CLAUSE_RE.match(clause)
        if not match:
            return False
        op, version = match.group(1), match.group(2).strip()
        cmp = compare_versions(panel_version, version)
        if cmp is None:
            return False
        if op == ">=" and cmp < 0:
            return False
        if op == "<=" and cmp > 0:
            return False
        if op == "==" and cmp != 0:
            return False
        if op == "!=" and cmp == 0:
            return False
        if op == ">" and cmp <= 0:
            return False
        if op == "<" and cmp >= 0:
            return False
    return True


def _parse_iso(text: str) -> datetime | None:
    if not text:
        return None
    try:
        # datetime.fromisoformat doesn't understand the "Z" suffix before
        # Python 3.11, and we have to work on 3.10 (the lowest supported
        # version, see CLAUDE.md).
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _is_expired(expires: str, now: datetime) -> bool:
    parsed = _parse_iso(expires)
    if parsed is None:
        # An empty "expires" means never-expiring on purpose; an unreadable
        # date also means never-expiring, but out of necessity: better to
        # show it for one extra day than to silently bury an important
        # message over a typo in a date.
        return False
    return parsed < now


def applicable(
    items: Collection[Announcement],
    *,
    panel_version: str,
    seen: Collection[str],
    now: datetime | None = None,
) -> list[Announcement]:
    """Announcements worth showing: not yet seen, not expired, targeted at this version."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    seen_ids = set(seen)
    result = []
    for ann in items:
        if ann.id in seen_ids:
            continue
        if _is_expired(ann.expires, now):
            continue
        if not _panel_version_matches(ann.panel_versions, panel_version):
            continue
        result.append(ann)
    return result


# --- network trip + once-a-day cache --------------------------------------


def _cache_path() -> Path:
    return paths.cache_dir() / _CACHE_FILE_NAME


def _feed_from_items(items: list[Announcement]) -> dict:
    """Back into the feed shape — so the cache can be read with the same
    ``parse_feed`` instead of having a separate (de)serializer for
    Announcement."""
    return {
        "version": 1,
        "announcements": [
            {
                "id": a.id,
                "severity": a.severity,
                "title": a.title,
                "body": a.body,
                "buttons": [{"label": b.label, "url": b.url} for b in a.buttons],
                "panel_versions": a.panel_versions,
                "expires": a.expires,
            }
            for a in items
        ],
    }


def _read_cache(now: datetime) -> list[Announcement] | None:
    path = _cache_path()
    try:
        payload = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    checked_at = _parse_iso(payload.get("checked_at", ""))
    if checked_at is None:
        return None
    if now - checked_at >= _MAX_AGE:
        return None
    return parse_feed(payload.get("feed"))


def _write_cache(now: datetime, items: list[Announcement]) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"checked_at": now.isoformat(), "feed": _feed_from_items(items)}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), "utf-8")
    os.replace(tmp, path)


def check(
    *,
    settings: Settings,
    panel_version: str,
    force: bool = False,
    fetch: Fetcher | None = None,
    now: datetime | None = None,
) -> list[Announcement]:
    """Announcements applicable right now. ``show_announcements=False`` returns ``[]``, no network.

    Just like ``updates.check`` — has its own once-a-day cache (the whole
    parsed feed, not the already-filtered list), because the
    ``seen``/``now`` filter must be recomputed on every call even when the
    feed itself hasn't been refreshed: otherwise a banner dismissed
    yesterday could resurface from the cache.
    """
    if not settings.show_announcements:
        return []

    now = now or datetime.now(timezone.utc)
    items: list[Announcement] | None = None
    if not force:
        items = _read_cache(now)
    if items is None:
        payload = fetch_json(feed_url(), fetch=fetch)
        items = parse_feed(payload)
        _write_cache(now, items)

    return applicable(items, panel_version=panel_version, seen=settings.seen_announcements, now=now)

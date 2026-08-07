"""One daily network trip that serves both updates and announcements.

``updates.check`` and ``announcements.check`` already keep their own
once-a-day cache and their own settings toggle each (see their docstrings)
— so calling both functions unconditionally on every panel open is safe:
if a toggle is off or the cache is still fresh, they won't reach the
network anyway. ``daily_refresh`` doesn't set up a third, separate timer on
top of these two — that would be a third moving part that would need to be
kept in sync with the first two. Instead it wraps the passed-in ``fetch``
with a counter and uses that to set ``checked``: whether even a single byte
actually went out on THIS call, rather than guessing from the age of
someone else's cache files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Sequence

from . import announcements as announcements_mod
from . import updates as updates_mod
from .announcements import Announcement
from .network import DEFAULT_TIMEOUT, Fetcher, NetworkError, urlopen_fetch
from .settings import Settings
from .updates import Update

if TYPE_CHECKING:
    from .registry import AgentEntry


@dataclass(frozen=True)
class RefreshResult:
    updates: list[Update] = field(default_factory=list)
    announcements: list[Announcement] = field(default_factory=list)
    checked: bool = False


def daily_refresh(
    *,
    settings: Settings,
    panel_version: str,
    force: bool = False,
    fetch: Fetcher | None = None,
    entries: Sequence[AgentEntry] = (),
    now: datetime | None = None,
    fresh_start: bool = True,
) -> RefreshResult:
    """Updates + announcements in a single pass. Never raises.

    With both toggles (``check_updates``, ``show_announcements``) off —
    zero network calls: the ``check()`` functions themselves decide this
    before ever touching ``fetch``, there's simply nothing to count here. A
    network error on either of the two steps doesn't propagate outward —
    the panel must open and work without internet access; it just ends up
    without that particular piece (the list of updates/announcements for
    the failed step will be empty for this call — there's no way to
    synthetically pull them from a past successful cache without lying
    about how current they are).

    ``fresh_start`` only reaches ``updates_mod.check`` — see its own
    docstring (``_FRESH_START_MAX_AGE``/``_SESSION_MAX_AGE``).
    ``announcements`` keeps its own, separate once-a-day cache untouched:
    the report this answers was specifically about the update banner going
    stale for hours during a day of frequent releases, not about the
    announcements feed, which has no comparable cadence to react to.
    """
    base_fetch = fetch or urlopen_fetch
    call_count = 0

    def counting_fetch(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> bytes:
        nonlocal call_count
        call_count += 1
        return base_fetch(url, timeout=timeout)

    found_updates: list[Update] = []
    try:
        # panel_version is passed in explicitly: since the caller already
        # knows the panel's current version (it must pass it for
        # announcement targeting below too), `updates.check` shouldn't have
        # to guess it again via importlib.metadata — that would be the same
        # fact computed twice through different paths, with a risk of them
        # disagreeing.
        found_updates = updates_mod.check(
            settings=settings,
            entries=entries,
            force=force,
            fetch=counting_fetch,
            now=now,
            panel_version=panel_version,
            fresh_start=fresh_start,
        )
    except NetworkError:
        pass

    found_announcements: list[Announcement] = []
    try:
        found_announcements = announcements_mod.check(
            settings=settings, panel_version=panel_version, force=force, fetch=counting_fetch, now=now
        )
    except NetworkError:
        pass

    return RefreshResult(updates=found_updates, announcements=found_announcements, checked=call_count > 0)

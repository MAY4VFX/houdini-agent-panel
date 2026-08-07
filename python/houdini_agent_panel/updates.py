"""Version comparison: registry agents, the panel, and fx against PyPI.

We use our own version parser instead of a dependency on ``packaging``,
because ``packaging`` is an extra wheel in the ``--target`` tree that gets
installed inside Houdini itself (see docs/architecture.md §0). The parsing
below covers what actually shows up in version numbers on PyPI and in the
ACP registry: numeric segments, pre-releases
(``a``/``b``/``rc``/alpha/beta), ``.postN``, ``.devN``. Exotic things like
epochs (``1!2.0``) or local versions (``+abc``) aren't needed by any of the
three packages compared here.

Comparing by segments (not sorted strings!) preserves the same priority
order as PEP 440: ``devN`` comes before any pre-release, a final release
comes after any pre-release, ``postN`` comes after the final release.
Garbage in a version string yields ``None``/``False``, not an exception: a
silent "update available" banner showing up every day because of an
unreadable version is worse than no banner at all.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from . import paths, runtime
from .network import Fetcher, NetworkError, fetch_json
from .settings import Settings

if TYPE_CHECKING:
    # Type hints only — we don't tie ourselves to registry.py at runtime
    # (duck typing on .id/.name/.version), so the module doesn't drag in
    # circular or premature imports.
    from .registry import AgentEntry

PYPI_URL = "https://pypi.org/pypi/{name}/json"

#: The panel's and fx's packages on PyPI — kind="panel"/"fx" are checked against these.
_PANEL_PACKAGE = "houdini-agent-panel"
_FX_PACKAGE = "fxhoudinimcp"

_CACHE_FILE_NAME = "updates.json"
#: How long a cached answer is trusted, and WHICH of the two depends on
#: why `check()` is being called at all — see `check`'s own `fresh_start`
#: parameter. A single one-day window (the original policy) was tuned for
#: a release cadence of maybe one a week; it stopped being right the day
#: this project started shipping several versions in an hour; the owner
#: restarted Houdini repeatedly on 0.6.1 while 0.7.0 and 0.7.1 were both
#: already on PyPI, and the banner said nothing every single time,
#: because the cache it read was written hours before either existed and
#: was still, by the old rule, "fresh." If release cadence ever slows
#: back down to roughly weekly, both numbers below are the first thing to
#: revisit — they exist for THIS project's current pace, not as a law.
#:
#: `_FRESH_START_MAX_AGE`: what a panel that JUST opened trusts. Minutes,
#: not hours — a process that has just started is exactly the moment a
#: stale answer costs the artist a whole session on an old build, and
#: nothing else is going to correct it until the NEXT restart.
_FRESH_START_MAX_AGE = timedelta(minutes=10)
#: `_SESSION_MAX_AGE`: what a periodic re-check, from a panel that has
#: already been open for a while, trusts. Long enough that a panel left
#: open all day does not poll PyPI every few minutes — short enough that
#: it still notices a same-day release without needing a restart at all.
_SESSION_MAX_AGE = timedelta(hours=2)


@dataclass(frozen=True)
class Update:
    kind: str  # "agent" | "panel" | "fx"
    target: str  # agent_id or the package name
    label: str  # what to show the human
    current: str
    latest: str


# --- version parsing and comparison ----------------------------------------

_VERSION_RE = re.compile(
    r"""
    ^\s*
    v?
    (?P<release>[0-9]+(?:\.[0-9]+)*)
    (?:[-_.]?(?P<pre_l>alpha|beta|preview|pre|a|b|c|rc)[-_.]?(?P<pre_n>[0-9]*))?
    (?:[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n>[0-9]*))?
    (?:[-_.]?(?P<dev_l>dev)[-_.]?(?P<dev_n>[0-9]*))?
    \s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)

#: PEP 440 treats "c" as a synonym for "rc"; "pre"/"preview" are the same family.
_PRE_RANK = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2, "pre": 2, "preview": 2}

_NEG_INF = float("-inf")
_POS_INF = float("inf")


def _parse_version(text: str) -> tuple | None:
    """``(release, pre, post, dev)``, or ``None`` for anything that doesn't parse."""
    if not isinstance(text, str) or not text.strip():
        return None
    match = _VERSION_RE.match(text)
    if not match:
        return None

    release = tuple(int(part) for part in match.group("release").split("."))
    # Trailing zeros aren't significant (1.2.0 == 1.2): trim them so
    # comparing tuples of different lengths doesn't confuse "shorter" with
    # "smaller".
    while len(release) > 1 and release[-1] == 0:
        release = release[:-1]

    pre = None
    if match.group("pre_l"):
        rank = _PRE_RANK[match.group("pre_l").lower()]
        num = match.group("pre_n")
        pre = (rank, int(num) if num else 0)

    post = None
    if match.group("post_l"):
        num = match.group("post_n")
        post = int(num) if num else 0

    dev = None
    if match.group("dev_l"):
        num = match.group("dev_n")
        dev = int(num) if num else 0

    return (release, pre, post, dev)


def _version_key(text: str):
    """A key for tuple comparison. ``None`` — the version didn't parse.

    Ordering within one release (per PEP 440):
    ``devN`` < any pre-release < final release < ``postN``.
    """
    parsed = _parse_version(text)
    if parsed is None:
        return None
    release, pre, post, dev = parsed

    if pre is None and post is None and dev is not None:
        pre_key: tuple = (_NEG_INF,)  # a pure dev release comes before every pre-release
    elif pre is None:
        pre_key = (_POS_INF,)  # a final release comes after any pre-release
    else:
        pre_key = pre

    post_key = (_NEG_INF,) if post is None else (post,)
    dev_key = (_POS_INF,) if dev is None else (dev,)
    return (release, pre_key, post_key, dev_key)


def compare_versions(a: str, b: str) -> int | None:
    """-1/0/1 following PEP 440 ordering; ``None`` — at least one version didn't parse.

    The single shared point of version comparison for the whole project:
    ``announcements.py`` uses this same function for targeting by
    ``panel_versions``, so it doesn't need a second version parser next to
    it.
    """
    key_a, key_b = _version_key(a), _version_key(b)
    if key_a is None or key_b is None:
        return None
    if key_a < key_b:
        return -1
    if key_a > key_b:
        return 1
    return 0


def is_newer(latest: str, current: str) -> bool:
    """Is ``latest`` strictly newer than ``current``? Garbage in either string yields ``False``."""
    cmp = compare_versions(latest, current)
    return cmp is not None and cmp > 0


# --- PyPI ---------------------------------------------------------------


def pypi_latest(name: str, *, fetch: Fetcher | None = None) -> str | None:
    """The latest version of a package on PyPI. ``None`` — the response wasn't in the expected shape.

    Network errors aren't swallowed here: that's the caller's call
    (``check`` below swallows them one at a time, so one package being
    unavailable on PyPI doesn't hide the result for another).
    """
    payload = fetch_json(PYPI_URL.format(name=name), fetch=fetch)
    if not isinstance(payload, dict):
        return None
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    version = info.get("version")
    return str(version) if version else None


def _current_panel_version() -> str | None:
    """What this process is running — see `install._panel_version` for why
    this asks the module and not `importlib.metadata`. Here the stale answer
    showed up as a banner offering an update that had already been applied,
    every day, because the number it compared against PyPI belonged to a
    `dist-info` directory four releases old."""
    try:
        from . import __version__

        return __version__
    except Exception:  # noqa: BLE001 - a version number is never worth an exception
        return None


def _current_fx_version() -> str | None:
    try:
        from importlib.metadata import version

        return version(_FX_PACKAGE)
    except Exception:  # noqa: BLE001 - fxhoudinimcp is unavailable outside the Houdini plugin
        return None


# --- checking --------------------------------------------------------------


def _cache_path() -> Path:
    return paths.cache_dir() / _CACHE_FILE_NAME


def _read_cache(now: datetime, *, fresh_start: bool) -> list[Update] | None:
    path = _cache_path()
    try:
        payload = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    checked_at_raw = payload.get("checked_at")
    if not isinstance(checked_at_raw, str):
        return None
    try:
        checked_at = datetime.fromisoformat(checked_at_raw)
    except ValueError:
        return None
    max_age = _FRESH_START_MAX_AGE if fresh_start else _SESSION_MAX_AGE
    if now - checked_at >= max_age:
        return None
    if payload.get("panel_version") != (_current_panel_version() or ""):
        # A different build wrote this. Its answers are about a version that
        # is no longer running, so they are not worth a day of trust.
        return None
    raw_updates = payload.get("updates")
    if not isinstance(raw_updates, list):
        return None
    updates: list[Update] = []
    for item in raw_updates:
        if not isinstance(item, dict):
            continue
        try:
            updates.append(
                Update(
                    kind=str(item["kind"]),
                    target=str(item["target"]),
                    label=str(item["label"]),
                    current=str(item["current"]),
                    latest=str(item["latest"]),
                )
            )
        except KeyError:
            continue
    return updates


def _write_cache(now: datetime, updates: list[Update]) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "checked_at": now.isoformat(),
        # Which panel produced this answer. A day-old cache written by an
        # older build keeps offering the update that build has since applied
        # — reported as 0.1.7 being told to upgrade to 0.1.5, with the button
        # leading nowhere. Recording the version lets the next start tell
        # "checked recently" from "checked by somebody else".
        "panel_version": _current_panel_version() or "",
        "updates": [asdict(u) for u in updates],
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), "utf-8")
    os.replace(tmp, path)


def check(
    *,
    settings: Settings,
    entries: Sequence[AgentEntry] = (),
    force: bool = False,
    fetch: Fetcher | None = None,
    now: datetime | None = None,
    panel_version: str | None = None,
    fx_version: str | None = None,
    fresh_start: bool = True,
) -> list[Update]:
    """The list of available updates: agents (from ``entries``), the panel, fx.

    ``settings.check_updates=False`` returns ``[]`` and makes zero network
    calls, verified by a test via the ``FakeFetcher.calls`` counter.

    The cache at ``<cache>/updates.json`` covers the whole check as one unit
    (agents end up in it too, even though they need no network) — how long
    it's trusted depends on ``fresh_start`` (``_FRESH_START_MAX_AGE`` vs.
    ``_SESSION_MAX_AGE``, see their own comments), not one fixed window for
    every caller. ``force`` bypasses the cache entirely regardless.

    ``fresh_start=True`` (the default) is a panel that just opened — a new
    tab, a Houdini that just started. ``False`` is a periodic re-check from
    a panel that has already been running for a while
    (``ui/panel.py``'s own recurring timer, reusing this same cache file
    and the same ``_RefreshWorker`` — deliberately not a second, separate
    polling mechanism).

    ``panel_version``/``fx_version`` override the auto-detected current
    version (tests need to control it without real package metadata in the
    environment); by default they're taken from ``importlib.metadata``.
    """
    if not settings.check_updates:
        return []

    now = now or datetime.now(timezone.utc)
    if not force:
        cached = _read_cache(now, fresh_start=fresh_start)
        if cached is not None:
            return cached

    updates: list[Update] = []

    for entry in entries:
        # The manifest (`runtime.installed_version`), not `settings.
        # installed_agents`: those two can disagree — an npx agent launches
        # fine on nothing but npx's own on-demand fetch, and used to leave
        # no manifest behind at all despite `settings` remembering it —
        # and whichever one this reads becomes what the artist sees, while
        # the Settings screen's own agent rows already read the manifest
        # (`ui/agents.py::_installed_record`). Reading a different source
        # here is exactly how the header, the Settings row, and this banner
        # ended up able to disagree about the same agent.
        current_version = runtime.installed_version(entry.id)
        if current_version is None:
            continue
        if is_newer(entry.version, current_version):
            updates.append(
                Update(
                    kind="agent",
                    target=entry.id,
                    label=f"{entry.name} {entry.version}",
                    current=current_version,
                    latest=entry.version,
                )
            )

    for kind, package, current in (
        ("panel", _PANEL_PACKAGE, panel_version if panel_version is not None else _current_panel_version()),
        ("fx", _FX_PACKAGE, fx_version if fx_version is not None else _current_fx_version()),
    ):
        if not current:
            continue
        try:
            latest = pypi_latest(package, fetch=fetch)
        except NetworkError:
            # One unavailable PyPI package shouldn't hide the result for
            # another (agents are already computed, the second package is
            # further down the loop).
            continue
        if latest and is_newer(latest, current):
            updates.append(
                Update(kind=kind, target=package, label=f"{package} {latest}", current=current, latest=latest)
            )

    _write_cache(now, updates)
    return updates

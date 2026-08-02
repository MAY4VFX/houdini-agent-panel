"""Telemetry — off by default, only versions and the fact of a crash.

Two independent gates must both be open before a single byte goes
anywhere: the toggle in settings (the human opted in) and the
``HAP_TELEMETRY_URL`` environment variable (the studio/distribution set an
endpoint). Without the second one, we're not going to ask "where do I send
this" — we silently stay a no-op: a flipped toggle with no configured
address must not try to knock on nothing.

``build_payload`` is the single place that decides what's even allowed into
an event. The list of allowed keys is hardcoded (an allowlist, not a
blocklist): that way new calling code with a new ``**extra`` can't
accidentally drag something extra along — an unfamiliar key is simply
dropped, not serialized on the hope that it's fine. That's what makes the
promise "telemetry never sees the scene" from docs/privacy.md a checkable
test rather than just our word for it.
"""

from __future__ import annotations

import os
import urllib.parse
from typing import Any

from .network import Fetcher, NetworkError, urlopen_fetch
from .settings import Settings

#: Address of the receiver. Not set — telemetry can't be enabled at all.
TELEMETRY_URL_ENV = "HAP_TELEMETRY_URL"

#: The only keys build_payload is willing to write into an event.
#: Everything else from **extra is silently dropped — see the module docstring.
_ALLOWED_EXTRA_KEYS = ("agent_version", "exception_type")

_SEND_TIMEOUT = 5.0


def is_enabled(settings: Settings) -> bool:
    """Both the toggle in settings and a configured endpoint — both conditions at once."""
    return bool(settings.telemetry) and bool(os.environ.get(TELEMETRY_URL_ENV))


def build_payload(settings: Settings, *, event: str, **extra: Any) -> dict[str, Any]:
    """Build an event's body. Never raises.

    Versions are the ones actually available in this process; an
    unavailable version (fx not installed, Houdini not responding) is
    simply absent from the payload, not replaced with a placeholder value.
    """
    payload: dict[str, Any] = {"event": str(event), "os": _os_name()}

    panel_version = _panel_version()
    if panel_version:
        payload["panel_version"] = panel_version

    fx_version = _fx_version()
    if fx_version:
        payload["fx_version"] = fx_version

    houdini_version = _houdini_version()
    if houdini_version and houdini_version != "unknown":
        payload["houdini_version"] = houdini_version

    for key in _ALLOWED_EXTRA_KEYS:
        value = extra.get(key)
        if value:
            payload[key] = str(value)

    return payload


def send(event: str, *, settings: Settings, fetch: Fetcher | None = None, **extra: Any) -> None:
    """Send an event if telemetry is enabled. Never interferes with the rest of the app.

    Disabled or no endpoint set — zero network calls. Sending is a plain
    GET with the payload in the query string, over the shared ``Fetcher``
    (see network.py): the panel has no reason to build a separate POST
    transport for an event that's a couple dozen bytes, and every network
    call the panel makes must go through ``Fetcher`` (otherwise the test
    safeguard ``no_real_network`` wouldn't work). Any network error is
    swallowed — an artist shouldn't notice that telemetry even tried to
    reach anywhere.
    """
    if not is_enabled(settings):
        return
    url = os.environ.get(TELEMETRY_URL_ENV)
    if not url:
        return

    payload = build_payload(settings, event=event, **extra)
    query = urllib.parse.urlencode(payload)
    separator = "&" if "?" in url else "?"
    full_url = f"{url}{separator}{query}"

    try:
        (fetch or urlopen_fetch)(full_url, timeout=_SEND_TIMEOUT)
    except NetworkError:
        pass


# --- value sources, each one must never raise -----------------------


def _os_name() -> str:
    import platform

    try:
        return platform.platform()
    except Exception:  # noqa: BLE001 - telemetry is not allowed to bring down the panel
        return "unknown"


def _panel_version() -> str:
    try:
        from importlib.metadata import version

        return version("houdini-agent-panel")
    except Exception:  # noqa: BLE001 - metadata may be missing in a --target tree
        try:
            from . import __version__

            return __version__
        except Exception:  # noqa: BLE001
            return ""


def _fx_version() -> str:
    try:
        from importlib.metadata import version

        return version("fxhoudinimcp")
    except Exception:  # noqa: BLE001
        return ""


def _houdini_version() -> str:
    try:
        from . import scene

        return scene.houdini_version()
    except Exception:  # noqa: BLE001 - scene.py touches hou, unavailable outside Houdini
        return "unknown"

"""Binding the panel to its own Houdini scene.

The panel lives inside the Houdini process, so it doesn't have to guess its
own fx server's port by scanning — `fxhoudinimcp_server.startup` in that
same process knows it exactly (see docs/architecture.md §4). The HTTP scan
over 8100..8115 is only a fallback for when the fx plugin isn't loaded or is
out of date; it finds SOMEONE ELSE's Houdini (the first live one in the
range), so it's used as a degradation with an explicit log entry, not
silently.

`hou` and `fxhoudinimcp_server` are imported lazily inside functions: the
module must be importable in tests outside Houdini.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

FX_SERVER_NAME = "fxhoudini"

#: The range and timeout match what fxhoudinimcp itself uses for its
#: auto-scan (see docs/facts/fxhoudinimcp.md §3): base 8100, 16 ports,
#: 1 second per port.
_PORT_SCAN_BASE = 8100
_PORT_SCAN_COUNT = 16
_PORT_SCAN_TIMEOUT = 1.0

_log = logging.getLogger(__name__)

#: Remembered answer of the HTTP scan (`(scanned?, port)`). The scan costs up
#: to `_PORT_SCAN_COUNT * _PORT_SCAN_TIMEOUT` = 16 seconds, and `fx_port()` is
#: called from the MAIN thread on every "new conversation". Sixteen seconds of
#: frozen Houdini per click is indistinguishable from a button that does
#: nothing. Caching is safe because the scan is only reached when
#: `fxhoudinimcp_server` can't be imported at all, and an import that failed
#: once in a process will keep failing.
_scanned_port: tuple[bool, int | None] = (False, None)


def reset_port_cache_for_tests() -> None:
    global _scanned_port
    _scanned_port = (False, None)


def fx_port() -> int | None:
    """The fx server's port in THIS Houdini process. None — the server isn't up."""
    try:
        import fxhoudinimcp_server.startup as startup  # noqa: PLC0415 - see the module docstring
    except ImportError:
        return _cached_scan_for_any_fx_port()

    if not startup.is_running():
        return None
    return startup.get_port()


def _cached_scan_for_any_fx_port() -> int | None:
    global _scanned_port
    scanned, port = _scanned_port
    if scanned:
        return port
    port = _scan_for_any_fx_port()
    _scanned_port = (True, port)
    return port


def fx_host() -> str:
    return "127.0.0.1"


def fx_python() -> str:
    """The interpreter fxhoudinimcp is installed in.

    Inside Houdini, `sys.executable` is Houdini's own binary, not Python:
    the MCP server can't be launched with that interpreter. `HAP_PYTHON` is
    the path the panel's installer records specifically for this purpose
    (see docs/architecture.md §0).
    """
    return os.environ.get("HAP_PYTHON") or sys.executable


def mcp_servers() -> list[dict]:
    """Exactly what goes into session/new as mcpServers.

    Pinning the port is mandatory: without it the MCP server scans the
    range and might connect to someone else's open Houdini. `env` is a list
    of {name, value} (`McpServerStdio.env: list[EnvVariable]`), not a dict.
    """
    env = [{"name": "HOUDINI_HOST", "value": fx_host()}]
    port = fx_port()
    if port is not None:
        env.append({"name": "HOUDINI_PORT", "value": str(port)})
    else:
        # The server hasn't come up in this process yet — nothing to pin.
        # Without a pin the agent will scan the range itself; it's the same
        # "someone else's Houdini" risk, but the degradation is unavoidable
        # here since there simply is no real port.
        _log.warning(
            "the fx server isn't up in this Houdini process — mcpServers "
            "will go out without HOUDINI_PORT, the agent will scan the "
            "range itself"
        )
    return [
        {
            "name": FX_SERVER_NAME,
            "command": fx_python(),
            "args": ["-m", "fxhoudinimcp"],
            "env": env,
        }
    ]


def hip_dir() -> str:
    """$HIP. From the main thread ONLY.

    An unsaved scene resolves to $HOME, not a nonexistent untitled path:
    the cwd in session/new must exist.
    """
    import hou  # noqa: PLC0415 - lazy, this module only exists inside Houdini

    if hou.hipFile.isNewFile():
        return str(Path.home())

    directory = Path(hou.hipFile.path()).parent
    if not directory.is_dir():
        return str(Path.home())
    return str(directory)


def houdini_version() -> str:
    """This process's Houdini version.

    `HOUDINI_VERSION` is the same environment variable Houdini exports
    itself and that the fx server's `mcp.health` returns (see
    docs/facts/fxhoudinimcp.md §8) — no need to go through `hou` for the
    same thing.
    """
    version = os.environ.get("HOUDINI_VERSION")
    if version:
        return version
    try:
        import hou  # noqa: PLC0415

        return ".".join(str(part) for part in hou.applicationVersion())
    except Exception:  # noqa: BLE001 - this version is only for diagnostics, must not raise
        return "unknown"


def is_fx_available() -> bool:
    return fx_port() is not None


def _scan_for_any_fx_port() -> int | None:
    """Fallback path: an HTTP scan of `mcp.health` over 8100..8115.

    Logged as a degradation — by construction, this path can't tell "our"
    Houdini apart from a neighboring one running on the same machine.

    Probed concurrently, not one port after another: a closed port still
    costs the full `_PORT_SCAN_TIMEOUT` before it fails, and this call
    happens on the main thread (`logbook._log_environment` at panel
    startup, before the result is cached by `_cached_scan_for_any_fx_port`).
    Sequentially that's up to `_PORT_SCAN_COUNT * _PORT_SCAN_TIMEOUT` = 16
    seconds of a frozen Houdini; concurrently it's bounded by one timeout,
    ~1 second, regardless of how many ports are dead. Ties (more than one
    port answering) resolve to the lowest port, matching the old
    lowest-first sequential scan.
    """
    _log.warning(
        "fxhoudinimcp_server is unreachable from inside the process (the "
        "plugin isn't loaded or is out of date) — scanning %s..%s over "
        "HTTP; this may find SOMEONE ELSE's Houdini instead of this one",
        _PORT_SCAN_BASE,
        _PORT_SCAN_BASE + _PORT_SCAN_COUNT - 1,
    )
    ports = range(_PORT_SCAN_BASE, _PORT_SCAN_BASE + _PORT_SCAN_COUNT)
    with concurrent.futures.ThreadPoolExecutor(max_workers=_PORT_SCAN_COUNT) as pool:
        alive = [port for port, ok in zip(ports, pool.map(_probe_health, ports)) if ok]
    return min(alive) if alive else None


def _probe_health(port: int) -> bool:
    """A single `mcp.health` request to `http://127.0.0.1:<port>/api`.

    The request shape is form-urlencoded `json=["mcp.health", [], {}]`, as
    `hwebserver` expects (docs/facts/fxhoudinimcp.md §3-4). Any error
    (port closed, timeout, non-JSON response) just means "wrong port".
    """
    body = urllib.parse.urlencode({"json": json.dumps(["mcp.health", [], {}])}).encode("ascii")
    request = urllib.request.Request(f"http://{fx_host()}:{port}/api", data=body)
    try:
        with urllib.request.urlopen(request, timeout=_PORT_SCAN_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("status") == "ok"

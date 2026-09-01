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
import re
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

FX_SERVER_NAME = "fxhoudini"

#: The range and timeout match what fxhoudinimcp itself uses for its
#: auto-scan (see docs/facts/fxhoudinimcp.md §3): base 8100, 16 ports,
#: 1 second per port.
_PORT_SCAN_BASE = 8100
_PORT_SCAN_COUNT = 16
_PORT_SCAN_TIMEOUT = 1.0

_log = logging.getLogger(__name__)

#: Remembered POSITIVE answer of the HTTP scan — `None` means "not cached",
#: never "cached as absent". The scan costs up to `_PORT_SCAN_COUNT *
#: _PORT_SCAN_TIMEOUT` = 16 seconds sequentially, ~1 second in practice since
#: `_scan_for_any_fx_port` probes concurrently, and `fx_port()` runs on the
#: MAIN thread on every "new conversation" — so a positive answer is worth
#: keeping, to not re-pay that cost on every call once the port is known.
#:
#: A NEGATIVE answer must NOT be cached, on pain of exactly the incident this
#: fixed: fxhoudinimcp's own auto-start is asynchronous (its `uiready.py`
#: polls readiness on a worker thread for up to ~15s — see
#: docs/facts/fxhoudinimcp.md §6), so the panel's first scan, which can run
#: within milliseconds of Houdini's own boot (`autostart_agent` fires a
#: session on the very first Qt event-loop tick), routinely lands before that
#: poll finishes. The old code cached that miss as a permanent verdict for
#: the rest of the process — every later call, including the one that builds
#: `mcpServers` for a brand new conversation started minutes later with the
#: fx server fully up (confirmed via `lsof` on the reported machine), kept
#: returning the same stale "no port". A positive result, by contrast, stays
#: true: `_pick_free_port` in fxhoudinimcp's `startup.py` returns the SAME
#: port to the same pid on every subsequent `start()`, so nothing in a normal
#: Houdini session reassigns it once found.
_scanned_port: int | None = None


def reset_port_cache_for_tests() -> None:
    global _scanned_port
    _scanned_port = None


def fx_port() -> int | None:
    """The fx server's port in THIS Houdini process. None — the server isn't up."""
    try:
        import fxhoudinimcp_server.startup as startup  # noqa: PLC0415 - see the module docstring
    except ImportError:
        _log.info(
            "fx_port: fxhoudinimcp_server is not importable in this process "
            "(plugin not loaded, out of date, or not on sys.path yet) — "
            "falling back to an HTTP scan"
        )
        return _cached_scan_for_any_fx_port()

    if not startup.is_running():
        # Not cached, unlike the scan branch below: this asks the plugin's
        # own live state on every call, so it self-corrects the moment
        # `uiready.py`'s async readiness poll (fxhoudinimcp's own, not
        # ours) confirms the server — no cache of ours could go stale here
        # because nothing here is remembered between calls.
        _log.info(
            "fx_port: fxhoudinimcp_server is loaded but hasn't confirmed "
            "its server is ready yet (auto-start polls asynchronously on "
            "its own worker thread) — no port for this call"
        )
        return None
    port = startup.get_port()
    _log.debug("fx_port: fxhoudinimcp_server reports port %s", port)
    return port


def fx_pending() -> bool:
    """True when `fxhoudinimcp_server`'s own auto-start readiness poll is
    IN FLIGHT in this process right now — worth a bounded wait, because a
    port answer is likely seconds away. False for every other case,
    including "no plugin at all": there is nothing there to wait ON.

    `startup.is_starting()` is the precise signal for this — set the
    instant `uiready.py` hands its readiness poll to a worker thread, and
    cleared the moment that poll settles either way (up to `startup.
    _READINESS_TIMEOUT` = 15s, measured in the installed package). This is
    deliberately NOT `not fx_port()`: that's also true once the poll has
    already given up, or when autostart never ran at all (autostart_agent
    off, or the plugin isn't loaded) — none of those will ever produce a
    port no matter how long anyone waits, so treating them as "pending"
    would just be a fixed delay for no reason.
    """
    try:
        import fxhoudinimcp_server.startup as startup  # noqa: PLC0415 - see the module docstring
    except ImportError:
        return False
    return startup.is_starting()


def _is_bindable(port: int) -> bool:
    """True when a plain TCP bind on `port` succeeds right now.

    This is precisely the check `_pick_free_port` in fxhoudinimcp's
    `startup.py` does NOT do. It calls a port free when nothing ANSWERS
    `mcp.health` there — which a wedged Houdini still holding the socket
    also satisfies. The search then hands that occupied port back,
    `hwebserver.run()` fails at bind, and the session is left with no MCP
    at all; the function's own docstring names the limitation ("nothing
    answers mcp.health is not the same as nothing holds the socket").
    Asking the kernel is the only answer that survives a process that
    holds the port but has stopped talking.

    Deliberately WITHOUT `SO_REUSEADDR`: the real question is whether
    hwebserver will get the port, and a plain bind errs on the safe side
    of that. It can call a port in TIME_WAIT unusable where hwebserver
    would have taken it, which costs one port out of sixteen and nothing
    else.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((fx_host(), port))
        except OSError:
            return False
    return True


def _first_bindable_port() -> int | None:
    """The lowest port in the scan range this process could actually serve on."""
    for port in range(_PORT_SCAN_BASE, _PORT_SCAN_BASE + _PORT_SCAN_COUNT):
        if _is_bindable(port):
            return port
    return None


def ensure_fx_started() -> None:
    """Start the fx server in THIS process when its own auto-start couldn't.

    Not a reimplementation of the plugin: it calls the plugin's own public
    `startup.start(port=...)`. The only thing added here is the argument —
    a port verified bindable by `_is_bindable`, rather than one merely
    found silent.

    Why this is needed at all. fxhoudinimcp auto-starts at Houdini launch
    and picks its port with `_pick_free_port`, which treats "nothing
    answers `mcp.health`" as "free". A Houdini that has wedged — not
    exited, still holding 8100, no longer answering anything — passes that
    test, so the new session binds nothing and stays MCP-less for its whole
    life. Restarting Houdini does not help while the wedged process is
    still up, which is exactly how it presents: "I restarted and it is
    still broken."

    Safe to call on every "+" and cheap when there is nothing to do: it
    returns on the first `is_running()`/`is_starting()` check, and only
    the down-and-not-starting case probes ports at all. Re-entry after a
    failed auto-start is sound — `_confirm_ready` sets `_server_started =
    False` and `_confirm_ready_async` clears `_starting` in a `finally`,
    deliberately, "or a failed start would leave the server permanently
    un-startable from the menu".

    Must run on the main thread: hwebserver keeps its `Server` in a
    `threading.local()`, so handlers registered on one thread are
    invisible to `run()` on another. Every call site is a panel one, which
    is the main thread by construction.

    Silent by design — no note in the feed. The artist gets a working MCP;
    the port it landed on is a log line, not their problem.
    """
    try:
        import fxhoudinimcp_server.startup as startup  # noqa: PLC0415 - see the module docstring
    except ImportError:
        # No plugin in this process — nothing to start, and the HTTP scan
        # fallback in `fx_port` is already the answer for that case.
        return

    if startup.is_running() or startup.is_starting():
        return

    port = _first_bindable_port()
    if port is None:
        _log.warning(
            "ensure_fx_started: the fx server is down and every port in %s..%s is "
            "held by something else — leaving it to the HTTP scan fallback",
            _PORT_SCAN_BASE,
            _PORT_SCAN_BASE + _PORT_SCAN_COUNT - 1,
        )
        return

    if port != _PORT_SCAN_BASE:
        _log.warning(
            # Deliberately no guess at WHO. This used to say "a wedged
            # Houdini, most likely", and on the owner's own machine that
            # sent the reader hunting for a Houdini process that did not
            # exist: 8100 was held by `hserver`, SideFX's licence daemon,
            # which runs all the time and had simply taken the port while
            # no Houdini was up. A log line that names the wrong culprit is
            # worse than one that names none — the port number is the fact,
            # and `lsof -nP -iTCP:%s` is one command away for anyone who
            # needs the rest.
            "ensure_fx_started: port %s is held by another process that isn't "
            "answering mcp.health (another Houdini, or anything else that took the "
            "port — `lsof -nP -iTCP:%s` says which) — starting the fx server on %s "
            "instead",
            _PORT_SCAN_BASE,
            _PORT_SCAN_BASE,
            port,
        )
    else:
        _log.info(
            "ensure_fx_started: the fx server isn't up and its auto-start isn't "
            "running — starting it on port %s",
            port,
        )

    try:
        # wait=False for the same reason auto-start uses it: the readiness
        # poll runs up to `_READINESS_TIMEOUT` = 15s, and this is the main
        # thread. `is_starting()` goes True here, so the panel's existing
        # bounded fx wait picks the result up on its own.
        startup.start(port=port, wait=False)
    except Exception:  # noqa: BLE001 - must never take the "+" button down with it
        _log.exception(
            "ensure_fx_started: starting the fx server on port %s failed — the "
            "session will open without Houdini tools",
            port,
        )


def _cached_scan_for_any_fx_port() -> int | None:
    global _scanned_port
    if _scanned_port is not None:
        _log.debug("fx port scan: reusing cached port %s", _scanned_port)
        return _scanned_port
    port = _scan_for_any_fx_port()
    if port is not None:
        _scanned_port = port
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


def mcp_python_status() -> str | None:
    """None when a `HAP_PYTHON` the installer recorded still points at a
    real file, a message for the artist otherwise.

    Only the "recorded, then vanished" case is checked — a uv cache pruned,
    a venv recreated, a Houdini reinstalled to a new path. install.py now
    refuses to record an interpreter it already knows won't survive the
    install itself (see its ephemeral-python handling), so that's the one
    way a bad `HAP_PYTHON` can still reach a *running* panel: it was good
    the day it was written and stopped being good later. `HAP_PYTHON` being
    entirely absent is not flagged here — routine outside an installed
    panel (dev mode, tests, a hand-built package json for `$HSITE`), and
    `fx_python()`'s own fallback to `sys.executable` already covers that
    case without complaint.
    """
    recorded = os.environ.get("HAP_PYTHON")
    if not recorded or Path(recorded).is_file():
        return None
    return (
        f"The Houdini MCP server's interpreter is gone (HAP_PYTHON={recorded}) — "
        "Houdini tools will not be available this session. Run `python -m "
        "houdini_agent_panel install` again to fix it."
    )


#: How the fx server is started, instead of a plain `-m fxhoudinimcp`.
#:
#: `hython` installs `haio.HoudiniEventLoopPolicy` as asyncio's default, and
#: `haio.HoudiniEventLoop.get_task_factory` raises `NotImplementedError`.
#: anyio calls it while starting its task group, so `mcp` — and therefore
#: the fx server — dies during startup with an `ExceptionGroup` before it
#: ever reads a byte of the protocol. Measured on 22.0.368 (Python 3.13) and
#: 20.5.445 (3.11): both policies are haio, both crash, and both start
#: cleanly with the stock policy restored.
#:
#: That only matters when the interpreter is Houdini's own, which is not the
#: intended case (`HAP_PYTHON` is meant to be an ordinary Python) but is
#: exactly what the installer records when it is run through `hython` — the
#: documented way to update. Reported as Codex showing
#: `mcp__fxhoudini__startup ✗ failed`; Claude failed the same way and said
#: nothing about it.
#:
#: The check is made at runtime, in the child, because that is the only
#: place the answer is known — the panel cannot tell from a path what
#: asyncio policy an interpreter will install. Under an ordinary Python it
#: finds no haio and changes nothing.
FX_BOOTSTRAP = """
import sys, asyncio, runpy
try:
    policy = asyncio.get_event_loop_policy()
except Exception:
    policy = None
if policy is not None and type(policy).__module__.split(".")[0] == "haio":
    asyncio.set_event_loop_policy(asyncio.DefaultEventLoopPolicy())
sys.argv = ["fxhoudinimcp"]
runpy.run_module("fxhoudinimcp", run_name="__main__")
"""


def mcp_servers() -> list[dict]:
    """Exactly what goes into session/new as mcpServers.

    Pinning the port is mandatory: without it the MCP server scans the
    range and might connect to someone else's open Houdini. `env` is a list
    of {name, value} (`McpServerStdio.env: list[EnvVariable]`), not a dict.
    """
    problem = mcp_python_status()
    if problem:
        _log.error("mcp_servers: %s", problem)
    env = [{"name": "HOUDINI_HOST", "value": fx_host()}]
    port = fx_port()
    if port is not None:
        env.append({"name": "HOUDINI_PORT", "value": str(port)})
        _log.info("mcp_servers: pinning the fx server on port %s for this session", port)
    else:
        # The server hasn't come up in this process yet — nothing to pin.
        # Without a pin the agent will scan the range itself; it's the same
        # "someone else's Houdini" risk, but the degradation is unavoidable
        # here since there simply is no real port. This is also a one-shot
        # decision: fxhoudinimcp's own client does its unpinned scan once,
        # at THIS session's MCP server startup, and never retries — a
        # session created in this state stays toolless for its whole life,
        # even once the fx server comes up moments later.
        _log.warning(
            "mcp_servers: the fx server isn't up in this Houdini process yet — "
            "mcpServers will go out without HOUDINI_PORT, the agent will scan "
            "the range itself, once, at its own MCP server's startup"
        )
    # Houdini's plain CPython carries no packages of its own, so it is told
    # where the panel's tree is. Set by the installer only when it chose
    # such an interpreter — an ordinary Python that already has
    # `fxhoudinimcp` installed must not be handed a tree of extensions
    # compiled for a different Python version.
    mcp_path = os.environ.get("HAP_MCP_PATH")
    if mcp_path:
        env.append({"name": "PYTHONPATH", "value": mcp_path})
    else:
        # Explicit, not merely absent: an entry here OVERRIDES whatever
        # PYTHONPATH the agent process this server is spawned under
        # happens to carry — the agent inherits its own env into every
        # stdio MCP child it starts, on top of only what we list here.
        # `fx_python()` in this branch is assumed to already have its own
        # matching `fxhoudinimcp` (see `install.py::_mcp_python`), and
        # that assumption is exactly what a leaked PYTHONPATH from
        # elsewhere breaks — measured for real: Houdini's own package json
        # writes `PYTHONPATH=$HAP_DEPS` for the PANEL's benefit, and one
        # unrelated bug (`shellenv.py`, since fixed) was enough for that to
        # reach this exact process and point a completely different
        # Python's compiled extensions at it. An empty value neutralizes
        # PYTHONPATH the same way leaving it unset would (verified:
        # `PYTHONPATH=""` and no `PYTHONPATH` at all produce an identical
        # `sys.path`) — this is a floor under `HAP_MCP_PATH`'s own
        # contract, not a new behavior, so nothing here needs to survive
        # this exact leak recurring some other way to keep the fx server
        # working.
        env.append({"name": "PYTHONPATH", "value": ""})
    return [
        {
            "name": FX_SERVER_NAME,
            "command": fx_python(),
            "args": ["-c", FX_BOOTSTRAP],
            "env": env,
        }
    ]


def hip_dir() -> str:
    """$HIP. From the main thread ONLY.

    An unsaved scene resolves to $HOME, not a nonexistent untitled path:
    the cwd in session/new must exist.
    """
    directory = real_hip_dir()
    return directory if directory is not None else str(Path.home())


def real_hip_dir() -> str | None:
    """The directory of an actually-saved scene, or `None` when there isn't
    one. From the main thread ONLY, same as `hip_dir()`.

    `hip_dir()` always returns a path (it falls back to `$HOME` so
    `session/new`'s cwd is never a nonexistent one), which makes it the
    wrong function for anything that WRITES next to the scene: a fresh,
    never-saved file would send that write straight into the artist's home
    directory. This is the honest half of that answer — the real project
    folder, or nothing.
    """
    import hou  # noqa: PLC0415 - lazy, this module only exists inside Houdini

    if hou.hipFile.isNewFile():
        return None

    path = Path(hou.hipFile.path())
    directory = path.parent
    if _is_backup_copy(path, directory):
        directory = directory.parent
    if not directory.is_dir():
        return None
    return str(directory)


#: Houdini's own versioned copy of a scene: `<name>_bak<N>.hip` (or
#: `.hipnc`/`.hiplc`), written into the backup folder on every save.
_BACKUP_COPY_RE = re.compile(r".+_bak\d+\.hip(nc|lc)?$", re.IGNORECASE)


def _backup_dir_name() -> str:
    """What Houdini calls its backup folder — `$HOUDINI_BACKUP_DIR`, or
    `backup` when it isn't set. Read through `hou.getenv` so a studio that
    renamed it is covered too; anything unexpected falls back to the
    default rather than raising inside a path check."""
    import hou  # noqa: PLC0415 - lazy, same reason as `real_hip_dir`

    getenv = getattr(hou, "getenv", None)
    if getenv is None:
        return "backup"
    try:
        configured = getenv("HOUDINI_BACKUP_DIR")
    except Exception:  # noqa: BLE001 - a path check has no right to raise
        return "backup"
    return Path(configured).name if configured else "backup"


def _is_backup_copy(path: Path, directory: Path) -> bool:
    """Whether Houdini is telling us about its OWN backup copy rather than
    the scene the artist has open.

    Houdini fires its ordinary "saved" event for the backup write too, and
    `hou.hipFile.path()` points at the backup file while it does. Believed
    literally, that moved a live session — and the conversation on disk
    with it — into `$HIP/backup`, where the artist never looks, and dropped
    AGENTS.md/CLAUDE.md in there as well. Measured on 2026-08-31: two
    conversations relabelled at 19:45:57 and 20:41:52, the exact seconds
    Houdini wrote `airship_v010_bak2.hip` and `_bak3.hip`.

    BOTH signals have to agree — the `_bakN` name AND the backup folder —
    so a scene the artist happened to name `shot010_bak1.hip`, or a real
    project folder called `backup`, is still taken at face value.
    """
    return bool(_BACKUP_COPY_RE.match(path.name)) and directory.name == _backup_dir_name()


def watch_hip_dir_changes(callback: Callable[[str], None]) -> object:
    """Call `callback` whenever the scene underneath an already-open panel
    might have moved — File > Open, File > New, a Save/Save As. From the
    main thread ONLY, same as `hip_dir()` itself: Houdini fires
    `hipFile` events synchronously on the thread that did the File > Open,
    which is always the main one.

    Nothing before this ever re-read `$HIP` after boot. A panel opened
    against a fresh, unsaved scene starts scoped to `$HOME`
    (`hip_dir()`'s own fallback); if the artist then opens a real project
    file into that SAME Houdini session, the panel kept the old scope
    forever — its header, and worse, `_restore_conversations` (which reads
    `scene.hip_dir()` at call time but is only ever CALLED at boot), so
    conversations already on disk for the folder actually open never
    appeared. Measured for real: a live panel's pool held a "New chat"
    scoped to `$HOME` side by side with the real, correctly-scoped session
    for the project the artist was actually in — the second only existed
    because a NEW message reads `hip_dir()` fresh; nothing ever went back
    and re-scoped the restore step.

    `callback` now receives WHAT KIND of change this was — `"saved"`,
    `"loaded"`, or `"new"` — one of the three outcomes `_on_hip_event`
    turns Houdini's raw `hou.hipFileEventType` into. Measured on 22.0.368,
    not assumed from the enum's own names (`docs/facts/houdini.md`,
    "hipFile event types"): a conversation that just followed the scene to
    a new/first path via Save is a different fact from one left behind by
    opening or clearing a DIFFERENT scene, and `AgentPanel._on_hip_dir_
    changed` needs to tell them apart to avoid closing a conversation the
    artist only just saved.

    - `"saved"` — `AfterSave`. `hou.hipFile.path()` already reports the
      NEW path by the time this fires (even inside `BeforeSave`).
    - `"loaded"` — `AfterLoad`. Measured: `hou.hipFile.load()` ALSO fires
      a nested `BeforeClear`/`AfterClear` pair partway through — that
      inner `AfterClear`'s own path already shows the file being loaded
      (not "untitled") and `isNewFile()` is already `False` there. It is
      deliberately NOT reported on its own (see next point); `AfterLoad`
      right after it is the one signal that means the load actually
      finished, with the correct final state.
    - `"new"` — `AfterClear`, but ONLY when `hou.hipFile.isNewFile()` is
      still `True` at that moment: File > New / Clear, as opposed to the
      nested `AfterClear` a Load fires on its way to a real file (where
      `isNewFile()` reads `False`, the case excluded above).

    `BeforeSave`/`BeforeLoad`/`BeforeClear`/`AfterMerge`/`BeforeMerge`/
    `BeforeQuit` are not reported at all: nothing here needs a "before"
    signal yet, and a merge loads geometry into the CURRENT scene without
    changing `$HIP` (measured: the path stays exactly where it was
    throughout `hou.hipFile.merge()`).

    Returns the actual registered callback, which `unwatch_hip_dir_changes`
    needs back to remove the right one — `hou.hipFile.removeEventCallback`
    takes the exact callable that was added, and `callback` itself isn't
    it (it's wrapped, to swallow whatever this Houdini version passes an
    event handler and translate it into the three outcomes above).
    """
    import hou  # noqa: PLC0415

    def _on_hip_event(event_type=None, *_args, **_kwargs) -> None:
        if event_type == hou.hipFileEventType.AfterSave:
            callback("saved")
        elif event_type == hou.hipFileEventType.AfterLoad:
            callback("loaded")
        elif event_type == hou.hipFileEventType.AfterClear and hou.hipFile.isNewFile():
            callback("new")
        # Anything else (a Before* event, AfterMerge, the nested AfterClear
        # inside a Load) is deliberately not forwarded — see the docstring
        # above for why each one is excluded.

    hou.hipFile.addEventCallback(_on_hip_event)
    return _on_hip_event


def unwatch_hip_dir_changes(handle: object) -> None:
    """Undo `watch_hip_dir_changes`. Safe to call on a handle that is
    already gone (a second `shutdown()`, a tab that never finished
    booting) — a panel tearing down is not the place to raise over a
    Houdini API that has nothing left to remove."""
    import hou  # noqa: PLC0415

    try:
        hou.hipFile.removeEventCallback(handle)
    except Exception:  # noqa: BLE001
        pass


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
    port = min(alive) if alive else None
    if port is None:
        _log.warning(
            "fx port scan: nothing answered mcp.health in %s..%s",
            _PORT_SCAN_BASE,
            _PORT_SCAN_BASE + _PORT_SCAN_COUNT - 1,
        )
    else:
        _log.info("fx port scan: found a server on port %s", port)
    return port


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

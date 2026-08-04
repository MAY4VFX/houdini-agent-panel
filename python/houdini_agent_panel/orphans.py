"""Agent processes that outlive the Houdini session that started them.

`AcpClient.stop()` runs on Qt's `aboutToQuit` and Python's `atexit`
(`client.py`) — both graceful-shutdown hooks, and neither fires when the
host process is killed outright: SIGKILL, a hard crash, a Force Quit. Every
agent is a plain `subprocess.Popen` with no process-group isolation and
nothing macOS offers as an equivalent to Linux's `PR_SET_PDEATHSIG`, so a
Houdini that dies that way leaves its agent process (and everything IT
spawned) parented to PID 1, running until something notices — confirmed by
direct reproduction: spawn one, SIGKILL the host, watch the child's PPID
become 1 (may-hub task, 2026-08-04).

The fix here is deliberately NOT a live watchdog. A background poll loop
whose entire purpose is to catch a rare, already-abnormal exit is a bad
trade — constant cost for a case that isn't the common path. Instead: every
agent process this panel starts gets one line in a small JSON file the
moment it's spawned (`record_started`), and the line comes back out once
the agent stops cleanly (`record_stopped`). Nothing reads the file except
the NEXT boot's `sweep()` — so the cost of this whole mechanism, on every
run that doesn't follow a crash, is one small file write per launch and
one read per boot. The price of not running anything in between is that a
crash's leftover process waits for the next panel open to be noticed, not
a moment sooner — accepted deliberately, not an oversight.

Safety is the reason this exists as a file-and-sweep instead of "just kill
whatever's using a lot of memory": a PID is recycled by the OS, and this
must never kill a process it merely guesses is an old agent. `sweep()`
kills a record ONLY if the live process at that PID still has the exact
command/args we launched it with AND the exact working directory — command
alone is reused often enough (every agent this panel ships eventually runs
as plain `node ...`) that cwd is load-bearing, not a nice-to-have. Anything
that doesn't verify is left alone, process AND record both — a record that
turns out not to match is kept, not dropped, because silently forgetting
something suspicious is worse than carrying a stale line in a JSON file
(see `sweep`'s docstring for the exact three outcomes).
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import paths

_FILENAME = "running_agents.json"

#: How long the OS-level SIGTERM->SIGKILL ladder waits before escalating —
#: shorter than `client._terminate_process`'s own timeouts is fine here:
#: unlike that one, nothing in the current session is waiting on this
#: process for anything, so there's no reason to be generous.
_TERM_GRACE_S = 2.0


@dataclass
class RunningAgent:
    """One line in the on-disk record: an agent process this panel started
    and has not yet recorded as stopped."""

    agent_id: str
    pid: int
    command: str
    args: list[str] = field(default_factory=list)
    cwd: str = ""
    started_at: str = ""

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass(frozen=True)
class SweptAgent:
    """One record `sweep()` actually found alive, verified, and stopped —
    what a caller reports (`AgentPanel`, one line in the feed)."""

    agent_id: str
    pid: int


def _path() -> Path:
    return paths.data_dir() / _FILENAME


def _load() -> dict[str, RunningAgent]:
    """Never raises: a corrupt or missing file just means nothing is
    tracked yet, the same forgiving contract `settings.load` has."""
    target = _path()
    if not target.exists():
        return {}
    try:
        payload = json.loads(target.read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(payload, dict):
        return {}
    records: dict[str, RunningAgent] = {}
    for key, item in payload.items():
        if not isinstance(item, dict):
            continue
        try:
            records[str(key)] = RunningAgent(
                agent_id=str(item.get("agent_id", "")),
                pid=int(item.get("pid", 0)),
                command=str(item.get("command", "")),
                args=[str(a) for a in item.get("args", []) or []],
                cwd=str(item.get("cwd", "")),
                started_at=str(item.get("started_at", "")),
            )
        except (TypeError, ValueError):
            continue
    return records


def _save(records: dict[str, RunningAgent]) -> None:
    target = _path()
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: asdict(record) for key, record in records.items()}
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", "utf-8")
    os.replace(tmp, target)


# --- writers: called from client.py around the process's own lifetime ----


def record_started(*, agent_id: str, pid: int, command: str, args: list[str], cwd: str) -> None:
    """A process this panel just spawned — called right after `Popen`
    returns (`client.py::AcpWorker.do_start`), so a crash any time after
    this point is exactly what `sweep()` exists to notice later."""
    records = _load()
    records[str(pid)] = RunningAgent(
        agent_id=agent_id, pid=pid, command=command, args=list(args), cwd=cwd, started_at=RunningAgent.now()
    )
    _save(records)


def record_stopped(pid: int) -> None:
    """The process stopped on its own terms — nothing for `sweep()` to
    ever do here, so the record goes with it (`client.py::AcpWorker.
    _terminate_process`, every exit path)."""
    records = _load()
    if str(pid) in records:
        del records[str(pid)]
        _save(records)


# --- process inspection: read-only until `_terminate` -------------------


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return str(pid) in completed.stdout
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, owned by someone else — cannot be a process WE spawned
        # (same user), so `_matches` will say no regardless. Reporting it
        # as "alive" here just routes it through that check honestly
        # instead of silently dropping the record.
        return True
    except OSError:
        return False
    return True


def _command_line(pid: int) -> "tuple[str, str] | None":
    """`(full command line, cwd)` for a live `pid`, or `None` if either
    can't be determined. `None` must never be read as a match — see
    `_matches`, which is the only caller."""
    if os.name == "nt":
        return _command_line_windows(pid)
    return _command_line_posix(pid)


def _command_line_posix(pid: int) -> "tuple[str, str] | None":
    proc_dir = Path(f"/proc/{pid}")
    if proc_dir.exists():  # Linux: no extra process needed
        try:
            raw = (proc_dir / "cmdline").read_bytes()
            argv = [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]
            cwd = os.readlink(proc_dir / "cwd")
        except OSError:
            return None
        return " ".join(argv), cwd
    # macOS has no /proc — shell out, same as the investigation that found
    # this bug in the first place (`ps`/`lsof`, both standard on macOS).
    try:
        cmd = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5
        )
        cwd_info = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if cmd.returncode != 0 or not cmd.stdout.strip():
        return None
    cwd = ""
    for line in cwd_info.stdout.splitlines():
        if line.startswith("n"):
            cwd = line[1:]
            break
    return cmd.stdout.strip(), cwd


def _command_line_windows(pid: int) -> "tuple[str, str] | None":
    """Best-effort — written but not run on a real Windows machine (this
    investigation happened on macOS). `Get-CimInstance` exposes both the
    command line and the process's own idea of its start directory in one
    call, which `tasklist` does not."""
    try:
        completed = subprocess.run(
            [
                "powershell", "-NoProfile", "-Command",
                f"(Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\") | "
                "Select-Object -ExpandProperty CommandLine",
            ],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    command_line = completed.stdout.strip()
    if not command_line:
        return None
    # Windows has no per-process "cwd" the way POSIX does once the process
    # is running; nothing here can verify it independently of the command
    # line, so cwd matching is skipped on this platform — `_matches` still
    # requires the (stronger) command-line match, which is not weakened.
    return command_line, ""


def _matches(record: RunningAgent) -> bool:
    """Command AND working directory, not PID alone — a PID is recycled by
    the OS, and killing a coincidentally-reused one is the one mistake
    this module may never make. `None` from `_command_line` (couldn't
    determine) is a non-match, same as an actual mismatch — see the
    module docstring for why an unnecessary "leave it" is the acceptable
    failure mode here, never an unnecessary "kill it"."""
    observed = _command_line(record.pid)
    if observed is None:
        return False
    live_command_line, live_cwd = observed

    if os.name != "nt":
        if not live_cwd:
            return False
        if os.path.realpath(record.cwd) != os.path.realpath(live_cwd):
            return False

    if record.args:
        return all(arg and arg in live_command_line for arg in record.args)
    return bool(record.command) and record.command in live_command_line


def _terminate(pid: int) -> None:
    """SIGTERM, a short wait, SIGKILL — the same shape as `client.py`'s
    `_terminate_process`, but by PID: this module never holds a live
    `Popen` handle, since the process being cleaned up belongs to a
    session that no longer exists."""
    if os.name == "nt":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=5)
        return
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + _TERM_GRACE_S
    while time.monotonic() < deadline:
        if not _process_alive(pid):
            return
        time.sleep(0.1)
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


def sweep() -> list[SweptAgent]:
    """Clean up whatever a past, now-gone session left behind.

    Every record on disk when this runs belongs to a session that has
    already ended — a session never sees its OWN agents in this file; it
    only ever adds to it (`record_started`) and removes from it
    (`record_stopped`) as it goes. Three outcomes per record:

    - the pid isn't running any more: nothing to clean, the record is
      dropped (it already did its job — the crash it was insurance
      against didn't happen, or already got cleaned some other way).
    - the pid is running AND still verifiably the same command/cwd this
      panel launched it with: it's ours, orphaned by a session that never
      got to stop it — terminated, then the record is dropped.
    - the pid is running but does not verify (different command/cwd, or
      undeterminable): left alone, process AND record both — see the
      module docstring for why.

    Meant to run off the main thread (`ui/panel.py`'s `_OrphanSweepWorker`)
    — `_command_line`'s subprocess calls are cheap but not instant, and
    opening the panel must never wait on them.
    """
    records = _load()
    if not records:
        return []

    cleaned: list[SweptAgent] = []
    drop_keys: list[str] = []
    for key, record in records.items():
        if not _process_alive(record.pid):
            drop_keys.append(key)  # already gone — nothing to clean, nothing to keep
            continue
        if _matches(record):
            _terminate(record.pid)
            cleaned.append(SweptAgent(agent_id=record.agent_id, pid=record.pid))
            drop_keys.append(key)
        # else: alive, unverified — left in place, dropped from neither
        # list, so it survives untouched into the file this function writes.

    if drop_keys:
        # Re-read right before writing rather than reusing the snapshot
        # from above: checking every candidate (a subprocess call or two
        # each) takes long enough that THIS SAME boot's own new agent
        # launch could have written its own fresh entry in the meantime
        # (`client.py::do_start` -> `record_started`) — dropping keys from
        # a just-now-fresh read, not from the stale `records` above, is
        # what keeps that entry from being overwritten out of existence.
        fresh = _load()
        for key in drop_keys:
            fresh.pop(key, None)
        _save(fresh)
    return cleaned


__all__ = [
    "RunningAgent",
    "SweptAgent",
    "record_started",
    "record_stopped",
    "sweep",
]

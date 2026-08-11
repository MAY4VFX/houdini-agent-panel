"""Spawning child processes the way each platform actually needs them spawned.

Everything the panel launches — the agent, a terminal login, the self
updater, a version probe — is a plain `subprocess.Popen` on macOS and Linux
and nothing here would need to exist. Windows needs three things none of
those calls do by themselves, and they are collected here so no call site
has to remember them one at a time.

1. **No console window.** Houdini on Windows is a GUI process with no
   console of its own, so every console child (`node.exe`, `kimi.exe`,
   `tasklist`) gets a fresh console WINDOW allocated for it. For the agent
   that is a black window sitting on the artist's desktop for the whole
   session; for the probes in `orphans.py` it is a flicker on every panel
   open. `CREATE_NO_WINDOW` suppresses it, and output still comes back
   through the pipes exactly as before.

2. **Overlapped pipes for asyncio.** `client.py` hands the agent's pipes to
   `loop.connect_read_pipe`/`connect_write_pipe`. On Windows the loop is a
   `ProactorEventLoop`, which reads a pipe by associating its HANDLE with
   an I/O completion port — and that only works for a handle opened for
   overlapped I/O. `subprocess.PIPE` creates anonymous pipes, which are
   not, and whose `fileno()` is a C-runtime descriptor rather than a
   handle. The standard library hit this first and solved it in
   `asyncio.windows_utils.Popen` ("Replacement for subprocess.Popen using
   overlapped pipe handles" — it is what asyncio's own subprocess transport
   spawns with), so `spawn_with_asyncio_pipes` uses it rather than
   re-deriving named-pipe creation here.

3. **Killing the tree, not the parent.** `Popen.terminate()` on Windows is
   `TerminateProcess`: no signal, nothing forwarded to children. An npx
   agent is `node npx-cli.js …` with the real agent as a grandchild, so
   terminating the parent alone leaves the agent running with its pipes
   closed. `taskkill /T` is the platform's own answer, and the same one
   `orphans.py` already uses for the processes it sweeps.

The Windows paths here cannot be executed by this project's test suite (no
Windows machine, see `ui/self_update.py`'s own note about the same
limitation) — hence `_is_windows()` as a single seam the tests can flip,
rather than the platform being read directly at each call site.
"""

from __future__ import annotations

import contextlib
import subprocess
import sys

#: `subprocess.CREATE_NO_WINDOW` only exists on Windows; the literal is the
#: documented value of the flag, used so this module imports everywhere.
_CREATE_NO_WINDOW = 0x08000000

#: How long to wait for `taskkill /T` before giving up on it and falling
#: back to killing the one process we have a handle for.
_TASKKILL_TIMEOUT = 5.0


def _is_windows() -> bool:
    """One seam instead of `sys.platform` at five call sites — tests patch
    this to exercise the Windows branches from a machine that isn't one."""
    return sys.platform == "win32"


def hidden_window_kwargs() -> dict:
    """Extra `Popen`/`run` keyword arguments that keep a console window from
    appearing. Empty on macOS and Linux, where there is nothing to hide."""
    if not _is_windows():
        return {}
    return {"creationflags": _CREATE_NO_WINDOW}


def run(argv, **kwargs):
    """`subprocess.run` that never flashes a console window.

    A thin wrapper so a call site says what it means and cannot forget the
    Windows half; every other argument goes straight through.
    """
    return subprocess.run(argv, **{**hidden_window_kwargs(), **kwargs})


def spawn_with_asyncio_pipes(argv, *, env, cwd) -> subprocess.Popen:
    """Spawn `argv` with stdin/stdout/stderr pipes an asyncio loop can read.

    On POSIX that is exactly `subprocess.Popen` — the selector loop reads
    the descriptors directly. On Windows the pipes have to be overlapped
    for the proactor loop (see this module's docstring, point 2), so the
    stdlib's own `asyncio.windows_utils.Popen` does the spawning; it is a
    `subprocess.Popen` subclass, so everything the caller does afterwards
    (`pid`, `wait()`, `terminate()`) is unchanged.
    """
    common = {
        "stdin": subprocess.PIPE,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": env,
        "cwd": cwd,
    }
    if not _is_windows():
        return subprocess.Popen(argv, **common)

    from asyncio import windows_utils  # noqa: PLC0415 - importable on Windows only

    # `bufsize=0` is required by windows_utils.Popen (it asserts on it): the
    # pipes it hands back are PipeHandle objects, not file objects, so there
    # is nothing for Python-level buffering to wrap.
    return windows_utils.Popen(argv, bufsize=0, **common, **hidden_window_kwargs())


def terminate_tree(process: subprocess.Popen) -> None:
    """Stop `process` AND whatever it spawned, as far as the platform allows.

    POSIX keeps the existing behaviour — `terminate()` sends SIGTERM, and
    the launchers we use (npx's `foreground-child`, the agent binaries)
    pass it on. Windows has no such convention, so the tree is taken down
    by `taskkill /T`; if that is unavailable or fails, we still fall back
    to terminating the one process we hold a handle for, because leaving
    it running is worse than an incomplete cleanup.
    """
    if not _is_windows():
        process.terminate()
        return

    killed = False
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        completed = run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            timeout=_TASKKILL_TIMEOUT,
        )
        killed = completed.returncode == 0
    if not killed:
        with contextlib.suppress(OSError):
            process.terminate()


__all__ = ["hidden_window_kwargs", "run", "spawn_with_asyncio_pipes", "terminate_tree"]

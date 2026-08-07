"""Updates the panel itself (or `fxhoudinimcp`) from the notice strip's
"Update" button — in a SEPARATE process, never in this one.

`AgentPanel._start_update`'s own docstring explains why: this process is
running FROM the deps tree `pip install --target` would have to rewrite,
and a process is not allowed to safely replace itself in place on every
platform this panel ships to. Measured directly before this was built
(both real Houdini installs, both macOS and Linux, `hython` with
`pydantic_core` actually imported and a model actually validated): `pip
install --upgrade --target <tree>` against a tree the running process has
already loaded from succeeds, and the running process survives — POSIX
lets you unlink and rewrite a file a process still has open or mapped, and
the running interpreter keeps whatever it already imported. What does NOT
survive automatically is a module this process has not imported YET at the
moment the tree changes: a plain `from . import x` reached for the first
time after the rewrite loads the NEW `x`, into a process everywhere else
still running the OLD one. That is real, not theoretical — reproduced by
importing a fresh submodule after the swap and watching it come back from
the new file. `AgentPanel` is the one that decides what to say about it
(see `_on_panel_update_succeeded`); this module only runs the update.

Windows could not be measured at all (no Windows machine in this project).
An open `.pyd`/`.dll` there cannot be replaced — the write fails with a
sharing violation — so `_classify_failure` below treats anything that
looks like that as its own case rather than a generic failure: "close
Houdini and run it again" is the only honest thing to say to a platform
that could not be tested directly, and it must never be confused with a
download that simply failed.

Runs `uvx --refresh --from <target>==<version> python -m houdini_agent_panel
install` — literally the manual command this notice already told the artist
to type by hand — rather than re-deriving the Houdini-detection/hython-
selection logic that command already does correctly in `install.py`.
`--refresh` matters: without it uvx can serve its own cached resolution of
`<target>` and report success having changed nothing, which is the exact
failure mode `_start_update`'s docstring already names for the manual
command.

The version is pinned explicitly (`Update.latest`, not a bare package name)
for a reason stronger than "why ask uvx to re-derive what we already know":
measured directly (owner report, then reproduced on this Mac with a planted
fake package on `PYTHONPATH`) that pinning ALONE does not fix it either.
Houdini's own package json prepends the deps tree to `PYTHONPATH`
(`houdini_package.py`), and this worker's subprocess inherits that from
`os.environ` like any other child. `PYTHONPATH` wins over a venv's own
site-packages regardless of what `uvx` resolved into that venv — so even
`uvx --from houdini-agent-panel==0.7.3 python -c "import houdini_agent_panel"`
imported the STALE package straight off `PYTHONPATH`, version pin or not.
`install.py::_panel_version()` then read `__version__` off that same stale
import and pinned the INNER `pip install --target` to it — a self-update
that silently reinstalled the version it was already running. This is the
exact same shadowing `mcp_runtime.SHADOWING_VARS` already exists to strip
for the fx server's own subprocess; stripping it here too is what actually
closes the bug — the explicit version pin only makes a correct resolution
faster and more deterministic once shadowing is no longer in the way.
"""

from __future__ import annotations

import logging
import subprocess
import threading
from shutil import which
from types import SimpleNamespace

from .. import mcp_runtime
from .qt import Signal
from .terminal_login import TerminalLoginWorker
from .worker import Worker, WorkerStopped

_log = logging.getLogger(__name__)

#: A fresh `uv` resolution of houdini-agent-panel and every dependency, on
#: a cold cache — comparable to `deps.py`'s own `_INSTALL_TIMEOUT` for the
#: same underlying `pip install` this drives, generous for the same reason:
#: the cost of waiting too long is a slow update, the cost of waiting too
#: little is one that's reported as broken while it was still working.
_UPDATE_TIMEOUT = 600.0

_NO_UV_MESSAGE = (
    "uv isn't on this machine's PATH, so the panel can't run the update itself. "
    "Install uv (https://astral.sh/uv), or run this by hand:\n"
    "    uvx --refresh --from {spec} python -m houdini_agent_panel install"
)

#: Substrings from pip's own output that mean "a file could not be
#: written because something still has it open" — the Windows sharing-
#: violation case above all, but POSIX can produce the same shape (a
#: permissions problem, an antivirus holding a lock) and deserves the
#: same, specific answer rather than being lumped in with a network
#: failure. Matched case-insensitively against the WHOLE captured output,
#: not line by line — pip/uv wrap the underlying OS error across lines.
_WRITE_FAILURE_SIGNATURES = (
    "winerror 32",  # ERROR_SHARING_VIOLATION, verbatim in Python's own OSError text
    "being used by another process",
    "access is denied",
    "permission denied",
    "errno 13",
    "read-only file system",
    "text file busy",  # ETXTBSY — a POSIX analogue for an executable/mapped file
)

#: Substrings that mean the update never reached the point of writing
#: anything — DNS, TLS, a dropped connection, a proxy that needed
#: exporting first (docs/facts from the install.sh work: this is common
#: on a studio network without HTTPS_PROXY set).
_DOWNLOAD_FAILURE_SIGNATURES = (
    "connectionerror",
    "connection refused",
    "connection reset",
    "could not find a version",
    "failed to resolve",
    "name or service not known",
    "name resolution",
    "network is unreachable",
    "no route to host",
    "proxyerror",
    "temporary failure in name resolution",
    "timed out",
    "certificate verify failed",
    "sslerror",
)


class SelfUpdateError(RuntimeError):
    """Raised from `work()` with the message already written for the
    artist — `Worker.failed` (the base class) carries `str(exc)` straight
    through, so classifying the failure happens once, here, not re-derived
    in the UI from a raw exit code."""


def _classify_failure(output: str, returncode: int, target: str) -> str:
    lowered = output.lower()
    for signature in _WRITE_FAILURE_SIGNATURES:
        if signature in lowered:
            return (
                f"Could not write the new files for {target} — something still has "
                "them open (on Windows this is the normal reason: Houdini itself). "
                "Close Houdini and run the update again."
            )
    for signature in _DOWNLOAD_FAILURE_SIGNATURES:
        if signature in lowered:
            return (
                f"Could not download the update for {target} — the network gave up "
                "partway through. If you're on a studio network, check Settings → "
                "Network for a proxy that needs to be set."
            )
    tail = "\n".join(output.strip().splitlines()[-8:])
    return (
        f"Updating {target} failed (exit code {returncode}). Last output:\n{tail}"
        if tail
        else f"Updating {target} failed (exit code {returncode}), with no output captured."
    )


class SelfUpdateWorker(Worker):
    """Runs the update for one PyPI-distributed target ("houdini-agent-panel"
    or "fxhoudinimcp") on a background thread, off the one Houdini paints
    the viewport with — the same reasoning as every other `Worker` in this
    codebase (`ui/worker.py`'s own docstring).
    """

    #: One line of the subprocess's own output, as it arrives — pip/uv's
    #: "Downloading X"/"Collecting Y" lines are the progress signal itself;
    #: there is no separate percentage to compute.
    progressed = Signal(str)
    succeeded = Signal()

    def __init__(self, target: str, version: str, *, parent=None) -> None:
        super().__init__(parent)
        self._target = target
        #: `Update.latest` — see this module's own docstring for why this is
        #: pinned into `--from` rather than left for uvx to resolve.
        self._version = version

    def work(self) -> None:  # noqa: D102 - Worker.work override
        # Same environment composition as a spawned terminal-login process
        # (`TerminalLoginWorker.build_env`) — not re-derived here: Houdini's
        # own process saw none of the artist's shell profile (no PATH
        # entry for `uv`, no proxy), and this needs exactly the same
        # widening. `SimpleNamespace(env={})` stands in for the
        # `terminal_auth` that method reads `.env` off of — there is no
        # per-command override here, unlike a real terminal auth method.
        env = TerminalLoginWorker.build_env(SimpleNamespace(env={}))
        # Houdini's own package json prepends its deps tree to PYTHONPATH
        # (`houdini_package.py`), and this subprocess inherits that from
        # `os.environ` like any other child spawned from here. Left in
        # place, it shadows whatever `uvx` actually resolved: `PYTHONPATH`
        # wins over a venv's own site-packages regardless of version pins,
        # so `python -m houdini_agent_panel install` imported the STALE
        # panel off `PYTHONPATH` and pinned the real install to ITS
        # version — measured directly, reproduced on this Mac with a
        # planted fake package on `PYTHONPATH`. Same fix as
        # `mcp_runtime._clean_env` already applies to the fx server's own
        # subprocess, for the identical reason.
        for name in mcp_runtime.SHADOWING_VARS:
            env.pop(name, None)

        uvx = which("uvx", path=env.get("PATH", ""))
        spec = f"{self._target}=={self._version}"
        if uvx is None:
            raise SelfUpdateError(_NO_UV_MESSAGE.format(spec=spec))

        argv = [
            uvx, "--refresh", "--from", spec,
            "python", "-m", "houdini_agent_panel", "install",
        ]
        # The exact command belongs in the log, not in the notice strip —
        # an artist reading the notice needs to know an update is under
        # way, not stare at a `--target` path; whoever ends up debugging a
        # failed one needs the literal argv, which is what `logbook`'s log
        # file is for.
        _log.info("self-update: %s", " ".join(argv))
        try:
            process = subprocess.Popen(
                argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
        except OSError as exc:
            raise SelfUpdateError(f"Could not start the update for {self._target}: {exc}") from exc

        # `for line in process.stdout` blocks on EACH read with no timeout
        # of its own — a child that goes silent (network stalled, proxy
        # dropped) hangs this loop forever, the exact "curl with no
        # --connect-timeout" shape already found and fixed in install.sh's
        # own fetches. A watchdog `Timer` kills the child if the WHOLE
        # operation overruns, independent of whether another line ever
        # arrives to notice it inline.
        timed_out = threading.Event()

        def _kill_on_timeout() -> None:
            timed_out.set()
            process.kill()

        timer = threading.Timer(_UPDATE_TIMEOUT, _kill_on_timeout)
        timer.start()
        lines: list[str] = []
        try:
            assert process.stdout is not None
            for line in process.stdout:
                line = line.rstrip("\n")
                if self.isInterruptionRequested():
                    process.terminate()
                    raise WorkerStopped
                if not line:
                    continue
                lines.append(line)
                _log.info("self-update (%s): %s", self._target, line)
                self.progressed.emit(line)
        finally:
            timer.cancel()

        returncode = process.wait()

        if timed_out.is_set():
            raise SelfUpdateError(
                f"Updating {self._target} timed out after {_UPDATE_TIMEOUT:.0f}s with no result."
            )
        if returncode != 0:
            raise SelfUpdateError(_classify_failure("\n".join(lines), returncode, self._target))

        self.succeeded.emit()


__all__ = ["SelfUpdateWorker", "SelfUpdateError"]

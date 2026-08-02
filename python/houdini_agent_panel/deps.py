"""Installing the panel's dependencies into Houdini's own Python.

Houdini 20.5 ships Python 3.11, Houdini 22 ships 3.13, and each has its own
ABI for `pydantic_core` (see architecture.md §0) — so dependencies aren't
installed into the installer's own Python, but via `hython -m pip install
--target` into a tree bound to that particular Houdini's Python version.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from fxhoudinimcp.install import printable_argv

#: Timeout for a one-off hython run — just printing the Python version.
_VERSION_TIMEOUT = 30.0
#: Timeout for pip install — wheels with binary extensions can be heavy.
_INSTALL_TIMEOUT = 600.0

#: Search roots, pulled out into module-level variables for testability:
#: unit tests replace them with directories under tmp_path instead of the
#: real `/Applications` etc.
_MAC_APPLICATIONS_ROOT = Path("/Applications/Houdini")
_LINUX_OPT_ROOT = Path("/opt")
_WINDOWS_PROGRAM_FILES = Path("C:/Program Files/Side Effects Software")


class DepsError(RuntimeError):
    """hython failed to start, failed to install dependencies, or produced unreadable output."""


def _system() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if os.name == "nt":
        return "windows"
    return "linux"


def _version_key(path: Path) -> tuple[int, ...]:
    """Sort key based on the Houdini build number embedded in the path.

    "Houdini20.5.589" must beat "Houdini20.5.445" — we compare as a tuple of
    numbers, not as a string (otherwise "589" < "445" lexicographically
    would matter for nothing, but "20.5.9" < "20.5.10" would break).
    """
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", path.as_posix())
    if not match:
        return (0, 0, 0)
    return tuple(int(part) for part in match.groups())


def _hfs_hython(hfs: Path) -> Path:
    # Through _system(), not os.name/sys.platform directly — this way tests
    # can patch a single function without touching os.name globally
    # (pathlib itself looks at os.name to decide whether to build a
    # WindowsPath or PosixPath, and patching it on a different OS breaks
    # Path construction right there in the test).
    name = "hython.exe" if _system() == "windows" else "hython"
    return hfs / "bin" / name


def find_hython(houdini_version: str) -> Path | None:
    """Find `hython` for a given Houdini version (e.g. "20.5").

    `$HFS` is honored first: if the artist (or the Houdini the installer
    itself is running under) has already pointed at an install directory
    explicitly, we trust that more than guessing from standard paths. If
    there are several candidates, we take the newest build.
    """
    hfs = os.environ.get("HFS")
    if hfs:
        candidate = _hfs_hython(Path(hfs))
        if candidate.is_file():
            return candidate

    candidates = _candidate_hythons(houdini_version)
    if not candidates:
        return None
    return max(candidates, key=_version_key)


def _candidate_hythons(houdini_version: str) -> list[Path]:
    system = _system()

    if system == "darwin":
        root = _MAC_APPLICATIONS_ROOT
        pattern = (
            f"Houdini{houdini_version}.*/Frameworks/Houdini.framework/"
            f"Versions/{houdini_version}/Resources/bin/hython"
        )
    elif system == "windows":
        root = _WINDOWS_PROGRAM_FILES
        pattern = f"Houdini {houdini_version}*/bin/hython.exe"
    else:
        root = _LINUX_OPT_ROOT
        pattern = f"hfs{houdini_version}*/bin/hython"

    if not root.is_dir():
        return []
    return [p for p in root.glob(pattern) if p.is_file()]


def python_version_of(hython: Path, *, timeout: float = _VERSION_TIMEOUT) -> tuple[int, int] | None:
    """The Python version inside `hython`, e.g. (3, 11).

    hython prints a setuptools warning to stderr on every startup — that's
    not an error, so we only look at stdout.
    """
    argv = [str(hython), "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DepsError(f"failed to run {hython}: {exc}") from exc

    if result.returncode != 0:
        raise DepsError(
            f"{hython} exited with code {result.returncode}: {result.stderr.strip()}"
        )

    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    match = re.fullmatch(r"(\d+)\.(\d+)", lines[-1])
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)))


def install_deps(
    hython: Path,
    *,
    target: Path,
    requirement: str,
    find_links: str | None = None,
    offline: bool = False,
    dry_run: bool = False,
    out=print,
) -> list[str]:
    """`hython -m pip install --upgrade --target <target> <requirement>`.

    `--upgrade`, because reinstalling a new panel version over an existing
    deps tree is the normal scenario (updating the panel), not a one-time
    install.

    `find_links` and `offline` are deliberately kept separate, even though
    they started out as a single flag. "Take the panel's wheel from this
    folder" and "don't touch the internet at all" are different intents,
    and merging them broke the main development scenario: you build a wheel
    locally and install it, but there's nowhere to get its dependencies
    (`acp`, `pydantic`) from, because `--no-index` shut those off too.
    """
    argv: list[str] = [
        str(hython), "-m", "pip", "install", "--upgrade", "--target", str(target), requirement,
    ]
    if find_links:
        argv += ["--find-links", find_links]
    if offline:
        argv.append("--no-index")

    if dry_run:
        out(f"[dry-run] {printable_argv(argv)}")
        return []

    target.mkdir(parents=True, exist_ok=True)
    out(f"Installing dependencies: {printable_argv(argv)}")
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=_INSTALL_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DepsError(f"pip install failed to start: {exc}") from exc

    lines = [line for line in result.stdout.splitlines() if line]
    for line in lines:
        out(line)

    if result.returncode != 0:
        for line in result.stderr.splitlines():
            out(line)
        raise DepsError(f"pip install exited with code {result.returncode}")

    return lines


def deps_ready(target: Path) -> bool:
    """Does the `target` tree look like the panel's dependencies are already installed?

    We check for two markers: `acp` (the ACP SDK, `agent-client-protocol`)
    and `houdini_agent_panel` itself — both must appear after a successful
    `pip install --target`, and their absence is a reliable sign of an
    interrupted install.
    """
    return (target / "acp").is_dir() and (target / "houdini_agent_panel").is_dir()


__all__: Sequence[str] = [
    "DepsError",
    "find_hython",
    "python_version_of",
    "install_deps",
    "deps_ready",
]

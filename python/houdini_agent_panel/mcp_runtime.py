"""Houdini's own plain CPython, so opening a conversation doesn't cost 10s.

`HAP_PYTHON` is the interpreter that runs the fx MCP server, and it is
whatever ran the installer. Run the installer through `hython` — which is
the documented way to update — and the server inherits Houdini's *embedded*
interpreter, which loads the whole of Houdini before it runs a line.

Measured on 22.0.368:

    hython -c pass                    8.9s, 10.3s, 16.5s
    full fx server initialize          8.5s, 10.9s, 14.6s

An empty program costs as much as the entire server, so none of the wait is
the server, the ports or the protocol. It is the interpreter, and it is
paid on every `session/new`.

But `hython` is not the only Python in a Houdini install. Every build ships
the stock CPython it is built on, without the Houdini wrapper:

    macOS    $HFS/../../../Python.framework/Versions/3.13/bin/python3.13
    Linux    $HFS/python/bin/python3.13
    Windows  $HFS/python313/python.exe

Measured, same machine and same server:

    plain python -c pass              0.09s (0.05s on Linux)
    full fx server initialize          1.5s, 1.8s, 4.6s (first run cold)

Six to ten times faster, and it costs nothing to obtain — no download, no
virtualenv, no dependency on what the artist happens to have installed. It
also has the stock asyncio policy rather than `haio`, so
`scene.FX_BOOTSTRAP` finds nothing to repair.

Best of all it is the *same* interpreter version the deps tree was built
for — necessarily, since that tree was installed by this very Houdini's
pip. So `fxhoudinimcp` and its compiled dependencies (`pydantic_core`
above all) import straight from the tree with `PYTHONPATH`, and there is no
second copy of anything to keep in step.

If the interpreter cannot be found or cannot import the server, nothing
breaks: the installer keeps `hython`, the server still starts (that is what
`scene.FX_BOOTSTRAP` is for), and it is merely slow.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

#: Variables that would let another interpreter's world answer for this one.
#: Houdini's package file prepends the deps tree to `PYTHONPATH` and every
#: child of the installer inherits it — which once made an unrelated
#: environment look like it had `fxhoudinimcp` installed when it held
#: nothing but pip. The check below sets `PYTHONPATH` itself, deliberately,
#: and must not be handed a second one.
_SHADOWING_VARS = ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP")

_CHECK_TIMEOUT = 60.0


def is_houdini_python(executable: str | os.PathLike[str]) -> bool:
    """Is this Houdini's *embedded* interpreter — the slow one?

    By name, not by asking it: asking costs a full Houdini start, which is
    the thing being avoided, and `hython`/`hython.exe` is the only name
    SideFX ships it under on any platform.
    """
    return Path(executable).name.lower().startswith("hython")


def plain_python_candidates(hython: Path, pyver: tuple[int, int]) -> list[Path]:
    """Where the stock CPython sits relative to `hython`, per platform.

    `hython` lives in `$HFS/bin`, so `$HFS` is its grandparent everywhere.
    All three layouts are returned regardless of the platform we are running
    on — a wrong one simply does not exist on disk, and checking beats
    branching on `sys.platform` for something this cheap.
    """
    hfs = hython.parent.parent
    tag = f"{pyver[0]}.{pyver[1]}"
    flat = f"{pyver[0]}{pyver[1]}"
    candidates = [
        # Linux, and Windows' MSI layout for the interpreter binary.
        hfs / "python" / "bin" / f"python{tag}",
        hfs / "python" / "bin" / "python3",
        hfs / f"python{flat}" / "python.exe",
        hfs / "python" / "python.exe",
    ]
    if len(hfs.parents) > 3:
        # macOS: Python.framework sits beside Houdini.framework, so four
        # levels up from $HFS, which is
        # `…/Frameworks/Houdini.framework/Versions/22.0/Resources`. (Counted
        # wrong the first time and landed inside Houdini.framework itself —
        # the path simply did not exist, which is why `find` verifies by
        # importing rather than by trusting a constructed path.)
        # `/opt/hfs22.0` has no fourth parent at all, and asking for one
        # raised IndexError — a crash in the middle of an install, on the
        # platform where this candidate can never apply.
        candidates.append(
            hfs.parents[3] / "Python.framework" / "Versions" / tag / "bin" / f"python{tag}"
        )
    return candidates


def _clean_env(pythonpath: Path | None) -> dict[str, str]:
    env = dict(os.environ)
    for name in _SHADOWING_VARS:
        env.pop(name, None)
    if pythonpath is not None:
        env["PYTHONPATH"] = str(pythonpath)
    return env


def _imports(python: Path, module: str, pythonpath: Path | None) -> bool:
    """Can this interpreter import the server? The only question that counts."""
    try:
        result = subprocess.run(
            [str(python), "-c", f"import {module}"],
            capture_output=True,
            timeout=_CHECK_TIMEOUT,
            check=False,
            env=_clean_env(pythonpath),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def find(
    hython: Path,
    pyver: tuple[int, int],
    deps: Path,
    *,
    out=print,
    module_name: str = "fxhoudinimcp",
) -> Path | None:
    """Houdini's plain CPython for this `hython`, or None.

    Verified by actually importing the server through it — a path that
    exists proves nothing, and recording an interpreter that cannot start
    the server would trade slow for broken.
    """
    for candidate in plain_python_candidates(hython, pyver):
        if not candidate.is_file():
            continue
        if not _imports(candidate, module_name, deps):
            out(f"  {candidate} cannot import {module_name} — not using it")
            continue
        return candidate
    return None


__all__ = ["find", "is_houdini_python", "plain_python_candidates"]

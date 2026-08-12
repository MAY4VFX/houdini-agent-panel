"""Where the panel stores its files.

We don't take on a separate dependency on ``platformdirs``: the rule for
three OSes is shorter than the conversation about why there's an extra
wheel in the dependency tree inside Houdini.

Everything returned here lives under a single root, and that root can be
overridden by the ``HAP_DATA_DIR`` variable. That's also the only entry
point tests need: no need to patch functions, just point at a temp
directory.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

APP_NAME = "HoudiniAgentPanel"
#: Name of the variable that overrides the data root.
DATA_DIR_ENV = "HAP_DATA_DIR"


def _default_root() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        if base:
            return Path(base) / APP_NAME
        return Path.home() / "AppData" / "Local" / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "houdini-agent-panel"


def data_dir() -> Path:
    """The panel's user data root. Created if it doesn't exist yet."""
    override = os.environ.get(DATA_DIR_ENV)
    root = Path(override).expanduser() if override else _default_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def python_tag(version_info: tuple[int, int] | None = None) -> str:
    """``py3.11`` — the name used to split up dependency trees.

    Houdini 20.5 ships Python 3.11, Houdini 22 ships 3.13. ``pydantic_core``
    has a binary for a specific ABI, so one shared tree for both versions
    isn't possible.
    """
    major, minor = version_info or sys.version_info[:2]
    return f"py{major}.{minor}"


def _sub(*parts: str) -> Path:
    path = data_dir().joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def deps_dir(tag: str | None = None) -> Path:
    return _sub("deps", tag or python_tag())


def agents_dir() -> Path:
    return _sub("agents")


def agent_dir(agent_id: str) -> Path:
    return _sub("agents", agent_id)


def node_dir() -> Path:
    return _sub("node")


def cache_dir() -> Path:
    return _sub("cache")


def logs_dir() -> Path:
    return _sub("logs")


def settings_path() -> Path:
    return data_dir() / "settings.json"


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write `text` to `path` so a reader never sees a partial or
    interleaved file — not even under two writers racing each other.

    `os.replace` itself is atomic; what wasn't is the temp file every
    caller wrote it FROM. Five call sites (`registry.py`,
    `conversations_store.py`, `settings.py`, `orphans.py`, `updates.py`)
    each hand-rolled the same `path.with_suffix(path.suffix + ".tmp")` —
    one FIXED name, shared by every writer. Two processes (two Houdini
    sessions, or the panel racing an installer) writing that same name at
    once can interleave: one truncates and starts writing while the other
    is mid-write, and whichever finishes last determines the final
    `os.replace` — but the bytes already in the file by then can be a
    hybrid of both writes. Measured for real, byte for byte: an owner's
    `registry.json` cache was a complete, valid 35728-byte JSON document
    plus exactly one trailing `}` — not truncated, not garbage, one
    leftover byte from a longer write that a shorter one raced and lost to
    (docs/facts/on-disk-writes.md). `_read_cache`'s own `except ValueError:
    return None` swallowed the corruption silently, and a stale-but-valid
    cache (`max_age=None` would normally accept one) can't save a file
    that doesn't parse at ANY age — the Agents section stayed empty with
    nothing in `panel.log` to explain why.

    `tempfile.mkstemp` gives every CALL its own name, in the SAME
    directory as `path` (required for `os.replace` to stay atomic — a
    temp file on a different filesystem/mount is a copy, not a rename).
    Two concurrent writers now hold two different files; neither can ever
    see the other's partial content, and whichever `os.replace` runs last
    simply wins outright, same as it always should have.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def open_in_file_manager(path: Path) -> None:
    """Show the folder in Finder/Explorer/the file manager.

    Errors are swallowed: the "Open" button in settings is no reason to
    bring down the panel on a machine without a graphical file manager.
    """
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        elif os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["xdg-open", str(path)])
    except OSError:
        pass

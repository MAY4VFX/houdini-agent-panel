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

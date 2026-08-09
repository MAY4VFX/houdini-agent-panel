"""Generating the plugin's package json and locating Houdini directories on disk.

The pattern was borrowed from `fxhoudinimcp/houdini_package.py` (see
`docs/facts/fxhoudinimcp.md` §2): only look at directories that already
exist, write without a BOM, don't guess about several installed Houdini
versions at once. The logic itself is our own — unlike fxhoudinimcp we
don't require ``packages/`` to already exist (it can be created), and the
set of OS paths differs, because we also need to pull the Houdini version
out of the directory name (for `deps.py`).
"""

from __future__ import annotations

import os

import json
import platform
import re
from pathlib import Path

#: The filename Houdini looks for in ``<prefs>/packages/``.
PACKAGE_NAME = "houdini_agent_panel.json"

#: "20.5" out of "20.5" (macOS) or "houdini20.5" (Linux/Windows).
_VERSION_RE = re.compile(r"^(?:houdini)?(\d+\.\d+)$")


def plugin_path() -> Path:
    """The Houdini plugin tree that ships alongside the package."""
    return Path(__file__).resolve().parent / "houdini"


def package_json(
    *,
    deps: Path,
    installer_python: str,
    plugin: Path | None = None,
    source: Path | None = None,
    mcp_path: Path | None = None,
) -> str:
    """Build the package json in exactly the format from architecture.md §0.

    ``deps`` is where `install_deps` puts the panel's dependencies (``pip
    install --target``), ``installer_python`` is the interpreter the
    installer was run from (the panel needs it for exactly one thing:
    building ``mcpServers[0].command`` for fxhoudinimcp, see `scene.py`).

    ``plugin`` is an optional override for the plugin tree's path. By
    default, ``path`` points at ``$HAP_DEPS/houdini_agent_panel/houdini``
    — that's where pip itself puts the package along with its package-data.
    An explicit ``plugin`` is needed for scenarios with no package in deps
    (e.g. ``--skip-deps``/a dev run straight from source) — in that case
    the path is written as an absolute one instead of through the
    variable.

    ``source`` turns on dev mode: the path to a repository checkout, whose
    ``python/`` goes on ``PYTHONPATH`` ahead of the deps tree and whose
    plugin tree becomes ``path``. Everything else stays as it is — compiled
    dependencies still come from ``deps``.
    """
    if source is not None:
        # Dev mode: Houdini imports the checkout, not the installed copy.
        #
        # Without this, editing the repo changes nothing you can see: Houdini
        # keeps loading the wheel from the deps tree, and the panel on screen
        # is a different build from the one in the editor — which is exactly
        # how an afternoon disappears comparing two versions of the same
        # widget.
        #
        # The deps tree stays on the path behind the checkout: `acp` and
        # `pydantic` carry compiled extensions built for this Houdini's
        # Python, and those still have to come from there.
        package_root = (source / "python").as_posix()
        python_path = f"{package_root}{os.pathsep}$HAP_DEPS"
        path_value = (source / "python" / "houdini_agent_panel" / "houdini").as_posix()
    else:
        python_path = "$HAP_DEPS"
        path_value = (
            plugin.as_posix() if plugin is not None else "$HAP_DEPS/houdini_agent_panel/houdini"
        )
    env: list[dict] = [
        {"HAP_DEPS": deps.as_posix()},
        {"HAP_PYTHON": installer_python},
    ]
    if mcp_path is not None:
        # Where `HAP_PYTHON` finds `fxhoudinimcp`. Written only when the
        # installer chose an interpreter that needs to be told — Houdini's
        # plain CPython, which has nothing installed in it but is the exact
        # version this tree was built for. An interpreter that carries its
        # own copy (the uvx install path) must NOT be handed this: the tree's
        # compiled extensions are built for one Python version only.
        env.append({"HAP_MCP_PATH": mcp_path.as_posix()})
    env.append({"PYTHONPATH": {"value": python_path, "method": "prepend"}})
    payload = {"env": env, "path": path_value}
    return json.dumps(payload, indent=4) + "\n"


def houdini_version_of(prefs_dir: Path) -> str | None:
    """"20.5" out of a prefs directory's name, None if the name doesn't look like a version."""
    match = _VERSION_RE.match(prefs_dir.name)
    return match.group(1) if match else None


def candidate_package_dirs() -> list[Path]:
    """The ``packages/`` directory for every Houdini found on the machine.

    Returns only the ones whose version prefs directory actually exists
    (``~/Library/Preferences/houdini/20.5`` and the like) — we never guess
    about Houdini itself. ``packages/`` inside it, on the other hand, can be
    created: that's the normal case for the first package being installed
    into a fresh artist profile.
    """
    prefs_dirs = _candidate_prefs_dirs()
    result = []
    for prefs_dir in sorted(prefs_dirs, key=lambda p: p.name):
        if houdini_version_of(prefs_dir) is None:
            continue
        packages = prefs_dir / "packages"
        packages.mkdir(parents=True, exist_ok=True)
        result.append(packages)
    return result


def _candidate_prefs_dirs() -> list[Path]:
    home = Path.home()
    system = platform.system()

    if system == "Darwin":
        root = home / "Library" / "Preferences" / "houdini"
        if not root.is_dir():
            return []
        return [p for p in root.iterdir() if p.is_dir()]

    if system == "Windows":
        # Three roots, not one, and the second is the reason: OneDrive's
        # "Back up your Documents folder" moves the real Documents to
        # `~/OneDrive/Documents` and leaves `~/Documents` either absent or a
        # redirect Houdini does not use. It is on by default on a
        # consumer/managed Windows 11, so `~/Documents` alone means the
        # installer reports "no Houdini preferences directory found" on a
        # machine that plainly has one. fxhoudinimcp's own
        # `candidate_package_dirs` already looks in all three (see
        # docs/facts/fxhoudinimcp.md §2) — this matches it rather than
        # inventing a fourth answer. `home` itself is there for the
        # `$HOUDINI_USER_PREF_DIR`-style layout some studios keep.
        found: list[Path] = []
        for root in (home / "Documents", home / "OneDrive" / "Documents", home):
            if not root.is_dir():
                continue
            for entry in sorted(root.glob("houdini*")):
                if entry.is_dir() and entry not in found:
                    found.append(entry)
        return found

    # Linux, and anything that isn't Darwin/Windows.
    if not home.is_dir():
        return []
    return [p for p in home.glob("houdini*") if p.is_dir()]

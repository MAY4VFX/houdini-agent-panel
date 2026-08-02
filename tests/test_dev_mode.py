"""Dev mode: Houdini imports the checkout, not the installed wheel.

Without it, editing the repo changes nothing visible — Houdini keeps loading
the package from the deps tree, and the panel on screen is a different build
from the one in the editor. That cost real time: two versions of the same UI
were compared side by side before anyone noticed they were different builds.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from houdini_agent_panel import houdini_package


def _payload(**kwargs) -> dict:
    return json.loads(
        houdini_package.package_json(
            deps=Path("/data/deps/py3.13"), installer_python="/usr/bin/python3", **kwargs
        )
    )


def _python_path(payload: dict) -> str:
    for entry in payload["env"]:
        if "PYTHONPATH" in entry:
            return entry["PYTHONPATH"]["value"]
    raise AssertionError("PYTHONPATH missing from the package file")


def test_without_dev_mode_nothing_changes():
    payload = _payload()

    assert _python_path(payload) == "$HAP_DEPS"
    assert payload["path"] == "$HAP_DEPS/houdini_agent_panel/houdini"


def test_checkout_comes_before_the_deps_tree():
    """Order is the whole point: the checkout has to win the import."""
    payload = _payload(source=Path("/repo"))

    parts = _python_path(payload).split(os.pathsep)
    assert parts[0] == "/repo/python"
    assert "$HAP_DEPS" in parts


def test_deps_tree_stays_on_the_path():
    """`acp` and `pydantic` carry compiled extensions built for this
    Houdini's Python — dropping the deps tree would break the panel outright."""
    payload = _payload(source=Path("/repo"))

    assert "$HAP_DEPS" in _python_path(payload)
    assert payload["env"][0]["HAP_DEPS"] == "/data/deps/py3.13"


def test_plugin_tree_points_at_the_checkout():
    payload = _payload(source=Path("/repo"))

    assert payload["path"] == "/repo/python/houdini_agent_panel/houdini"


def test_dev_mode_still_records_the_mcp_interpreter():
    """HAP_PYTHON is how the panel builds mcpServers for fxhoudinimcp —
    dev mode must not cost the artist the scene connection."""
    payload = _payload(source=Path("/repo"))

    assert payload["env"][1]["HAP_PYTHON"] == "/usr/bin/python3"

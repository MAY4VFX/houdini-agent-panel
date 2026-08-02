"""The version is declared in two places and must match.

Otherwise the install breaks halfway through: the installer asks pip for
exactly `houdini-agent-panel==<version from __init__>`, while PyPI carries
whatever pyproject built. The mismatch is only visible to a user, and only
after the install has already told them everything's fine.
"""

from __future__ import annotations

import re
from pathlib import Path

import houdini_agent_panel

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_version_matches_package_version():
    text = (REPO_ROOT / "pyproject.toml").read_text("utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    assert match, "could not find a version line in pyproject.toml"
    assert match.group(1) == houdini_agent_panel.__version__

"""PATH for the npx agent.

Kept as a separate file because this is a regression that was only found by
a live run: every npx unit test was green, but the agent on a machine
without Node died before its first byte.
"""

from __future__ import annotations

import os
from pathlib import Path

from houdini_agent_panel import node


def test_path_with_node_puts_our_node_first():
    result = node.path_with_node(Path("/data/node/22.14.0/bin/node"), "/usr/bin:/bin")

    assert result.split(os.pathsep)[0] == "/data/node/22.14.0/bin"
    assert "/usr/bin" in result.split(os.pathsep)


def test_path_with_node_does_not_duplicate_itself():
    """Restarting the agent shouldn't make PATH grow forever."""
    once = node.path_with_node(Path("/data/node/bin/node"), "/usr/bin")
    twice = node.path_with_node(Path("/data/node/bin/node"), once)

    assert once == twice


def test_path_with_node_keeps_existing_tools():
    """The agent might need git and other tools from the machine — we
    extend PATH, not replace it."""
    result = node.path_with_node(Path("/data/node/bin/node"), "/opt/homebrew/bin:/usr/bin")

    assert "/opt/homebrew/bin" in result.split(os.pathsep)

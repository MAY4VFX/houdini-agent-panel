"""PATH для npx-агента.

Отдельным файлом, потому что это регрессия, найденная только живым запуском:
все юнит-тесты на npx были зелёными, а агент на машине без Node умирал до
первого байта.
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
    """Повторный запуск агента не должен наращивать PATH до бесконечности."""
    once = node.path_with_node(Path("/data/node/bin/node"), "/usr/bin")
    twice = node.path_with_node(Path("/data/node/bin/node"), once)

    assert once == twice


def test_path_with_node_keeps_existing_tools():
    """Агенту могут понадобиться git и прочее с машины — PATH дополняем,
    а не подменяем."""
    result = node.path_with_node(Path("/data/node/bin/node"), "/opt/homebrew/bin:/usr/bin")

    assert "/opt/homebrew/bin" in result.split(os.pathsep)

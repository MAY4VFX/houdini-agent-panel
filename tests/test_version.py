"""Версия объявлена в двух местах и обязана совпадать.

Иначе установка ломается на середине: инсталлятор просит у pip ровно
`houdini-agent-panel==<версия из __init__>`, а на PyPI лежит то, что собрал
pyproject. Расхождение видно только у пользователя и только после того, как
установка уже сказала ему, что всё идёт хорошо.
"""

from __future__ import annotations

import re
from pathlib import Path

import houdini_agent_panel

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_version_matches_package_version():
    text = (REPO_ROOT / "pyproject.toml").read_text("utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    assert match, "в pyproject.toml не нашлась строка version"
    assert match.group(1) == houdini_agent_panel.__version__

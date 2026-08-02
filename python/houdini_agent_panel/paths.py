"""Где панель хранит свои файлы.

Отдельной зависимости на ``platformdirs`` не берём: правило на три ОС короче,
чем разговор о том, зачем в дереве зависимостей внутри Houdini лишнее колесо.

Всё, что здесь возвращается, живёт под одним корнем, и корень переопределяется
переменной ``HAP_DATA_DIR``. Это же — единственная точка входа для тестов: не
надо патчить функции, достаточно указать temp-директорию.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

APP_NAME = "HoudiniAgentPanel"
#: Имя переменной, которой перекрывается корень данных.
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
    """Корень пользовательских данных панели. Создаётся, если его ещё нет."""
    override = os.environ.get(DATA_DIR_ENV)
    root = Path(override).expanduser() if override else _default_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def python_tag(version_info: tuple[int, int] | None = None) -> str:
    """``py3.11`` — по этому имени разводятся деревья зависимостей.

    Houdini 20.5 несёт Python 3.11, Houdini 22 — 3.13. У ``pydantic_core`` бинарь
    под конкретный ABI, поэтому одно общее дерево на обе версии невозможно.
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
    """Показать папку в Finder/Explorer/файловом менеджере.

    Ошибку глушим: кнопка «Открыть» в настройках не повод ронять панель на
    машине без графического файлового менеджера.
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

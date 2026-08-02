"""Генерация package json плагина и поиск директорий Houdini на диске.

Паттерн подсмотрен у `fxhoudinimcp/houdini_package.py` (см.
`docs/facts/fxhoudinimcp.md` §2): искать только уже существующие директории,
писать без BOM, не гадать про несколько установленных версий Houdini разом.
Сама логика своя — в отличие от fxhoudinimcp мы не требуем существования
``packages/`` заранее (её можно создать), а состав ОС-путей другой, потому что
нам ещё нужно вытащить версию Houdini из имени директории (для `deps.py`).
"""

from __future__ import annotations

import json
import platform
import re
from pathlib import Path

#: Имя файла, которое Houdini ищет в ``<prefs>/packages/``.
PACKAGE_NAME = "houdini_agent_panel.json"

#: "20.5" из "20.5" (macOS) или "houdini20.5" (Linux/Windows).
_VERSION_RE = re.compile(r"^(?:houdini)?(\d+\.\d+)$")


def plugin_path() -> Path:
    """Дерево плагина Houdini, которое едет вместе с пакетом."""
    return Path(__file__).resolve().parent / "houdini"


def package_json(
    *,
    deps: Path,
    installer_python: str,
    plugin: Path | None = None,
) -> str:
    """Собрать package json ровно в формате architecture.md §0.

    ``deps`` — куда `install_deps` кладёт зависимости панели (``pip install
    --target``), ``installer_python`` — интерпретатор, из которого запущен
    инсталлятор (нужен панели только на одно: собрать ``mcpServers[0].command``
    для fxhoudinimcp, см. `scene.py`).

    ``plugin`` — необязательный override пути к дереву плагина. По умолчанию
    ``path`` ссылается на ``$HAP_DEPS/houdini_agent_panel/houdini`` — туда сам
    pip кладёт пакет вместе с его package-data. Явный ``plugin`` нужен для
    сценариев без пакета в deps (например ``--skip-deps``/dev-запуск прямо из
    исходников) — тогда путь пишется абсолютным, а не через переменную.
    """
    path_value = plugin.as_posix() if plugin is not None else "$HAP_DEPS/houdini_agent_panel/houdini"
    payload = {
        "env": [
            {"HAP_DEPS": deps.as_posix()},
            {"HAP_PYTHON": installer_python},
            {"PYTHONPATH": {"value": "$HAP_DEPS", "method": "prepend"}},
        ],
        "path": path_value,
    }
    return json.dumps(payload, indent=4) + "\n"


def houdini_version_of(prefs_dir: Path) -> str | None:
    """"20.5" из имени prefs-директории, None — если имя не похоже на версию."""
    match = _VERSION_RE.match(prefs_dir.name)
    return match.group(1) if match else None


def candidate_package_dirs() -> list[Path]:
    """Директории ``packages/`` для каждой найденной на машине Houdini.

    Возвращает только те, чья prefs-директория версии реально существует
    (``~/Library/Preferences/houdini/20.5`` и т.п.) — саму Houdini не гадаем.
    ``packages/`` внутри неё, наоборот, можно создать: это обычное дело для
    первого пакета, который ставится в свежий профиль художника.
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
        root = home / "Documents"
        if not root.is_dir():
            return []
        return [p for p in root.glob("houdini*") if p.is_dir()]

    # Linux и всё, что не Darwin/Windows.
    if not home.is_dir():
        return []
    return [p for p in home.glob("houdini*") if p.is_dir()]

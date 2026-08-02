"""Привязка панели к своей сцене Houdini.

Панель живёт внутри процесса Houdini, поэтому порт своего fx-сервера ей не
надо угадывать сканом — `fxhoudinimcp_server.startup` в этом же процессе
знает его точно (см. docs/architecture.md §4). HTTP-скан 8100..8115 — только
запасной путь на случай, если плагин fx не загружен или устарел; он находит
ЧУЖУЮ Houdini (первую живую в диапазоне), поэтому используется как деградация
с явным логом, а не молча.

`hou` и `fxhoudinimcp_server` импортируются лениво внутри функций: модуль
обязан импортироваться в тестах вне Houdini.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

FX_SERVER_NAME = "fxhoudini"

#: Диапазон и таймаут — те же, что использует сам fxhoudinimcp при автоскане
#: (см. docs/facts/fxhoudinimcp.md §3): база 8100, 16 портов, 1 секунда на порт.
_PORT_SCAN_BASE = 8100
_PORT_SCAN_COUNT = 16
_PORT_SCAN_TIMEOUT = 1.0

_log = logging.getLogger(__name__)


def fx_port() -> int | None:
    """Порт fx-сервера в ЭТОМ процессе Houdini. None — сервер не поднят."""
    try:
        import fxhoudinimcp_server.startup as startup  # noqa: PLC0415 - см. докстринг модуля
    except ImportError:
        return _scan_for_any_fx_port()

    if not startup.is_running():
        return None
    return startup.get_port()


def fx_host() -> str:
    return "127.0.0.1"


def fx_python() -> str:
    """Интерпретатор, в котором стоит fxhoudinimcp.

    Внутри Houdini `sys.executable` — бинарь самой Houdini, не Python: MCP-
    сервер таким интерпретатором не поднимется. `HAP_PYTHON` — путь,
    записанный инсталлятором панели именно для этой цели (см.
    docs/architecture.md §0).
    """
    return os.environ.get("HAP_PYTHON") or sys.executable


def mcp_servers() -> list[dict]:
    """Ровно то, что уходит в session/new как mcpServers.

    Пин порта обязателен: без него MCP-сервер сканирует диапазон и может
    подключиться к чужой открытой Houdini. `env` — список {name, value}
    (`McpServerStdio.env: list[EnvVariable]`), не словарь.
    """
    env = [{"name": "HOUDINI_HOST", "value": fx_host()}]
    port = fx_port()
    if port is not None:
        env.append({"name": "HOUDINI_PORT", "value": str(port)})
    else:
        # Сервер ещё не поднялся в этом процессе — пинить нечего. Без пина
        # агент сам просканирует диапазон; это тот же риск "чужой Houdini",
        # но деградация здесь неизбежна, т.к. настоящего порта попросту нет.
        _log.warning(
            "fx-сервер не поднят в этом процессе Houdini — mcpServers уйдёт "
            "без HOUDINI_PORT, агент будет сканировать диапазон сам"
        )
    return [
        {
            "name": FX_SERVER_NAME,
            "command": fx_python(),
            "args": ["-m", "fxhoudinimcp"],
            "env": env,
        }
    ]


def hip_dir() -> str:
    """$HIP. ТОЛЬКО с главного потока.

    Несохранённая сцена — $HOME, а не несуществующий untitled-путь: cwd в
    session/new обязан существовать.
    """
    import hou  # noqa: PLC0415 - лениво, модуль есть только внутри Houdini

    if hou.hipFile.isNewFile():
        return str(Path.home())

    directory = Path(hou.hipFile.path()).parent
    if not directory.is_dir():
        return str(Path.home())
    return str(directory)


def houdini_version() -> str:
    """Версия Houdini этого процесса.

    `HOUDINI_VERSION` — та же переменная окружения, которую сама Houdini
    экспортирует и которую отдаёт `mcp.health` fx-сервера (см.
    docs/facts/fxhoudinimcp.md §8) — не нужно ходить в `hou` за тем же самым.
    """
    version = os.environ.get("HOUDINI_VERSION")
    if version:
        return version
    try:
        import hou  # noqa: PLC0415

        return ".".join(str(part) for part in hou.applicationVersion())
    except Exception:  # noqa: BLE001 - версия для диагностики, падать нельзя
        return "unknown"


def is_fx_available() -> bool:
    return fx_port() is not None


def _scan_for_any_fx_port() -> int | None:
    """Запасной путь: HTTP-скан `mcp.health` по 8100..8115.

    Логируется как деградация — этот путь по конструкции не может отличить
    "нашу" Houdini от соседней, работающей на той же машине.
    """
    _log.warning(
        "fxhoudinimcp_server недоступен изнутри процесса (плагин не "
        "загружен или устарел) — сканирую %s..%s по HTTP; это может найти "
        "ЧУЖУЮ Houdini, а не эту",
        _PORT_SCAN_BASE,
        _PORT_SCAN_BASE + _PORT_SCAN_COUNT - 1,
    )
    for port in range(_PORT_SCAN_BASE, _PORT_SCAN_BASE + _PORT_SCAN_COUNT):
        if _probe_health(port):
            return port
    return None


def _probe_health(port: int) -> bool:
    """Один запрос `mcp.health` к `http://127.0.0.1:<port>/api`.

    Форма запроса — form-urlencoded `json=["mcp.health", [], {}]`, как того
    хочет `hwebserver` (docs/facts/fxhoudinimcp.md §3-4). Любая ошибка
    (порт закрыт, таймаут, не-JSON ответ) — просто "порт не тот".
    """
    body = urllib.parse.urlencode({"json": json.dumps(["mcp.health", [], {}])}).encode("ascii")
    request = urllib.request.Request(f"http://{fx_host()}:{port}/api", data=body)
    try:
        with urllib.request.urlopen(request, timeout=_PORT_SCAN_TIMEOUT) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return isinstance(payload, dict) and payload.get("status") == "ok"

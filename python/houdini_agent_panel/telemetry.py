"""Телеметрия — по умолчанию выключена, только версии и факт падения.

Две независимые задвижки должны совпасть, прежде чем куда-то улетит хоть один
байт: тумблер в настройках (человек согласился) и переменная окружения
``HAP_TELEMETRY_URL`` (студия/дистрибутив задала эндпоинт). Нет второй — не
собираемся спрашивать «а куда слать», молча остаёмся no-op: включённый тумблер
без настроенного адреса не должен пытаться постучаться в никуда.

``build_payload`` — единственное место, где решается, что вообще разрешено
положить в событие. Список разрешённых ключей жёстко зашит (allowlist, а не
blacklist): так добавление нового вызывающего кода с новым ``**extra`` не
может случайно протащить что-то лишнее — незнакомый ключ просто отбрасывается,
а не сериализуется в надежде, что он okay. Это то, что делает обещание
"телеметрия не видит сцену" из docs/privacy.md проверяемым тестом, а не
честным словом.
"""

from __future__ import annotations

import os
import urllib.parse
from typing import Any

from .network import Fetcher, NetworkError, urlopen_fetch
from .settings import Settings

#: Адрес приёмника. Не задан — телеметрия не может быть включена в принципе.
TELEMETRY_URL_ENV = "HAP_TELEMETRY_URL"

#: Единственные ключи, которые build_payload готов записать в событие.
#: Всё остальное из **extra отбрасывается молча — см. докстринг модуля.
_ALLOWED_EXTRA_KEYS = ("agent_version", "exception_type")

_SEND_TIMEOUT = 5.0


def is_enabled(settings: Settings) -> bool:
    """И тумблер в настройках, и заданный эндпоинт — оба условия сразу."""
    return bool(settings.telemetry) and bool(os.environ.get(TELEMETRY_URL_ENV))


def build_payload(settings: Settings, *, event: str, **extra: Any) -> dict[str, Any]:
    """Собрать тело события. Никогда не бросает исключение.

    Версии — те, что реально доступны в этом процессе; недоступная версия
    (fx не установлен, Houdini не отвечает) просто отсутствует в payload,
    а не заменяется на фиктивное значение.
    """
    payload: dict[str, Any] = {"event": str(event), "os": _os_name()}

    panel_version = _panel_version()
    if panel_version:
        payload["panel_version"] = panel_version

    fx_version = _fx_version()
    if fx_version:
        payload["fx_version"] = fx_version

    houdini_version = _houdini_version()
    if houdini_version and houdini_version != "unknown":
        payload["houdini_version"] = houdini_version

    for key in _ALLOWED_EXTRA_KEYS:
        value = extra.get(key)
        if value:
            payload[key] = str(value)

    return payload


def send(event: str, *, settings: Settings, fetch: Fetcher | None = None, **extra: Any) -> None:
    """Отправить событие, если телеметрия включена. Никогда не мешает работе.

    Выключена или эндпоинт не задан — ни одного сетевого вызова. Отправка —
    простой GET с payload в query string поверх общего ``Fetcher`` (см.
    network.py): у панели нет причин заводить отдельный POST-транспорт ради
    события в пару десятков байт, а прогонять его через ``Fetcher`` обязаны
    все сетевые вызовы панели (тестовая страховка ``no_real_network``
    иначе не сработает). Любая сетевая ошибка глушится — художник не должен
    заметить, что телеметрия вообще пыталась куда-то сходить.
    """
    if not is_enabled(settings):
        return
    url = os.environ.get(TELEMETRY_URL_ENV)
    if not url:
        return

    payload = build_payload(settings, event=event, **extra)
    query = urllib.parse.urlencode(payload)
    separator = "&" if "?" in url else "?"
    full_url = f"{url}{separator}{query}"

    try:
        (fetch or urlopen_fetch)(full_url, timeout=_SEND_TIMEOUT)
    except NetworkError:
        pass


# --- источники значений, каждый обязан не падать -----------------------


def _os_name() -> str:
    import platform

    try:
        return platform.platform()
    except Exception:  # noqa: BLE001 - телеметрия не имеет права уронить панель
        return "unknown"


def _panel_version() -> str:
    try:
        from importlib.metadata import version

        return version("houdini-agent-panel")
    except Exception:  # noqa: BLE001 - метаданных может не быть в --target-дереве
        try:
            from . import __version__

            return __version__
        except Exception:  # noqa: BLE001
            return ""


def _fx_version() -> str:
    try:
        from importlib.metadata import version

        return version("fxhoudinimcp")
    except Exception:  # noqa: BLE001
        return ""


def _houdini_version() -> str:
    try:
        from . import scene

        return scene.houdini_version()
    except Exception:  # noqa: BLE001 - scene.py трогает hou, вне Houdini недоступен
        return "unknown"

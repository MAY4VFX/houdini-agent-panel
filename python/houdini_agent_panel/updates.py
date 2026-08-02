"""Сравнение версий: агенты из реестра, панель и fx с PyPI.

Своего парсера версий, а не зависимости на ``packaging``, потому что
``packaging`` — лишнее колесо в ``--target``-дереве, которое ставится внутрь
самой Houdini (см. docs/architecture.md §0). Разбор ниже покрывает то, что
реально встречается в номерах версий на PyPI и в реестре ACP: числовые
сегменты, пре-релизы (``a``/``b``/``rc``/alpha/beta), ``.postN``, ``.devN``.
Экзотика вроде эпох (``1!2.0``) или локальных версий (``+abc``) не нужна ни
одному из трёх пакетов, которые тут сравниваются.

Сравнение по сегментам (не отсортированные строки!) сохраняет тот же порядок
приоритетов, что и PEP 440: ``devN`` раньше любого пре-релиза, финальный
релиз позже любого пре-релиза, ``postN`` позже финального. Мусор в строке
версии — ``None``/``False``, не исключение: тихая плашка «обновление
доступно» появляющаяся каждый день из-за нечитаемой версии хуже, чем
отсутствие плашки вообще.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

from . import paths
from .network import Fetcher, NetworkError, fetch_json
from .settings import Settings

if TYPE_CHECKING:
    # Только для подсказок типов — во время исполнения на registry.py не
    # завязываемся (duck typing по .id/.name/.version), чтобы модуль не тянул
    # за собой циклических или преждевременных импортов.
    from .registry import AgentEntry

PYPI_URL = "https://pypi.org/pypi/{name}/json"

#: Пакеты панели и fx на PyPI — по ним сверяется kind="panel"/"fx".
_PANEL_PACKAGE = "houdini-agent-panel"
_FX_PACKAGE = "fxhoudinimcp"

_CACHE_FILE_NAME = "updates.json"
_MAX_AGE = timedelta(days=1)


@dataclass(frozen=True)
class Update:
    kind: str  # "agent" | "panel" | "fx"
    target: str  # agent_id или имя пакета
    label: str  # что показать человеку
    current: str
    latest: str


# --- разбор и сравнение версий ----------------------------------------------

_VERSION_RE = re.compile(
    r"""
    ^\s*
    v?
    (?P<release>[0-9]+(?:\.[0-9]+)*)
    (?:[-_.]?(?P<pre_l>alpha|beta|preview|pre|a|b|c|rc)[-_.]?(?P<pre_n>[0-9]*))?
    (?:[-_.]?(?P<post_l>post|rev|r)[-_.]?(?P<post_n>[0-9]*))?
    (?:[-_.]?(?P<dev_l>dev)[-_.]?(?P<dev_n>[0-9]*))?
    \s*$
    """,
    re.VERBOSE | re.IGNORECASE,
)

#: PEP 440 считает "c" синонимом "rc"; "pre"/"preview" — то же самое семейство.
_PRE_RANK = {"a": 0, "alpha": 0, "b": 1, "beta": 1, "c": 2, "rc": 2, "pre": 2, "preview": 2}

_NEG_INF = float("-inf")
_POS_INF = float("inf")


def _parse_version(text: str) -> tuple | None:
    """``(release, pre, post, dev)`` или ``None`` на всё, что не разобралось."""
    if not isinstance(text, str) or not text.strip():
        return None
    match = _VERSION_RE.match(text)
    if not match:
        return None

    release = tuple(int(part) for part in match.group("release").split("."))
    # Хвостовые нули не значимы (1.2.0 == 1.2): срезаем, чтобы сравнение
    # кортежей разной длины не путало "короче" с "меньше".
    while len(release) > 1 and release[-1] == 0:
        release = release[:-1]

    pre = None
    if match.group("pre_l"):
        rank = _PRE_RANK[match.group("pre_l").lower()]
        num = match.group("pre_n")
        pre = (rank, int(num) if num else 0)

    post = None
    if match.group("post_l"):
        num = match.group("post_n")
        post = int(num) if num else 0

    dev = None
    if match.group("dev_l"):
        num = match.group("dev_n")
        dev = int(num) if num else 0

    return (release, pre, post, dev)


def _version_key(text: str):
    """Ключ для сравнения кортежами. ``None`` — версия не разобралась.

    Порядок внутри одного релиза (по PEP 440):
    ``devN`` < любой пре-релиз < финальный релиз < ``postN``.
    """
    parsed = _parse_version(text)
    if parsed is None:
        return None
    release, pre, post, dev = parsed

    if pre is None and post is None and dev is not None:
        pre_key: tuple = (_NEG_INF,)  # чистый dev-релиз — раньше всех пре-релизов
    elif pre is None:
        pre_key = (_POS_INF,)  # финальный релиз — позже любого пре-релиза
    else:
        pre_key = pre

    post_key = (_NEG_INF,) if post is None else (post,)
    dev_key = (_POS_INF,) if dev is None else (dev,)
    return (release, pre_key, post_key, dev_key)


def compare_versions(a: str, b: str) -> int | None:
    """-1/0/1 по PEP 440-порядку; ``None`` — хоть одна версия не разобралась.

    Общая точка сравнения версий на весь проект: ``announcements.py``
    использует её же для таргетинга по ``panel_versions``, чтобы не заводить
    второй парсер версий рядом.
    """
    key_a, key_b = _version_key(a), _version_key(b)
    if key_a is None or key_b is None:
        return None
    if key_a < key_b:
        return -1
    if key_a > key_b:
        return 1
    return 0


def is_newer(latest: str, current: str) -> bool:
    """``latest`` строго новее ``current``. Мусор в любой из строк — ``False``."""
    cmp = compare_versions(latest, current)
    return cmp is not None and cmp > 0


# --- PyPI ---------------------------------------------------------------


def pypi_latest(name: str, *, fetch: Fetcher | None = None) -> str | None:
    """Последняя версия пакета на PyPI. ``None`` — ответ не в ожидаемой форме.

    Сетевые ошибки не глушатся здесь: это решение вызывающей стороны
    (``check`` ниже глушит их поштучно, чтобы недоступность PyPI для одного
    пакета не прятала результат для другого).
    """
    payload = fetch_json(PYPI_URL.format(name=name), fetch=fetch)
    if not isinstance(payload, dict):
        return None
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    version = info.get("version")
    return str(version) if version else None


def _current_panel_version() -> str | None:
    try:
        from importlib.metadata import version

        return version(_PANEL_PACKAGE)
    except Exception:  # noqa: BLE001 - метаданных может не быть в --target-дереве
        try:
            from . import __version__

            return __version__
        except Exception:  # noqa: BLE001
            return None


def _current_fx_version() -> str | None:
    try:
        from importlib.metadata import version

        return version(_FX_PACKAGE)
    except Exception:  # noqa: BLE001 - fxhoudinimcp недоступен вне Houdini-плагина
        return None


# --- проверка --------------------------------------------------------------


def _cache_path() -> Path:
    return paths.cache_dir() / _CACHE_FILE_NAME


def _read_cache(now: datetime) -> list[Update] | None:
    path = _cache_path()
    try:
        payload = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    checked_at_raw = payload.get("checked_at")
    if not isinstance(checked_at_raw, str):
        return None
    try:
        checked_at = datetime.fromisoformat(checked_at_raw)
    except ValueError:
        return None
    if now - checked_at >= _MAX_AGE:
        return None
    raw_updates = payload.get("updates")
    if not isinstance(raw_updates, list):
        return None
    updates: list[Update] = []
    for item in raw_updates:
        if not isinstance(item, dict):
            continue
        try:
            updates.append(
                Update(
                    kind=str(item["kind"]),
                    target=str(item["target"]),
                    label=str(item["label"]),
                    current=str(item["current"]),
                    latest=str(item["latest"]),
                )
            )
        except KeyError:
            continue
    return updates


def _write_cache(now: datetime, updates: list[Update]) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"checked_at": now.isoformat(), "updates": [asdict(u) for u in updates]}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), "utf-8")
    os.replace(tmp, path)


def check(
    *,
    settings: Settings,
    entries: Sequence[AgentEntry] = (),
    force: bool = False,
    fetch: Fetcher | None = None,
    now: datetime | None = None,
    panel_version: str | None = None,
    fx_version: str | None = None,
) -> list[Update]:
    """Список доступных обновлений: агенты (из ``entries``), панель, fx.

    ``settings.check_updates=False`` — ``[]`` и ни одного сетевого вызова,
    проверяется тестом по счётчику ``FakeFetcher.calls``.

    Кеш в ``<cache>/updates.json`` держит всю проверку целиком (агенты в неё
    попадают тоже, хотя сети не требуют) не чаще раза в сутки — так проще
    и совпадает с контрактом design.md, который не разводит агентов и PyPI
    по разной частоте проверки. ``force`` обходит кеш.

    ``panel_version``/``fx_version`` — переопределение автоопределяемой
    текущей версии (тестам нужно контролировать её без реальных метаданных
    пакета в окружении); по умолчанию берутся из ``importlib.metadata``.
    """
    if not settings.check_updates:
        return []

    now = now or datetime.now(timezone.utc)
    if not force:
        cached = _read_cache(now)
        if cached is not None:
            return cached

    updates: list[Update] = []

    for entry in entries:
        installed = settings.installed_agents.get(entry.id)
        if installed is None:
            continue
        if is_newer(entry.version, installed.version):
            updates.append(
                Update(
                    kind="agent",
                    target=entry.id,
                    label=f"{entry.name} {entry.version}",
                    current=installed.version,
                    latest=entry.version,
                )
            )

    for kind, package, current in (
        ("panel", _PANEL_PACKAGE, panel_version if panel_version is not None else _current_panel_version()),
        ("fx", _FX_PACKAGE, fx_version if fx_version is not None else _current_fx_version()),
    ):
        if not current:
            continue
        try:
            latest = pypi_latest(package, fetch=fetch)
        except NetworkError:
            # Один недоступный PyPI-пакет не должен прятать результат для
            # другого (агенты уже посчитаны, второй пакет ниже по циклу).
            continue
        if latest and is_newer(latest, current):
            updates.append(
                Update(kind=kind, target=package, label=f"{package} {latest}", current=current, latest=latest)
            )

    _write_cache(now, updates)
    return updates

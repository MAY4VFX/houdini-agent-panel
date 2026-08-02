"""Фид оповещений — канал связи со студией в обход апдейта пакета.

Источник — статический JSON по фиксированному адресу в этом же репозитории
(``feed/announcements.json``), но с точки зрения кода это чужой ответ из
интернета: правит его человек руками, значит там будут опечатки, отсутствующие
поля и значения не того типа. Битая ЗАПИСЬ пропускается — весь фид из-за неё
не теряется (см. ``_parse_one``).

Важность (`severity`) решает про UI (тихая плашка vs блокирующий попап над
полем ввода, см. design.md), но это не наша забота: этот модуль только
отдаёт список применимых оповещений, а какой виджет их рисует — дело
``ui/announcement.py``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Collection

from . import paths
from .network import Fetcher, fetch_json
from .settings import Settings
from .updates import compare_versions

#: Адрес фида по умолчанию.
#:
#: Работает только пока репозиторий публичный: `raw.githubusercontent.com`
#: анонимным запросам отдаёт 404 на приватные репозитории, а панель ходит
#: сюда без токена и не должна его иметь. Проверено запросом — на приватном
#: репозитории это ровно 404, а не ошибка доступа, так что и диагностировать
#: со стороны панели нечего.
DEFAULT_FEED_URL = (
    "https://raw.githubusercontent.com/MAY4VFX/houdini-agent-panel/main/feed/announcements.json"
)

#: Чем студия (или сам разработчик до публикации репозитория) переопределяет
#: адрес фида, не пересобирая пакет.
FEED_URL_ENV = "HAP_FEED_URL"


def feed_url() -> str:
    return os.environ.get(FEED_URL_ENV) or DEFAULT_FEED_URL


#: Оставлено для обратной совместимости с кодом и тестами, читавшими константу.
FEED_URL = DEFAULT_FEED_URL

_KNOWN_SEVERITIES = ("info", "blocking")
_CACHE_FILE_NAME = "announcements.json"
_MAX_AGE = timedelta(days=1)


@dataclass(frozen=True)
class Button:
    label: str
    url: str = ""


@dataclass(frozen=True)
class Announcement:
    id: str
    severity: str  # "info" | "blocking"
    title: str
    body: str = ""
    buttons: tuple[Button, ...] = ()
    panel_versions: str = ""  # спецификатор версий, "" — всем
    expires: str = ""  # ISO 8601, "" — бессрочно


# --- разбор фида -------------------------------------------------------


def parse_feed(payload: Any) -> list[Announcement]:
    if not isinstance(payload, dict):
        return []
    raw_items = payload.get("announcements")
    if not isinstance(raw_items, list):
        return []
    result: list[Announcement] = []
    for raw in raw_items:
        parsed = _parse_one(raw)
        if parsed is not None:
            result.append(parsed)
    return result


def _parse_one(raw: Any) -> Announcement | None:
    if not isinstance(raw, dict):
        return None
    ann_id = raw.get("id")
    title = raw.get("title")
    # id и title — единственное, без чего показывать оповещение нечем и
    # незачем (без id некуда положить факт "уже видел").
    if not isinstance(ann_id, str) or not ann_id:
        return None
    if not isinstance(title, str) or not title:
        return None

    severity = raw.get("severity")
    # Неизвестная будущая важность (например студия придумает "critical" в
    # новой версии панели, а у художника ещё старая) не должна ПО УМОЛЧАНИЮ
    # заблокировать ввод — тихая плашка безопаснее ошибочной блокировки.
    if severity not in _KNOWN_SEVERITIES:
        severity = "info"

    body = raw.get("body")
    body = body if isinstance(body, str) else ""

    buttons: list[Button] = []
    raw_buttons = raw.get("buttons")
    if isinstance(raw_buttons, list):
        for raw_button in raw_buttons:
            if not isinstance(raw_button, dict):
                continue
            label = raw_button.get("label")
            if not isinstance(label, str) or not label:
                continue
            url = raw_button.get("url")
            buttons.append(Button(label=label, url=url if isinstance(url, str) else ""))

    panel_versions = raw.get("panel_versions")
    panel_versions = panel_versions if isinstance(panel_versions, str) else ""

    expires = raw.get("expires")
    expires = expires if isinstance(expires, str) else ""

    return Announcement(
        id=ann_id,
        severity=severity,
        title=title,
        body=body,
        buttons=tuple(buttons),
        panel_versions=panel_versions,
        expires=expires,
    )


# --- таргетинг по версии панели -----------------------------------------

_CLAUSE_RE = re.compile(r"^(>=|<=|==|!=|>|<)\s*(.+)$")


def _panel_version_matches(specifier: str, panel_version: str) -> bool:
    """Спецификатор вида ``">=0.2,<0.4"``; условия через запятую — все обязаны сойтись.

    Пустая строка — оповещение для всех версий. Любая нечитаемая часть
    (незнакомый оператор, версия не по PEP 440, наша собственная версия не
    разобралась) исключает оповещение, а не показывает его всем: ошибка в
    таргетинге чужого фида не должна ПОКАЗАТЬ то, что предназначалось для
    другой версии панели — тот же принцип "молчание лучше", что и в
    ``updates.is_newer``.
    """
    if not specifier.strip():
        return True
    for clause in specifier.split(","):
        clause = clause.strip()
        if not clause:
            return False
        match = _CLAUSE_RE.match(clause)
        if not match:
            return False
        op, version = match.group(1), match.group(2).strip()
        cmp = compare_versions(panel_version, version)
        if cmp is None:
            return False
        if op == ">=" and cmp < 0:
            return False
        if op == "<=" and cmp > 0:
            return False
        if op == "==" and cmp != 0:
            return False
        if op == "!=" and cmp == 0:
            return False
        if op == ">" and cmp <= 0:
            return False
        if op == "<" and cmp >= 0:
            return False
    return True


def _parse_iso(text: str) -> datetime | None:
    if not text:
        return None
    try:
        # datetime.fromisoformat не понимает суффикс "Z" до Python 3.11, а мы
        # обязаны работать с 3.10 (нижняя поддерживаемая версия, см. CLAUDE.md).
        value = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _is_expired(expires: str, now: datetime) -> bool:
    parsed = _parse_iso(expires)
    if parsed is None:
        # Пустой "expires" — бессрочно осознанно; нечитаемая дата — тоже
        # бессрочно, но уже по необходимости: лучше показать лишний день,
        # чем молча похоронить важное сообщение из-за опечатки в дате.
        return False
    return parsed < now


def applicable(
    items: Collection[Announcement],
    *,
    panel_version: str,
    seen: Collection[str],
    now: datetime | None = None,
) -> list[Announcement]:
    """Оповещения, которые стоит показать: не показанные, не истёкшие, свои по версии."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    seen_ids = set(seen)
    result = []
    for ann in items:
        if ann.id in seen_ids:
            continue
        if _is_expired(ann.expires, now):
            continue
        if not _panel_version_matches(ann.panel_versions, panel_version):
            continue
        result.append(ann)
    return result


# --- сетевой поход + суточный кеш --------------------------------------


def _cache_path() -> Path:
    return paths.cache_dir() / _CACHE_FILE_NAME


def _feed_from_items(items: list[Announcement]) -> dict:
    """Обратно в форму фида — чтобы читать кеш тем же ``parse_feed``,
    не заводя для Announcement отдельный (де)сериализатор."""
    return {
        "version": 1,
        "announcements": [
            {
                "id": a.id,
                "severity": a.severity,
                "title": a.title,
                "body": a.body,
                "buttons": [{"label": b.label, "url": b.url} for b in a.buttons],
                "panel_versions": a.panel_versions,
                "expires": a.expires,
            }
            for a in items
        ],
    }


def _read_cache(now: datetime) -> list[Announcement] | None:
    path = _cache_path()
    try:
        payload = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    checked_at = _parse_iso(payload.get("checked_at", ""))
    if checked_at is None:
        return None
    if now - checked_at >= _MAX_AGE:
        return None
    return parse_feed(payload.get("feed"))


def _write_cache(now: datetime, items: list[Announcement]) -> None:
    path = _cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"checked_at": now.isoformat(), "feed": _feed_from_items(items)}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), "utf-8")
    os.replace(tmp, path)


def check(
    *,
    settings: Settings,
    panel_version: str,
    force: bool = False,
    fetch: Fetcher | None = None,
    now: datetime | None = None,
) -> list[Announcement]:
    """Применимые сейчас оповещения. ``show_announcements=False`` — ``[]``, без сети.

    Как и ``updates.check`` — свой суточный кеш (весь разобранный фид, не
    список уже отфильтрованных), потому что фильтр по ``seen``/``now``
    обязан пересчитываться на каждый вызов даже если сам фид не обновлялся:
    иначе закрытая вчера плашка могла бы всплыть снова из кеша.
    """
    if not settings.show_announcements:
        return []

    now = now or datetime.now(timezone.utc)
    items: list[Announcement] | None = None
    if not force:
        items = _read_cache(now)
    if items is None:
        payload = fetch_json(feed_url(), fetch=fetch)
        items = parse_feed(payload)
        _write_cache(now, items)

    return applicable(items, panel_version=panel_version, seen=settings.seen_announcements, now=now)

"""Один суточный поход в сеть, обслуживающий и обновления, и оповещения.

``updates.check`` и ``announcements.check`` уже сами держат по своему
кешу-на-сутки и по своему тумблеру в настройках (см. их докстринги) —
поэтому вызывать обе функции безусловно при каждом открытии панели безопасно:
если тумблер выключен или кеш ещё свежий, они и так не пойдут в сеть.
``daily_refresh`` не заводит третий, отдельный таймер поверх этих двух — это
была бы третья точка, которую пришлось бы держать в согласии с первыми
двумя. Вместо этого он оборачивает переданный ``fetch`` счётчиком и
по нему же выставляет ``checked``: реально ли в ЭТОТ вызов ушёл хоть один
байт наружу, а не гадает по возрасту чужих файлов кеша.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Sequence

from . import announcements as announcements_mod
from . import updates as updates_mod
from .announcements import Announcement
from .network import DEFAULT_TIMEOUT, Fetcher, NetworkError, urlopen_fetch
from .settings import Settings
from .updates import Update

if TYPE_CHECKING:
    from .registry import AgentEntry


@dataclass(frozen=True)
class RefreshResult:
    updates: list[Update] = field(default_factory=list)
    announcements: list[Announcement] = field(default_factory=list)
    checked: bool = False


def daily_refresh(
    *,
    settings: Settings,
    panel_version: str,
    force: bool = False,
    fetch: Fetcher | None = None,
    entries: Sequence[AgentEntry] = (),
    now: datetime | None = None,
) -> RefreshResult:
    """Обновления + оповещения одним заходом. Никогда не бросает исключение.

    Оба тумблера (``check_updates``, ``show_announcements``) выключены — ни
    одного сетевого вызова: сами ``check()`` внутри решают это раньше, чем
    коснутся ``fetch``, здесь просто нечего считать. Сетевая ошибка на любом
    из двух шагов не выходит наружу — панель обязана открыться и работать
    без интернета; она просто останется без этой конкретной части (список
    обновлений/оповещений за неудавшийся шаг будет пустым в этом вызове,
    из кеша прошлого удачного захода их всё равно не вытащить синтетически,
    не соврав про их актуальность).
    """
    base_fetch = fetch or urlopen_fetch
    call_count = 0

    def counting_fetch(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> bytes:
        nonlocal call_count
        call_count += 1
        return base_fetch(url, timeout=timeout)

    found_updates: list[Update] = []
    try:
        # panel_version прокидывается явно: раз вызывающая сторона уже знает
        # текущую версию панели (она обязана передать её и для таргетинга
        # оповещений ниже), `updates.check` не должен ещё раз угадывать её
        # через importlib.metadata — это тот же самый факт, посчитанный дважды
        # разными путями с риском разойтись.
        found_updates = updates_mod.check(
            settings=settings,
            entries=entries,
            force=force,
            fetch=counting_fetch,
            now=now,
            panel_version=panel_version,
        )
    except NetworkError:
        pass

    found_announcements: list[Announcement] = []
    try:
        found_announcements = announcements_mod.check(
            settings=settings, panel_version=panel_version, force=force, fetch=counting_fetch, now=now
        )
    except NetworkError:
        pass

    return RefreshResult(updates=found_updates, announcements=found_announcements, checked=call_count > 0)

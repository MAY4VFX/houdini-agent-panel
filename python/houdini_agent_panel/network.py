"""Единственная дверь панели в сеть.

Всё, что ходит наружу — реестр, PyPI, фид оповещений, nodejs.org, архивы
агентов — обязано принимать параметр ``fetch`` этого типа. Причины две.

Первая: тест не должен зависеть от интернета, а мок одного протокола дешевле
патчинга ``urllib`` в шести модулях.

Вторая, важнее: в design.md записано обещание, что с выключенными оповещениями
и телеметрией панель не делает ни одного запроса. Обещание проверяемо только
если запросы физически идут через одну функцию, которую тест может посчитать.
"""

from __future__ import annotations

import os
import ssl
import urllib.error
import urllib.request
from typing import Callable, Protocol

#: Панель представляется честно: администратору студии, увидевшему это в логах
#: прокси, должно быть понятно, что за софт стучится наружу.
USER_AGENT = "houdini-agent-panel"

DEFAULT_TIMEOUT = 30.0

#: Своя связка корневых сертификатов для студий с перехватывающим прокси.
CA_BUNDLE_ENV = "HAP_CA_BUNDLE"

_ssl_context: ssl.SSLContext | None = None


def ssl_context() -> ssl.SSLContext:
    """Контекст TLS, который работает и внутри Houdini.

    Python, который приносит с собой Houdini, собран без связки корневых
    сертификатов: любой HTTPS оттуда падает с
    ``CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate``
    (проверено запуском в Houdini 22.0.368). А панель ходит в сеть именно
    оттуда — за реестром, агентами, Node, версиями и оповещениями. Без этого
    у неё не работает ни одна сетевая функция.

    Поэтому связку берём у ``certifi``: он и так приезжает вместе с
    зависимостями. Отключать проверку сертификатов — не вариант: мы по этим
    соединениям качаем исполняемые файлы.
    """
    global _ssl_context
    if _ssl_context is not None:
        return _ssl_context

    override = os.environ.get(CA_BUNDLE_ENV)
    if override and os.path.exists(override):
        _ssl_context = ssl.create_default_context(cafile=override)
        return _ssl_context

    try:
        import certifi

        _ssl_context = ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - вне Houdini системная связка обычно есть
        _ssl_context = ssl.create_default_context()
    return _ssl_context


class NetworkError(RuntimeError):
    """Что угодно, что помешало получить ответ. Причина — в тексте."""


class Fetcher(Protocol):
    def __call__(self, url: str, *, timeout: float = DEFAULT_TIMEOUT) -> bytes: ...


#: Колбэк прогресса для длинных скачиваний. ``total`` — None, если сервер не
#: прислал Content-Length.
Progress = Callable[[int, "int | None", str], None]


def urlopen_fetch(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> bytes:
    """Забрать URL целиком. Годится для JSON, не годится для архивов."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl_context()) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise NetworkError(f"{url}: HTTP {exc.code} {exc.reason}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise NetworkError(f"{url}: {exc}") from exc


def stream_fetch(
    url: str,
    destination,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    progress: Progress | None = None,
    chunk_size: int = 1 << 16,
) -> int:
    """Скачать URL в открытый бинарный файл, отдавая прогресс.

    Архивы агентов и Node — десятки мегабайт. Читать их в память целиком, чтобы
    потом записать, незачем, а прогресс-бар без потоковой загрузки нарисовать
    нечем. Возвращает число записанных байт.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=ssl_context()) as response:
            raw_length = response.headers.get("Content-Length")
            total = int(raw_length) if raw_length and raw_length.isdigit() else None
            done = 0
            while True:
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                destination.write(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, total, url.rsplit("/", 1)[-1])
            return done
    except urllib.error.HTTPError as exc:
        raise NetworkError(f"{url}: HTTP {exc.code} {exc.reason}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise NetworkError(f"{url}: {exc}") from exc


def fetch_json(url: str, *, fetch: Fetcher | None = None, timeout: float = DEFAULT_TIMEOUT):
    """Забрать и разобрать JSON. Мусор в ответе — тоже ``NetworkError``."""
    import json

    payload = (fetch or urlopen_fetch)(url, timeout=timeout)
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise NetworkError(f"{url}: ответ не разобрался как JSON: {exc}") from exc

"""Установка зависимостей панели в собственный Python самой Houdini.

Houdini 20.5 несёт Python 3.11, Houdini 22 — 3.13, и у каждого свой ABI для
`pydantic_core` (см. architecture.md §0) — поэтому зависимости ставятся не в
Python инсталлятора, а через `hython -m pip install --target` в дерево,
привязанное к версии Python конкретной Houdini.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from fxhoudinimcp.install import printable_argv

#: Таймаут на разовый запуск hython — только напечатать версию Python.
_VERSION_TIMEOUT = 30.0
#: Таймаут на pip install — колёса с бинарными расширениями бывают тяжёлыми.
_INSTALL_TIMEOUT = 600.0

#: Корни поиска, вынесены в переменные модуля ради тестируемости: юнит-тесты
#: подменяют их на директории в tmp_path вместо реальных `/Applications` и т.п.
_MAC_APPLICATIONS_ROOT = Path("/Applications/Houdini")
_LINUX_OPT_ROOT = Path("/opt")
_WINDOWS_PROGRAM_FILES = Path("C:/Program Files/Side Effects Software")


class DepsError(RuntimeError):
    """hython не запустился, не поставил зависимости или отдал непонятный вывод."""


def _system() -> str:
    if sys.platform == "darwin":
        return "darwin"
    if os.name == "nt":
        return "windows"
    return "linux"


def _version_key(path: Path) -> tuple[int, ...]:
    """Ключ сортировки по номеру сборки Houdini, зашитому в путь.

    "Houdini20.5.589" должен обыграть "Houdini20.5.445" — сравниваем как
    кортеж чисел, а не как строку (иначе "589" < "445" лексикографически ни при
    чём, но "20.5.9" < "20.5.10" сломалось бы).
    """
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", path.as_posix())
    if not match:
        return (0, 0, 0)
    return tuple(int(part) for part in match.groups())


def _hfs_hython(hfs: Path) -> Path:
    # Через _system(), а не напрямую os.name/sys.platform — так тесты могут
    # подменить одну функцию, не трогая os.name глобально (pathlib сам
    # смотрит на os.name, чтобы решить WindowsPath или PosixPath строить, и
    # его подмена на чужой ОС ломает создание Path прямо в тесте).
    name = "hython.exe" if _system() == "windows" else "hython"
    return hfs / "bin" / name


def find_hython(houdini_version: str) -> Path | None:
    """Найти `hython` нужной версии Houdini (например "20.5").

    `$HFS` уважается в первую очередь: если художник (или Houdini, из-под
    которой запущен сам инсталлятор) уже указал каталог установки явно, ему
    доверяем больше, чем угадыванию по стандартным путям. Если кандидатов
    несколько — берём самый свежий билд.
    """
    hfs = os.environ.get("HFS")
    if hfs:
        candidate = _hfs_hython(Path(hfs))
        if candidate.is_file():
            return candidate

    candidates = _candidate_hythons(houdini_version)
    if not candidates:
        return None
    return max(candidates, key=_version_key)


def _candidate_hythons(houdini_version: str) -> list[Path]:
    system = _system()

    if system == "darwin":
        root = _MAC_APPLICATIONS_ROOT
        pattern = (
            f"Houdini{houdini_version}.*/Frameworks/Houdini.framework/"
            f"Versions/{houdini_version}/Resources/bin/hython"
        )
    elif system == "windows":
        root = _WINDOWS_PROGRAM_FILES
        pattern = f"Houdini {houdini_version}*/bin/hython.exe"
    else:
        root = _LINUX_OPT_ROOT
        pattern = f"hfs{houdini_version}*/bin/hython"

    if not root.is_dir():
        return []
    return [p for p in root.glob(pattern) if p.is_file()]


def python_version_of(hython: Path, *, timeout: float = _VERSION_TIMEOUT) -> tuple[int, int] | None:
    """Версия Python внутри `hython`, например (3, 11).

    hython печатает в stderr предупреждение про setuptools при каждом старте —
    это не ошибка, поэтому смотрим только на stdout.
    """
    argv = [str(hython), "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DepsError(f"не удалось запустить {hython}: {exc}") from exc

    if result.returncode != 0:
        raise DepsError(
            f"{hython} завершился с кодом {result.returncode}: {result.stderr.strip()}"
        )

    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    match = re.fullmatch(r"(\d+)\.(\d+)", lines[-1])
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)))


def install_deps(
    hython: Path,
    *,
    target: Path,
    requirement: str,
    find_links: str | None = None,
    offline: bool = False,
    dry_run: bool = False,
    out=print,
) -> list[str]:
    """`hython -m pip install --upgrade --target <target> <requirement>`.

    `--upgrade`, потому что повторная установка новой версии панели поверх
    старого дерева deps — обычный сценарий (обновление панели), а не разовая
    установка.

    `find_links` и `offline` разведены сознательно, хотя изначально это был
    один флаг. «Возьми колесо панели из этой папки» и «не ходи в интернет
    вообще» — разные намерения, и склеивание их ломало главный сценарий
    разработки: собрал колесо локально, ставишь его, а зависимости (`acp`,
    `pydantic`) взять неоткуда, потому что `--no-index` закрыл и их тоже.
    """
    argv: list[str] = [
        str(hython), "-m", "pip", "install", "--upgrade", "--target", str(target), requirement,
    ]
    if find_links:
        argv += ["--find-links", find_links]
    if offline:
        argv.append("--no-index")

    if dry_run:
        out(f"[dry-run] {printable_argv(argv)}")
        return []

    target.mkdir(parents=True, exist_ok=True)
    out(f"Ставлю зависимости: {printable_argv(argv)}")
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=_INSTALL_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DepsError(f"pip install не запустился: {exc}") from exc

    lines = [line for line in result.stdout.splitlines() if line]
    for line in lines:
        out(line)

    if result.returncode != 0:
        for line in result.stderr.splitlines():
            out(line)
        raise DepsError(f"pip install завершился с кодом {result.returncode}")

    return lines


def deps_ready(target: Path) -> bool:
    """Похоже ли дерево `target` на уже поставленные зависимости панели.

    Проверяем два маркера: `acp` (ACP SDK, `agent-client-protocol`) и сам
    `houdini_agent_panel` — оба обязаны появиться после успешного `pip
    install --target`, и их отсутствие — надёжный признак прерванной
    установки.
    """
    return (target / "acp").is_dir() and (target / "houdini_agent_panel").is_dir()


__all__: Sequence[str] = [
    "DepsError",
    "find_hython",
    "python_version_of",
    "install_deps",
    "deps_ready",
]

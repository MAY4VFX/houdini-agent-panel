"""Портативный Node.js: найти системный или скачать свой.

4 из 6 агентов v1 (см. design.md) ставятся через npx, поэтому Node обязателен.
Систему никогда не трогаем — либо используем то, что уже стоит и достаточно
свежее, либо качаем официальный архив с nodejs.org в `paths.node_dir()`.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from . import paths
from . import runtime
from .network import Fetcher, urlopen_fetch
from .runtime import ChecksumError, InstallError, Progress

#: Ниже этой версии считаем системный Node непригодным (слишком старый npx).
MIN_NODE = (20, 0, 0)
#: Что качаем, если системного нет или он слишком стар.
NODE_VERSION = "22.14.0"

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


def find_system_node(minimum: tuple[int, int, int] = MIN_NODE) -> Path | None:
    """Системный `node`, если он есть в PATH и не старше `minimum`.

    Мусор в выводе `node --version` (не тот бинарь, повреждённая установка) —
    трактуем как "системного нет", а не падаем: панель не обязана понимать,
    что именно пошло не так с чужим Node на диске у человека.
    """
    found = shutil.which("node")
    if not found:
        return None
    path = Path(found)
    try:
        result = subprocess.run(
            [str(path), "--version"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    version = _parse_version(result.stdout)
    if version is None or version < minimum:
        return None
    return path


def _parse_version(text: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.match(text.strip())
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def node_platform() -> tuple[str, str]:
    """("darwin", "arm64") — имена, как их использует nodejs.org в архивах."""
    system = platform.system()
    os_name = {"Darwin": "darwin", "Linux": "linux", "Windows": "win"}.get(system)
    if os_name is None:
        raise InstallError(f"неизвестная платформа: {system!r}")
    machine = platform.machine().lower()
    arch = {
        "arm64": "arm64",
        "aarch64": "arm64",
        "x86_64": "x64",
        "amd64": "x64",
    }.get(machine, machine)
    return os_name, arch


def dist_url(version: str = NODE_VERSION) -> str:
    os_name, arch = node_platform()
    ext = "zip" if os_name == "win" else "tar.gz"
    return f"https://nodejs.org/dist/v{version}/node-v{version}-{os_name}-{arch}.{ext}"


def shasums_url(version: str = NODE_VERSION) -> str:
    return f"https://nodejs.org/dist/v{version}/SHASUMS256.txt"


def _find_sha256(shasums_text: str, archive_name: str) -> str | None:
    """SHASUMS256.txt — строки вида `<hex-sha256>  <filename>`."""
    for line in shasums_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == archive_name:
            return parts[0]
    return None


def _node_bin_path(root: Path, os_name: str | None = None) -> Path:
    """Путь к бинарю node внутри распакованного архива nodejs.org."""
    os_name = os_name or node_platform()[0]
    if os_name == "win":
        return root / "node.exe"
    return root / "bin" / "node"


def install_node(
    *, version: str = NODE_VERSION, progress: Progress | None = None, fetch: Fetcher | None = None
) -> Path:
    """Скачать архив с nodejs.org, сверить по SHASUMS256.txt, распаковать.

    Идемпотентно: версия уже стоит в `paths.node_dir()/<version>` — сеть не
    трогаем вовсе. Систему не трогаем никогда — ставим только в свой каталог.
    """
    target_dir = paths.node_dir() / version
    node_bin = _node_bin_path(target_dir)
    if node_bin.exists():
        return node_bin

    fetch_impl = fetch or urlopen_fetch
    archive_url = dist_url(version)
    archive_name = archive_url.rsplit("/", 1)[-1]

    shasums_text = fetch_impl(shasums_url(version)).decode("utf-8")
    sha256 = _find_sha256(shasums_text, archive_name)
    if sha256 is None:
        raise ChecksumError(f"{archive_name}: нет записи в SHASUMS256.txt")

    node_root = paths.node_dir()
    with tempfile.TemporaryDirectory(dir=node_root) as tmp_name:
        tmp_dir = Path(tmp_name)
        archive_path = tmp_dir / archive_name
        runtime.download_and_verify(archive_url, sha256, archive_path, progress=progress, fetch=fetch)

        extract_root = tmp_dir / "extracted"
        extract_root.mkdir()
        runtime.extract_archive(archive_path, extract_root)

        roots = list(extract_root.iterdir())
        if len(roots) != 1 or not roots[0].is_dir():
            raise InstallError(f"{archive_name}: неожиданное содержимое архива")

        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(roots[0]), str(target_dir))

    result = _node_bin_path(target_dir)
    if not result.exists():
        raise InstallError(f"после распаковки не найден бинарь node в {target_dir}")
    return result


def ensure_node(*, progress: Progress | None = None, fetch: Fetcher | None = None) -> Path:
    """Системный Node, если он годится; иначе — свой в `paths.node_dir()`.

    `fetch` не было в исходном контракте архитектуры (`docs/architecture.md`
    §5 перечисляет `ensure_node(*, progress=None) -> Path`), но без него
    `install_agent` не смог бы прокинуть `FakeFetcher` теста через
    `ensure_node -> install_node -> network`. Отступление сделано и здесь, и
    во всех вызывающих (`runtime.install_agent`, `runtime.launch_spec`).
    """
    system_node = find_system_node()
    if system_node is not None:
        return system_node
    return install_node(progress=progress, fetch=fetch)


def npx_argv(node_bin: Path, package: str, args: Sequence[str]) -> list[str]:
    """`[<node>, <npx-cli.js>, "--yes", package, *args]`.

    Зовём `npx-cli.js` напрямую нашим же `node`, а не шелловый шим `npx`: шим
    ищет `node` в PATH, а окружение агента у нас почти пустое
    (`docs/facts/acp-sdk.md` — `default_environment()`), в нём PATH до нашего
    Node может не быть вовсе.
    """
    npx_cli = _npx_cli_path(node_bin)
    return [str(node_bin), str(npx_cli), "--yes", package, *args]


class NpxNotFoundError(RuntimeError):
    """Рядом с этим Node нет npm. Явная ошибка вместо пути в никуда."""


def npx_cli_candidates(node_bin: Path) -> list[Path]:
    """Где может лежать `npx-cli.js` относительно данного `node`.

    Раньше здесь была одна догадка с `resolve()`, и она разваливалась ровно на
    самом типичном случае — Homebrew. `/opt/homebrew/bin/node` это symlink в
    `Cellar/node/<версия>/bin/node`, но npm Homebrew кладёт НЕ туда, а в
    `/opt/homebrew/lib/node_modules`. То есть `resolve()` уводил в дерево, где
    npm нет вовсе, и мы возвращали несуществующий путь как ни в чём не бывало.

    Поэтому теперь это список: и по симлинку, и по реальному пути, и обе
    раскладки — POSIX (`bin/../lib/node_modules`) и Windows
    (`node_modules` рядом с `node.exe`). Проверять существование обязан
    вызывающий.
    """
    candidates: list[Path] = []
    for base in (node_bin, node_bin.resolve()):
        parent = base.parent
        # Windows: node.exe и node_modules лежат в одном каталоге.
        candidates.append(parent / "node_modules" / "npm" / "bin" / "npx-cli.js")
        # POSIX: <root>/bin/node и <root>/lib/node_modules.
        candidates.append(parent.parent / "lib" / "node_modules" / "npm" / "bin" / "npx-cli.js")

    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def _npx_cli_path(node_bin: Path) -> Path:
    """Первый существующий кандидат.

    Не нашлось ничего — падаем с внятным текстом прямо здесь. Вернуть
    несуществующий путь означало бы запустить `node <нет-такого-файла>`:
    процесс умирает мгновенно и молча, а панель остаётся ждать приветствия от
    покойника. Именно так это и выглядело у художника — «Запускаю…» навсегда.
    """
    for candidate in npx_cli_candidates(node_bin):
        if candidate.is_file():
            return candidate
    raise NpxNotFoundError(
        f"рядом с {node_bin} нет npm (искал npx-cli.js в: "
        + ", ".join(str(c) for c in npx_cli_candidates(node_bin))
        + ")"
    )


def path_with_node(node_bin: Path, base: str | None = None) -> str:
    """PATH, в начале которого лежит каталог с нашим `node`.

    Нужно не нам, а самому npm: `npx-cli.js` порождает дочерние процессы
    командой `node` и ищет её в PATH. Без этого агент на машине без Node
    умирает до первого байта, а клиент видит только «соединение закрыто» —
    диагностировать это со стороны панели практически нечем.

    Дописываем в начало к тому PATH, который уже есть, а не заменяем его:
    агенту могут понадобиться и другие инструменты с машины, и отбирать их
    у него мы не собираемся.
    """
    node_dir = str(node_bin.parent)
    existing = base if base is not None else os.environ.get("PATH", "")
    parts = [node_dir] + [part for part in existing.split(os.pathsep) if part and part != node_dir]
    return os.pathsep.join(parts)

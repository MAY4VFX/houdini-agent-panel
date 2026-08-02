"""Установка, запуск и удаление агентов: скачать, проверить sha256, распаковать.

Самый рискованный код в проекте по одной причине — он пишет на диск то, что
пришло из интернета. Два правила держат это безопасным: контрольная сумма
проверяется ДО того, как что-либо занимает постоянное место (`download_and_verify`),
и распаковка отклоняет любой путь внутри архива, целящий наружу целевой
директории (`extract_archive`, Zip Slip).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from . import paths
from .network import Fetcher, stream_fetch
from .registry import AgentEntry, BinaryDistribution, NpxDistribution, platform_key
from .settings import CustomAgent

_MANIFEST_NAME = "manifest.json"


class Progress(Protocol):
    def __call__(self, done: int, total: int | None, note: str) -> None: ...


@dataclass(frozen=True)
class LaunchSpec:
    command: str
    args: list[str]
    env: dict[str, str]  # добавка к окружению процесса, не замена


class InstallError(RuntimeError):
    """Что угодно, что помешало поставить агента или Node. Причина — в тексте."""


class ChecksumError(InstallError):
    """sha256 скачанного не совпал с ожидаемым.

    `download_and_verify` в этом случае не оставляет на диске ничего — ни
    промежуточного файла, ни тем более итогового.
    """


# --- манифест на диске: чем `is_installed`/`installed_version` проверяют,
# что уже стоит, не трогая ни сеть, ни глобальные настройки панели -----------


def _manifest_path_readonly(agent_id: str) -> Path:
    """Путь к манифесту для ЧТЕНИЯ — без побочного создания папки агента.

    `paths.agent_dir()` делает `mkdir` при каждом обращении (`paths._sub`).
    `is_installed`/`installed_version` дергаются для КАЖДОГО агента реестра
    при отрисовке экрана "Агенты" — через `paths.agent_dir()` это заводило бы
    пустую папку на диске для каждого ещё не установленного агента.
    """
    return paths.agents_dir() / agent_id / _MANIFEST_NAME


def _write_manifest(entry: AgentEntry, *, kind: str) -> None:
    payload = {
        "agent_id": entry.id,
        "version": entry.version,
        "kind": kind,
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    manifest = paths.agent_dir(entry.id) / _MANIFEST_NAME  # тут mkdir нужен по делу — мы пишем
    manifest.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")


def installed_version(agent_id: str) -> str | None:
    manifest = _manifest_path_readonly(agent_id)
    if not manifest.exists():
        return None
    try:
        data = json.loads(manifest.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    version = data.get("version")
    return str(version) if version else None


def is_installed(entry: AgentEntry) -> bool:
    """Идемпотентность установки: та же версия уже стоит — не качаем заново."""
    return installed_version(entry.id) == entry.version


# --- скачивание с проверкой sha256 ------------------------------------------


class _HashingWriter:
    """Прокси файлового объекта: пишет на диск и считает sha256 по потоку.

    Так `download_and_verify` не читает архив дважды (один раз для записи,
    второй для хеша) — sha256 копится по мере поступления чанков от
    `network.stream_fetch`.
    """

    def __init__(self, raw, hasher: "hashlib._Hash") -> None:
        self._raw = raw
        self._hasher = hasher

    def write(self, chunk: bytes) -> int:
        self._hasher.update(chunk)
        return self._raw.write(chunk)


def _stream_to_file(url: str, dest_file, *, fetch: Fetcher | None, progress: Progress | None) -> int:
    """Скачать `url` в открытый файловый объект `dest_file`.

    С переданным `fetch` (тесты) — тело целиком через `Fetcher`, это ОК для
    маленьких тестовых фикстур и, важнее, единственный способ, которым тест
    вообще может подменить сеть (`network.py`: "мок одного протокола дешевле
    патчинга urllib в шести модулях" — это верно и для потоковых архивов).
    Без `fetch` (продакшен) — настоящий `network.stream_fetch`, чтобы не
    таскать десятки мегабайт архива агента/Node в память целиком.
    """
    if fetch is not None:
        payload = fetch(url)
        dest_file.write(payload)
        if progress is not None:
            progress(len(payload), len(payload), url.rsplit("/", 1)[-1])
        return len(payload)
    return stream_fetch(url, dest_file, progress=progress)


def download_and_verify(
    url: str,
    sha256: str,
    dest: Path,
    *,
    progress: Progress | None = None,
    fetch: Fetcher | None = None,
) -> Path:
    """Скачать `url` в `dest`, проверив sha256, атомарно.

    Пишем во временный файл РЯДОМ с `dest` (`<dest>.part`) и переименовываем
    на место только после совпадения контрольной суммы. Прерванная закачка
    или несовпавший хеш не оставляют на диске ни `dest`, ни `.part`-файла —
    иначе панель считала бы половину архива "установленным" агентом.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    hasher = hashlib.sha256()
    try:
        with tmp.open("wb") as raw:
            writer = _HashingWriter(raw, hasher)
            _stream_to_file(url, writer, fetch=fetch, progress=progress)
        digest = hasher.hexdigest()
        if digest.lower() != sha256.lower():
            raise ChecksumError(f"{url}: sha256 {digest} не совпал с ожидаемым {sha256}")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(dest)
    return dest


# --- безопасная распаковка (Zip Slip) ---------------------------------------


def _safe_member_path(dest: Path, member_name: str) -> Path:
    """Резолвит путь члена архива внутри `dest`, отклоняя выход за границу.

    `Path.joinpath` сам "телепортирует" результат на абсолютный путь, если
    один из компонентов абсолютный (`Path("/a") / "/etc/passwd" == Path("/etc/passwd")`) —
    поэтому проверка "результат лежит внутри dest" после `resolve()` ловит
    и `..`-обход, и абсолютные пути членов одним и тем же кодом.
    """
    member_path = (dest / member_name).resolve()
    if member_path != dest and dest not in member_path.parents:
        raise InstallError(f"архив содержит небезопасный путь: {member_name!r}")
    return member_path


def _apply_zip_permissions(path: Path, info: "zipfile.ZipInfo") -> None:
    mode = (info.external_attr >> 16) & 0o777
    if mode:
        try:
            path.chmod(mode)
        except OSError:
            pass


def _extract_zip(archive: Path, dest: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            if info.filename.endswith("/"):
                _safe_member_path(dest, info.filename).mkdir(parents=True, exist_ok=True)
                continue
            target = _safe_member_path(dest, info.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
            _apply_zip_permissions(target, info)


def _extract_tar(archive: Path, dest: Path) -> None:
    with tarfile.open(archive, "r:*") as tf:
        for member in tf.getmembers():
            if member.issym() or member.islnk():
                # Симлинки внутри архива не создаём и не следуем по ним — самый
                # простой способ закрыть Zip Slip через ссылки: цель снаружи
                # dest никогда не появляется на диске. Наши агенты/Node этого
                # не требуют: npx_argv() зовёт npx-cli.js напрямую, минуя
                # симлинк-шимы bin/npx, которые несёт архив nodejs.org.
                continue
            target = _safe_member_path(dest, member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue  # устройства/fifo из архива нам не нужны
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tf.extractfile(member)
            if extracted is None:
                continue
            with extracted as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
            if member.mode:
                try:
                    target.chmod(member.mode)
                except OSError:
                    pass


def extract_archive(archive: Path, dest: Path) -> None:
    """tar.gz/tgz/zip. Пути с `..` и абсолютные — отклоняются (Zip Slip)."""
    archive = Path(archive)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith(".zip"):
        _extract_zip(archive, dest)
    elif name.endswith((".tar.gz", ".tgz", ".tar")):
        _extract_tar(archive, dest)
    else:
        raise InstallError(f"{archive}: неизвестный формат архива")


# --- установка/удаление/запуск агентов --------------------------------------


def _resolve_cmd(root: Path, cmd: str) -> Path:
    """`"./opencode"` — относительно корня распакованного архива."""
    cleaned = cmd[2:] if cmd.startswith("./") else cmd
    cleaned = cleaned.replace("\\", "/")  # часть записей реестра — под Windows
    return root / cleaned


def _make_executable(path: Path) -> None:
    if sys.platform == "win32":
        return
    try:
        mode = path.stat().st_mode
        path.chmod(mode | 0o111)
    except OSError:
        pass


def _npx_launch_spec(node_bin: Path, dist: NpxDistribution) -> LaunchSpec:
    from . import node as node_module

    args = node_module.npx_argv(node_bin, dist.package, dist.args)
    return LaunchSpec(command=args[0], args=args[1:], env=dict(dist.env))


def _binary_launch_spec(version_dir: Path, dist: BinaryDistribution) -> LaunchSpec:
    return LaunchSpec(command=str(_resolve_cmd(version_dir, dist.cmd)), args=list(dist.args), env={})


def install_agent(
    entry: AgentEntry, *, progress: Progress | None = None, fetch: Fetcher | None = None
) -> LaunchSpec:
    """Поставить агента и вернуть готовую команду запуска.

    Идемпотентно: версия из `entry` уже стоит — ни сети, ни диска, сразу
    `launch_spec`. npx-агент — только `ensure_node()` и манифест, сам пакет
    качает `npx` при первом запуске. Бинарный — качает архив, сверяет
    sha256, распаковывает в `<data>/agents/<id>/<version>`, ставит +x.
    """
    if is_installed(entry):
        return launch_spec(entry)

    key = platform_key()
    dist = entry.distribution_for(key)
    if dist is None:
        # Ключ передаём явно тем же вызовом platform_key(), что и выше: свой
        # (не registry-модульный) platform_key переопределён в тестах через
        # runtime.platform_key, и unavailable_reason() обязана согласиться с
        # тем же значением, а не читать реальную платформу заново.
        raise InstallError(entry.unavailable_reason(key))

    if isinstance(dist, NpxDistribution):
        from . import node as node_module

        node_bin = node_module.ensure_node(progress=progress, fetch=fetch)
        _write_manifest(entry, kind="npx")
        return _npx_launch_spec(node_bin, dist)

    if not dist.sha256:
        # В реестре встречаются записи без sha256 (§ registry.py,
        # BinaryDistribution.sha256). Ставить бинарь, который нечем
        # проверить, панель отказывается — лучше явная ошибка, чем тихая
        # брешь в целостности того, что запускается в системе художника.
        raise InstallError(f"{entry.name}: в реестре нет sha256 для проверки, установка отклонена")

    version_dir = paths.agent_dir(entry.id) / entry.version
    archive_name = dist.archive.rsplit("/", 1)[-1]
    agents_root = paths.agents_dir()
    with tempfile.TemporaryDirectory(dir=agents_root) as tmp_name:
        tmp_dir = Path(tmp_name)
        archive_path = tmp_dir / archive_name
        download_and_verify(dist.archive, dist.sha256, archive_path, progress=progress, fetch=fetch)

        extract_root = tmp_dir / "extracted"
        extract_root.mkdir()
        extract_archive(archive_path, extract_root)

        if version_dir.exists():
            shutil.rmtree(version_dir)
        version_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(extract_root), str(version_dir))

    cmd_path = _resolve_cmd(version_dir, dist.cmd)
    _make_executable(cmd_path)
    _write_manifest(entry, kind="binary")
    return _binary_launch_spec(version_dir, dist)


def uninstall_agent(agent_id: str) -> None:
    """Чистит папку агента целиком. Соседей (других агентов) не трогает.

    Путь считаем через `paths.agents_dir() / agent_id`, а не через
    `paths.agent_dir(agent_id)`: последняя создаёт директорию как побочный
    эффект (`paths._sub` делает `mkdir` при каждом обращении) — уничтожать то,
    что сама же перед этим создала, было бы странно и для несуществовавшего
    агента оставляло бы пустую папку на диске.
    """
    directory = paths.agents_dir() / agent_id
    if directory.exists():
        shutil.rmtree(directory)


def launch_spec(entry: AgentEntry) -> LaunchSpec:
    """Команда запуска уже установленного агента.

    Для npx `ensure_node()` здесь дешёвый: агент устанавливался через
    `install_agent`, который уже развернул Node (системный либо свой) — это
    просто находит его снова, без сети, если он уже на диске.
    """
    key = platform_key()
    dist = entry.distribution_for(key)
    if dist is None:
        # Ключ передаём явно тем же вызовом platform_key(), что и выше: свой
        # (не registry-модульный) platform_key переопределён в тестах через
        # runtime.platform_key, и unavailable_reason() обязана согласиться с
        # тем же значением, а не читать реальную платформу заново.
        raise InstallError(entry.unavailable_reason(key))

    if isinstance(dist, NpxDistribution):
        from . import node as node_module

        node_bin = node_module.ensure_node()
        return _npx_launch_spec(node_bin, dist)

    if not is_installed(entry):
        raise InstallError(f"{entry.name} {entry.version}: не установлен")
    version_dir = paths.agent_dir(entry.id) / entry.version
    return _binary_launch_spec(version_dir, dist)


def custom_launch_spec(agent: CustomAgent) -> LaunchSpec:
    """«Свой агент» — команда как есть, без установки и версий."""
    return LaunchSpec(command=agent.command, args=list(agent.args), env=dict(agent.env))

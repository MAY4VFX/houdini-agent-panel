"""Реестр ACP-агентов: разбор, кеш, выбор дистрибутива под платформу.

Источник — публичный JSON без каких-либо гарантий совместимости со стороны
houdini-agent-panel: чужой проект может добавить поле, убрать необязательное
или прислать значение не того типа. `parse_registry` обязана пережить любую
из этих ситуаций, уронив только конкретную битую запись, а не весь реестр.
"""

from __future__ import annotations

import json
import os
import platform
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import paths
from .network import Fetcher, NetworkError, fetch_json

REGISTRY_URL = "https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json"

#: Шестёрка из design.md — ровно то, что панель предлагает. Порядок — порядок
#: показа в UI.
#:
#: Это НЕ «всё, что есть в реестре»: там под сорок записей, и вываливать их
#: художнику значит подменить выбор списком, в котором он не разбирается и не
#: обязан. Всё, чего здесь нет, ставится через «Свой агент» — это и есть ответ
#: дизайна на «остальное».
#:
#: Идентификаторы сверены с живым реестром (version 1.0.0) и не совпадают с
#: тем, как агенты называются для людей: "Claude Agent" лежит под "claude-acp",
#: "Gemini CLI" под "gemini", "Kimi CLI" под "kimi". Угадать их по памяти
#: нельзя — только "codex-acp", "grok-build" и "opencode" очевидны.
FEATURED_AGENT_IDS: tuple[str, ...] = (
    "claude-acp",
    "codex-acp",
    "grok-build",
    "opencode",
    "gemini",
    "kimi",
)


def featured(entries: "Sequence[AgentEntry]") -> "list[AgentEntry]":
    """Отобрать и упорядочить агентов v1.

    Порядок берётся из ``FEATURED_AGENT_IDS``, а не из реестра: реестр
    отсортирован по идентификатору, и для человека этот порядок не значит
    ничего. Запись, которой в реестре не оказалось (переименовали, убрали),
    просто пропускается — панель не должна показывать пустую строку с именем,
    которое ей неоткуда взять.
    """
    order = {agent_id: index for index, agent_id in enumerate(FEATURED_AGENT_IDS)}
    chosen = [entry for entry in entries if entry.id in order]
    chosen.sort(key=lambda entry: order[entry.id])
    return chosen


_CACHE_FILE_NAME = "registry.json"


@dataclass(frozen=True)
class NpxDistribution:
    package: str
    args: list[str] = field(default_factory=list)
    #: Не было в исходном контракте архитектуры, но реальный реестр его несёт
    #: (например у "auggie" — `AUGMENT_DISABLE_AUTO_UPDATE=1`), а без него
    #: `install_agent` не сможет собрать корректный `LaunchSpec.env` для тех
    #: агентов, которым это нужно. Отступление от architecture.md §3.
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class BinaryDistribution:
    archive: str
    cmd: str
    args: list[str] = field(default_factory=list)
    #: В контракте — обязательное str. В реальном реестре у части агентов
    #: (`crow-cli`, `corust-agent`) поля нет вовсе. Делаем необязательным с
    #: пустой строкой по умолчанию и трактуем пустое значение как "проверить
    #: нечем" — `runtime.install_agent` в этом случае отказывает в установке,
    #: а не устанавливает непроверенный бинарь. Отступление от architecture.md §3.
    sha256: str = ""


@dataclass(frozen=True)
class AgentEntry:
    id: str
    name: str
    version: str
    description: str = ""
    repository: str = ""
    website: str = ""
    license: str = ""
    icon: str = ""
    authors: tuple[str, ...] = ()
    npx: NpxDistribution | None = None
    binaries: Mapping[str, BinaryDistribution] = field(default_factory=dict)

    @property
    def needs_node(self) -> bool:
        return self.npx is not None

    def distribution_for(self, key: str | None = None) -> NpxDistribution | BinaryDistribution | None:
        """None — агента нельзя поставить на эту платформу.

        Например Kimi CLI не собирается под `darwin-x86_64` (design.md). UI
        обязан показать это причиной (см. `unavailable_reason`), а не молча
        спрятать кнопку установки.
        """
        if self.npx is not None:
            return self.npx
        return self.binaries.get(key or platform_key())

    def unavailable_reason(self, key: str | None = None) -> str:
        """Человекочитаемая причина отсутствия дистрибутива под `key`.

        Пустая строка означает "доступен" — вызывающему нет смысла её
        показывать. Непустая — то, что UI обязан вывести человеку вместо
        того, чтобы просто не рисовать кнопку установки.
        """
        resolved_key = key or platform_key()
        if self.distribution_for(resolved_key) is not None:
            return ""
        if self.npx is None and not self.binaries:
            return f"{self.name}: в реестре нет способа установки"
        return f"{self.name} не собирается под {resolved_key}"


class RegistryError(RuntimeError):
    """Реестр недоступен: ни сети, ни пригодного кеша."""


def platform_key() -> str:
    """darwin-aarch64 | darwin-x86_64 | linux-aarch64 | linux-x86_64 | windows-x86_64

    Ровно те пять ключей, что встречаются в качестве строений `distribution.binary`
    в живом реестре (там же попадается ещё и `windows-aarch64` у части агентов,
    но Houdini под Windows ARM не бывает, так что этот ключ мы никогда не просим).
    """
    system = platform.system()
    machine = platform.machine().lower()
    arch = "aarch64" if machine in ("arm64", "aarch64") else "x86_64"
    if system == "Darwin":
        return f"darwin-{arch}"
    if system == "Linux":
        return f"linux-{arch}"
    if system == "Windows":
        return "windows-x86_64"
    raise RegistryError(f"неизвестная платформа: {system!r}")


def parse_registry(payload: Mapping) -> list[AgentEntry]:
    """Разобрать тело `registry.json`.

    Реестр — чужой JSON без версионной гарантии на наши ожидания. Битая
    запись (не тот тип, отсутствующее обязательное поле) пропускается —
    остальные агенты обязаны разобраться, иначе одна опечатка стороннего
    майнтейнера кладёт весь экран "Агенты" панели.
    """
    if not isinstance(payload, Mapping):
        return []
    raw_agents = payload.get("agents")
    if not isinstance(raw_agents, list):
        return []

    entries: list[AgentEntry] = []
    for raw in raw_agents:
        entry = _parse_entry(raw)
        if entry is not None:
            entries.append(entry)
    return entries


def _parse_entry(raw: Any) -> AgentEntry | None:
    if not isinstance(raw, Mapping):
        return None
    agent_id = raw.get("id")
    name = raw.get("name")
    version = raw.get("version")
    if not isinstance(agent_id, str) or not agent_id:
        return None
    if not isinstance(name, str) or not name:
        return None
    # version в схеме — строка, но не рискуем ронять запись из-за числа-жабы;
    # берём str() от чего угодно сериализуемого, кроме None.
    if version is None:
        return None
    version = str(version)

    authors_raw = raw.get("authors")
    authors = tuple(str(a) for a in authors_raw) if isinstance(authors_raw, list) else ()

    distribution = raw.get("distribution")
    npx = _parse_npx(distribution) if isinstance(distribution, Mapping) else None
    binaries = _parse_binaries(distribution) if isinstance(distribution, Mapping) else {}

    def _str(key: str) -> str:
        value = raw.get(key)
        return value if isinstance(value, str) else ""

    return AgentEntry(
        id=agent_id,
        name=name,
        version=version,
        description=_str("description"),
        repository=_str("repository"),
        website=_str("website"),
        license=_str("license"),
        icon=_str("icon"),
        authors=authors,
        npx=npx,
        binaries=binaries,
    )


def _parse_npx(distribution: Mapping) -> NpxDistribution | None:
    raw = distribution.get("npx")
    if not isinstance(raw, Mapping):
        return None
    package = raw.get("package")
    if not isinstance(package, str) or not package:
        return None
    args_raw = raw.get("args")
    args = [str(a) for a in args_raw] if isinstance(args_raw, list) else []
    env_raw = raw.get("env")
    env = {str(k): str(v) for k, v in env_raw.items()} if isinstance(env_raw, Mapping) else {}
    return NpxDistribution(package=package, args=args, env=env)


def _parse_binaries(distribution: Mapping) -> dict[str, BinaryDistribution]:
    raw = distribution.get("binary")
    if not isinstance(raw, Mapping):
        return {}
    binaries: dict[str, BinaryDistribution] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, Mapping):
            continue
        archive = value.get("archive")
        cmd = value.get("cmd")
        if not isinstance(archive, str) or not archive:
            continue
        if not isinstance(cmd, str) or not cmd:
            continue
        args_raw = value.get("args")
        args = [str(a) for a in args_raw] if isinstance(args_raw, list) else []
        sha256 = value.get("sha256")
        binaries[key] = BinaryDistribution(
            archive=archive,
            cmd=cmd,
            args=args,
            sha256=sha256 if isinstance(sha256, str) else "",
        )
    return binaries


# --- кеш на диске -----------------------------------------------------------


def _cache_path() -> Path:
    return paths.cache_dir() / _CACHE_FILE_NAME


def _read_cache(path: Path, *, max_age: float | None) -> list[AgentEntry] | None:
    """`max_age=None` — взять кеш любого возраста (сеть недоступна, кеш есть)."""
    if not path.exists():
        return None
    try:
        wrapper = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(wrapper, dict):
        return None
    payload = wrapper.get("payload")
    if not isinstance(payload, dict):
        return None
    if max_age is not None:
        fetched_at = wrapper.get("fetched_at")
        if not isinstance(fetched_at, (int, float)):
            return None
        if time.time() - fetched_at > max_age:
            return None
    return parse_registry(payload)


def _write_cache(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    wrapper = {"fetched_at": time.time(), "payload": payload}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(wrapper), "utf-8")
    os.replace(tmp, path)


def fetch_registry(
    *, force: bool = False, max_age: float = 86400.0, fetch: Fetcher | None = None
) -> list[AgentEntry]:
    """Реестр агентов, кеш в `<cache>/registry.json`.

    Кеш свежее `max_age` и `force=False` — отдаём его, не ходя в сеть. Сеть
    недоступна (в том числе `force=True`, но запрос не удался) — отдаём кеш
    ЛЮБОГО возраста, не притворяясь, что данные свежие: экран "Агенты" лучше
    показать со старыми версиями, чем не показать вовсе. Ни кеша, ни сети —
    `RegistryError`.
    """
    cache_file = _cache_path()
    if not force:
        cached = _read_cache(cache_file, max_age=max_age)
        if cached is not None:
            return cached

    try:
        payload = fetch_json(REGISTRY_URL, fetch=fetch)
    except NetworkError:
        stale = _read_cache(cache_file, max_age=None)
        if stale is not None:
            return stale
        raise RegistryError(
            f"{REGISTRY_URL}: сеть недоступна, а локального кеша ещё нет"
        ) from None

    entries = parse_registry(payload)
    _write_cache(cache_file, payload)
    return entries

"""ACP agent registry: parsing, cache, picking the right distribution for the platform.

The source is public JSON with no compatibility guarantee toward
houdini-agent-panel whatsoever: someone else's project may add a field,
remove an optional one, or send a value of the wrong type. `parse_registry`
must survive any of these, dropping only the one broken entry rather than
the whole registry.
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

#: The six from design.md — exactly what the panel offers. The order is the
#: order they're shown in the UI.
#:
#: This is NOT "everything in the registry": there are close to forty
#: entries there, and dumping them on an artist would replace a curated
#: choice with a list they don't know and shouldn't have to know. Anything
#: not here can be installed via "Custom Agent" — that's the design's answer
#: to "everything else".
#:
#: Ids were checked against the live registry (version 1.0.0) and don't
#: match the human-facing agent names: "Claude Agent" is under "claude-acp",
#: "Gemini CLI" under "gemini", "Kimi CLI" under "kimi". They can't be
#: guessed from memory — only "codex-acp", "grok-build" and "opencode" are
#: obvious.
FEATURED_AGENT_IDS: tuple[str, ...] = (
    "claude-acp",
    "codex-acp",
    "grok-build",
    "opencode",
    "gemini",
    "kimi",
)


def featured(entries: "Sequence[AgentEntry]") -> "list[AgentEntry]":
    """Select and order the v1 agents.

    The order comes from ``FEATURED_AGENT_IDS``, not from the registry: the
    registry is sorted by id, and that order means nothing to a human. An
    entry missing from the registry (renamed, removed) is simply skipped —
    the panel shouldn't show a blank row with a name it has nowhere to get.
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
    #: Not in the original architecture contract, but the real registry
    #: carries it (e.g. "auggie" has `AUGMENT_DISABLE_AUTO_UPDATE=1`), and
    #: without it `install_agent` couldn't build a correct `LaunchSpec.env`
    #: for the agents that need it. A deviation from architecture.md §3.
    env: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class BinaryDistribution:
    archive: str
    cmd: str
    args: list[str] = field(default_factory=list)
    #: The contract says this is a required str. In the real registry, some
    #: agents (`crow-cli`, `corust-agent`) don't have this field at all. We
    #: make it optional with an empty-string default and treat an empty
    #: value as "nothing to verify against" — `runtime.install_agent` in
    #: that case refuses to install rather than installing an unverified
    #: binary. A deviation from architecture.md §3.
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
        """None — the agent can't be installed on this platform.

        For example Kimi CLI isn't built for `darwin-x86_64` (design.md).
        The UI must surface this as a reason (see `unavailable_reason`)
        rather than silently hiding the install button.
        """
        if self.npx is not None:
            return self.npx
        return self.binaries.get(key or platform_key())

    def unavailable_reason(self, key: str | None = None) -> str:
        """Human-readable reason for the missing distribution under `key`.

        An empty string means "available" — there's no point in the caller
        showing it. A non-empty one is what the UI must display to the
        human instead of just not drawing the install button.
        """
        resolved_key = key or platform_key()
        if self.distribution_for(resolved_key) is not None:
            return ""
        if self.npx is None and not self.binaries:
            return f"{self.name}: no installation method in the registry"
        return f"{self.name} isn't built for {resolved_key}"


class RegistryError(RuntimeError):
    """The registry is unavailable: no network, no usable cache."""


def platform_key() -> str:
    """darwin-aarch64 | darwin-x86_64 | linux-aarch64 | linux-x86_64 | windows-x86_64

    Exactly the five keys that appear as `distribution.binary` entries in
    the live registry (there's also a `windows-aarch64` for some agents, but
    Houdini doesn't exist on Windows ARM, so we never ask for that key).
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
    raise RegistryError(f"unknown platform: {system!r}")


def parse_registry(payload: Mapping) -> list[AgentEntry]:
    """Parse the body of `registry.json`.

    The registry is someone else's JSON with no version guarantee toward
    our expectations. A broken entry (wrong type, missing required field)
    is skipped — the rest of the agents must still come through, otherwise
    a single typo by a third-party maintainer takes down the panel's whole
    "Agents" screen.
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
    # version in the schema is a string, but we don't want to risk dropping
    # an entry over a numeric toad; we take str() of anything serializable
    # except None.
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


# --- on-disk cache -----------------------------------------------------------


def _cache_path() -> Path:
    return paths.cache_dir() / _CACHE_FILE_NAME


def _read_cache(path: Path, *, max_age: float | None) -> list[AgentEntry] | None:
    """`max_age=None` — accept a cache of any age (network unavailable, cache exists)."""
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
    """The agent registry, cached at `<cache>/registry.json`.

    If the cache is fresher than `max_age` and `force=False`, we return it
    without touching the network. If the network is unavailable (including
    with `force=True`, but the request failed), we return the cache at ANY
    age, without pretending the data is fresh: it's better for the "Agents"
    screen to show stale versions than not show anything at all. Neither
    cache nor network — `RegistryError`.
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
            f"{REGISTRY_URL}: network unavailable, and there's no local cache yet"
        ) from None

    entries = parse_registry(payload)
    _write_cache(cache_file, payload)
    return entries

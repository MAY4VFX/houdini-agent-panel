"""Tests for the ACP registry: parsing someone else's JSON, platform selection, caching."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from houdini_agent_panel import paths, registry

SAMPLE_PATH = Path(__file__).parent / "data" / "registry_sample.json"


def _sample_payload() -> dict:
    return json.loads(SAMPLE_PATH.read_text("utf-8"))


def _by_id(entries) -> dict[str, registry.AgentEntry]:
    return {e.id: e for e in entries}


# --- parse_registry ---------------------------------------------------------


def test_parse_registry_returns_featured_six_by_real_ids():
    entries = registry.parse_registry(_sample_payload())
    ids = {e.id for e in entries}
    for featured_id in registry.FEATURED_AGENT_IDS:
        assert featured_id in ids


def test_parse_registry_skips_broken_entry_without_losing_others():
    entries = registry.parse_registry(_sample_payload())
    # "id": 12345 (not a string) — the entry should be silently skipped.
    assert all(isinstance(e.id, str) for e in entries)
    # but the other eight agents in the fixture must still come through.
    ids = {e.id for e in entries}
    assert ids == {
        "claude-acp",
        "codex-acp",
        "gemini",
        "grok-build",
        "kimi",
        "opencode",
        "crow-cli",
        "auggie",
    }


def test_parse_registry_non_list_agents_returns_empty():
    assert registry.parse_registry({"agents": "not-a-list"}) == []
    assert registry.parse_registry({}) == []
    assert registry.parse_registry("garbage") == []  # type: ignore[arg-type]


def test_parse_npx_distribution_claude():
    entries = _by_id(registry.parse_registry(_sample_payload()))
    claude = entries["claude-acp"]
    assert claude.needs_node is True
    assert claude.npx == registry.NpxDistribution(
        package="@agentclientprotocol/claude-agent-acp@0.64.1", args=[], env={}
    )
    assert claude.binaries == {}


def test_parse_npx_distribution_with_env_and_args():
    entries = _by_id(registry.parse_registry(_sample_payload()))
    auggie = entries["auggie"]
    assert auggie.npx.package == "@augmentcode/auggie@0.34.0"
    assert auggie.npx.args == ["--acp"]
    assert auggie.npx.env == {"AUGMENT_DISABLE_AUTO_UPDATE": "1"}


def test_parse_binary_distribution_opencode_has_all_five_contract_keys():
    entries = _by_id(registry.parse_registry(_sample_payload()))
    opencode = entries["opencode"]
    assert opencode.needs_node is False
    assert opencode.npx is None
    for key in (
        "darwin-aarch64",
        "darwin-x86_64",
        "linux-aarch64",
        "linux-x86_64",
        "windows-x86_64",
    ):
        dist = opencode.binaries[key]
        assert dist.archive.startswith("https://")
        assert dist.sha256  # opencode carries sha256 for every platform


def test_parse_binary_distribution_missing_sha256_defaults_to_empty_string():
    entries = _by_id(registry.parse_registry(_sample_payload()))
    crow = entries["crow-cli"]
    assert crow.binaries["darwin-aarch64"].sha256 == ""
    assert crow.binaries["darwin-aarch64"].cmd == "./crow-cli"
    assert crow.binaries["darwin-aarch64"].args == ["acp"]


# --- distribution_for / unavailable_reason ----------------------------------


def test_kimi_has_no_darwin_x86_64_build():
    entries = _by_id(registry.parse_registry(_sample_payload()))
    kimi = entries["kimi"]
    assert kimi.distribution_for("darwin-x86_64") is None
    reason = kimi.unavailable_reason("darwin-x86_64")
    assert reason  # there must be a non-empty reason, not a silent None
    assert "darwin-x86_64" in reason


def test_kimi_has_darwin_aarch64_build():
    entries = _by_id(registry.parse_registry(_sample_payload()))
    kimi = entries["kimi"]
    dist = kimi.distribution_for("darwin-aarch64")
    assert isinstance(dist, registry.BinaryDistribution)
    assert kimi.unavailable_reason("darwin-aarch64") == ""


def test_npx_distribution_for_ignores_platform_key():
    entries = _by_id(registry.parse_registry(_sample_payload()))
    claude = entries["claude-acp"]
    # an npx agent installs regardless of platform — Node sorts out the architecture itself.
    assert claude.distribution_for("windows-x86_64") is claude.npx
    assert claude.unavailable_reason("windows-x86_64") == ""


def test_unavailable_reason_for_agent_without_any_distribution():
    entry = registry.AgentEntry(id="x", name="X", version="1.0.0")
    assert entry.distribution_for("darwin-aarch64") is None
    assert "no installation method" in entry.unavailable_reason("darwin-aarch64")


# --- platform_key ------------------------------------------------------------


@pytest.mark.parametrize(
    "system, machine, expected",
    [
        ("Darwin", "arm64", "darwin-aarch64"),
        ("Darwin", "x86_64", "darwin-x86_64"),
        ("Linux", "aarch64", "linux-aarch64"),
        ("Linux", "x86_64", "linux-x86_64"),
        ("Windows", "AMD64", "windows-x86_64"),
        ("Windows", "ARM64", "windows-x86_64"),
    ],
)
def test_platform_key(monkeypatch, system, machine, expected):
    monkeypatch.setattr(registry.platform, "system", lambda: system)
    monkeypatch.setattr(registry.platform, "machine", lambda: machine)
    assert registry.platform_key() == expected


def test_platform_key_unknown_system_raises(monkeypatch):
    monkeypatch.setattr(registry.platform, "system", lambda: "BeOS")
    with pytest.raises(registry.RegistryError):
        registry.platform_key()


# --- fetch_registry: cache and network ----------------------------------------


def test_fetch_registry_uses_fetcher_and_writes_cache(fetcher):
    fetcher.add_json(registry.REGISTRY_URL, _sample_payload())
    entries = registry.fetch_registry(fetch=fetcher)
    assert len(entries) == 8
    assert fetcher.calls == [registry.REGISTRY_URL]

    cache_path = paths.cache_dir() / "registry.json"
    assert cache_path.exists()
    wrapper = json.loads(cache_path.read_text("utf-8"))
    assert wrapper["payload"]["agents"]


def test_fetch_registry_fresh_cache_skips_network(fetcher):
    fetcher.add_json(registry.REGISTRY_URL, _sample_payload())
    registry.fetch_registry(fetch=fetcher)
    assert len(fetcher.calls) == 1

    # a second call within max_age must not hit the network again.
    entries = registry.fetch_registry(fetch=fetcher, max_age=86400.0)
    assert len(fetcher.calls) == 1
    assert len(entries) == 8


def test_fetch_registry_force_refetches_even_with_fresh_cache(fetcher):
    fetcher.add_json(registry.REGISTRY_URL, _sample_payload())
    registry.fetch_registry(fetch=fetcher)
    assert len(fetcher.calls) == 1

    registry.fetch_registry(fetch=fetcher, force=True)
    assert len(fetcher.calls) == 2


def test_fetch_registry_offline_with_any_age_cache_returns_stale(fetcher):
    fetcher.add_json(registry.REGISTRY_URL, _sample_payload())
    registry.fetch_registry(fetch=fetcher)

    # artificially age the cache, so max_age definitely won't consider it fresh.
    cache_path = paths.cache_dir() / "registry.json"
    wrapper = json.loads(cache_path.read_text("utf-8"))
    wrapper["fetched_at"] -= 10 * 86400.0
    cache_path.write_text(json.dumps(wrapper), "utf-8")

    from houdini_agent_panel.network import NetworkError

    class OfflineFetcher:
        def __init__(self):
            self.calls = []

        def __call__(self, url, *, timeout=30.0):
            self.calls.append(url)
            raise NetworkError("no network")

    offline = OfflineFetcher()
    entries = registry.fetch_registry(fetch=offline, max_age=1.0)
    assert len(entries) == 8
    assert offline.calls == [registry.REGISTRY_URL]  # the network was actually tried


def test_fetch_registry_no_cache_no_network_raises(fetcher):
    from houdini_agent_panel.network import NetworkError

    def always_fails(url, *, timeout=30.0):
        raise NetworkError("no network")

    with pytest.raises(registry.RegistryError):
        registry.fetch_registry(fetch=always_fails)

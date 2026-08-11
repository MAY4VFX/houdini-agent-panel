"""Panel settings — one small JSON file.

Read whole, written atomically. There are deliberately no partial merges:
the file has a dozen keys, and `os.replace` over a temp file guarantees
that a Houdini crash mid-write never leaves someone with a truncated JSON
file and no panel.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import paths

SETTINGS_VERSION = 1


@dataclass
class CustomAgent:
    """"Custom Agent" — an arbitrary command that speaks ACP.

    No version, no download: the human already installed this themselves,
    our job is just to launch it and stay out of the way.
    """

    id: str
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class InstalledAgent:
    agent_id: str
    version: str
    kind: str  # "npx" | "binary" | "custom"
    installed_at: str = ""

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class AgentAuthMethod:
    """One entry from `client.AgentInfo.auth_methods`, kept here as plain
    data — `id`/`name`/`description` mirror `client.AuthMethod`'s own
    fields exactly, so this module never has to import the client layer
    just to remember what an agent offered (design.md's four layers)."""

    id: str
    name: str
    description: str = ""


@dataclass
class AgentAuthInfo:
    """What an agent's own `initialize` said about signing in, the last
    time it actually connected — cached because `authMethods`/`supports_
    logout` are constants of the BUILD, not the account
    (docs/facts/acp-sdk.md §11), so unlike "is the artist actually signed
    in right now" they don't go stale between one connection and the next.

    This is what lets a Settings row offer Sign in/Sign out for an agent
    that isn't the one currently connected in this tab, instead of only
    ever the single agent a tab happens to be running right now (issue
    #33) — without launching every installed agent just to ask it.
    """

    methods: list[AgentAuthMethod] = field(default_factory=list)
    supports_logout: bool = False


@dataclass
class AuthAttempt:
    """What the last sign-in or sign-out attempt on this agent actually
    did. Shown right beside the Settings row that started it — a failure
    that only ever lived on the sign-in screen's own `QLabel` was invisible
    again the instant the artist left that screen, or was never visible at
    all for an agent that isn't the one connected right now. Both are
    exactly issue #33's report.
    """

    action: str = "sign_in"  # "sign_in" | "sign_out"
    method_id: str = ""
    ok: bool = False
    message: str = ""
    at: str = ""

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Settings:
    version: int = SETTINGS_VERSION
    default_agent: str | None = None
    autostart_agent: bool = True
    check_updates: bool = True
    show_announcements: bool = True
    telemetry: bool = False
    telemetry_consent_asked: bool = False
    whisper_endpoint: str = ""
    #: API key for `whisper_endpoint`, sent as `X-API-Key` by `ui/voice.py::
    #: default_uploader`. Blank means "no auth header" — a local, unauthenticated
    #: whisper (the common case this field was added alongside) must keep
    #: working with nothing filled in here.
    whisper_api_key: str = ""
    buddy: str = "crag"
    #: Studio proxy, e.g. "http://proxy.studio.local:8080". Empty means
    #: "whatever the machine already exports" — see `proxy.effective_proxy`.
    proxy_url: str = ""
    #: Extra bypass entries. `localhost`/`127.0.0.1`/`::1` are always added.
    no_proxy: str = ""
    #: PEM bundle for a TLS-inspecting proxy.
    ca_bundle: str = ""
    custom_agents: list[CustomAgent] = field(default_factory=list)
    installed_agents: dict[str, InstalledAgent] = field(default_factory=dict)
    seen_announcements: list[str] = field(default_factory=list)
    #: Agents this machine has actually used without hitting `auth_required`
    #: — the only evidence the protocol leaves that somebody is signed in.
    #: Measured on a machine where none of them had ever been configured:
    #: `claude-acp` advertises no methods and opens a session anyway (it
    #: fails at the first prompt), `opencode` advertises one and also opens
    #: a session, `codex-acp` refuses `session/new` outright. So a session
    #: proves nothing on two agents out of three, and a completed turn is
    #: what all three agree on. Persisted, or the Sign in row would come
    #: back on every Houdini restart until the artist typed something.
    signed_in_agents: list[str] = field(default_factory=list)
    #: The artist's last pick for each agent's own `configOptions` (model,
    #: reasoning effort, …) — `{agent_id: {config_id: value}}`. Per AGENT,
    #: not per conversation: a conversation survives switching agents, but
    #: `configOptions` are the CURRENT agent's own vocabulary, so a value
    #: chosen for one agent has no meaning carried into another. ACP scopes
    #: `configOptions` to a live session, which dies with the process — this
    #: is what makes the pick survive a Houdini restart anyway, reapplied
    #: onto the next `session/new` (`AgentPanel._reapply_remembered_config`).
    config_options_by_agent: dict[str, dict[str, str]] = field(default_factory=dict)
    #: What each agent's own `initialize` said about signing in, the last
    #: time it connected — see `AgentAuthInfo`. Read by every Settings row,
    #: not only the one currently connected in this tab.
    agent_auth_info: dict[str, AgentAuthInfo] = field(default_factory=dict)
    #: The last sign-in/sign-out attempt per agent id — see `AuthAttempt`.
    auth_attempts: dict[str, AuthAttempt] = field(default_factory=dict)
    #: Where the in-panel bug reporter files to — configurable because the
    #: service isn't the only thing that can move (`bugreport.
    #: DEFAULT_ENDPOINT` is only the starting value).
    bugreport_endpoint: str = ""
    #: The artist's last choice for each attachment, remembered so it
    #: doesn't have to be re-made every time — someone working under NDA
    #: removes "conversation" once and it stays removed, rather than
    #: fighting the same checkbox on every report (the report this
    #: feature exists to answer). Missing keys default to included; see
    #: `ui/bugreport_view.py`.
    bugreport_attachments: dict[str, bool] = field(default_factory=dict)
    #: `{agent_id: {env_var: token}}` — a token minted by a terminal-auth
    #: command that prints it once and never writes it anywhere else
    #: (Claude's `setup-token`, docs/facts/acp-sdk.md §21: it exits with no
    #: credentials file at all — `ui/terminal_login.py::TerminalLoginWorker.
    #: token_captured` is the only chance to catch it). Stored here on the
    #: same trust level `proxy_url`/`ca_bundle` already carry — this file
    #: is per-machine, unsynced, plain JSON, same as any local credentials
    #: file the CLI itself would have written if run in a real terminal —
    #: and injected into that agent's own launch env by `runtime.py::
    #: _with_oauth_tokens`, the same way `proxy.child_env` already injects
    #: the studio proxy. Keyed by agent id so a future agent found to have
    #: the same "print once, store it yourself" shape (team asked to check
    #: Codex/Gemini/Grok/Kimi — not yet established either way) has
    #: somewhere to go without a second field.
    agent_oauth_tokens: dict[str, dict[str, str]] = field(default_factory=dict)
    #: Launch agents against a panel-owned config directory instead of the
    #: artist's real one (`runtime.py::_with_config_isolation`,
    #: `paths.agent_config_dir`). Off by default: today's behavior — every
    #: agent sees the artist's own account config exactly as a terminal
    #: launch would — is what artists already have and expect, and turning
    #: it off is the surprising direction. Currently only `claude-acp` has a
    #: documented variable for this (`CLAUDE_CONFIG_DIR`); every other agent
    #: ignores the setting entirely rather than have us guess at a mechanism
    #: we haven't verified (design.md: "the agent doesn't support it — the
    #: control doesn't get drawn").
    isolate_agent_config: bool = False

    # --- serialization

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["installed_agents"] = {k: asdict(v) for k, v in self.installed_agents.items()}
        payload["agent_auth_info"] = {
            agent_id: {
                "methods": [asdict(m) for m in info.methods],
                "supports_logout": info.supports_logout,
            }
            for agent_id, info in self.agent_auth_info.items()
        }
        payload["auth_attempts"] = {k: asdict(v) for k, v in self.auth_attempts.items()}
        return payload

    @classmethod
    def from_dict(cls, payload: Any) -> "Settings":
        """Build settings out of whatever's given, without raising.

        Unknown keys are ignored, missing ones fall back to their defaults,
        and a key of the wrong type falls back to its default too. The
        settings file gets edited by hand, and a typo in it shouldn't cost
        someone their workday.
        """
        settings = cls()
        if not isinstance(payload, dict):
            return settings

        known = {f.name: f for f in fields(cls)}
        for name, spec in known.items():
            if name not in payload:
                continue
            value = payload[name]
            if name == "custom_agents":
                settings.custom_agents = [
                    CustomAgent(
                        id=str(item.get("id", "")),
                        name=str(item.get("name", "")),
                        command=str(item.get("command", "")),
                        args=[str(a) for a in item.get("args", []) or []],
                        env={str(k): str(v) for k, v in (item.get("env") or {}).items()},
                    )
                    for item in value or []
                    if isinstance(item, dict) and item.get("id") and item.get("command")
                ]
            elif name == "installed_agents":
                installed: dict[str, InstalledAgent] = {}
                for key, item in (value or {}).items():
                    if not isinstance(item, dict):
                        continue
                    installed[str(key)] = InstalledAgent(
                        agent_id=str(item.get("agent_id", key)),
                        version=str(item.get("version", "")),
                        kind=str(item.get("kind", "binary")),
                        installed_at=str(item.get("installed_at", "")),
                    )
                settings.installed_agents = installed
            elif name == "config_options_by_agent":
                by_agent: dict[str, dict[str, str]] = {}
                for agent_id, mapping in (value or {}).items():
                    if not isinstance(mapping, dict):
                        continue
                    by_agent[str(agent_id)] = {
                        str(config_id): str(v) for config_id, v in mapping.items()
                    }
                settings.config_options_by_agent = by_agent
            elif name == "bugreport_attachments":
                settings.bugreport_attachments = {
                    str(k): bool(v) for k, v in (value or {}).items()
                }
            elif name == "agent_auth_info":
                auth_info: dict[str, AgentAuthInfo] = {}
                for agent_id, item in (value or {}).items():
                    if not isinstance(item, dict):
                        continue
                    methods = [
                        AgentAuthMethod(
                            id=str(m.get("id", "")),
                            name=str(m.get("name", "")),
                            description=str(m.get("description", "")),
                        )
                        for m in item.get("methods", []) or []
                        if isinstance(m, dict) and m.get("id")
                    ]
                    auth_info[str(agent_id)] = AgentAuthInfo(
                        methods=methods,
                        supports_logout=bool(item.get("supports_logout", False)),
                    )
                settings.agent_auth_info = auth_info
            elif name == "auth_attempts":
                attempts: dict[str, AuthAttempt] = {}
                for agent_id, item in (value or {}).items():
                    if not isinstance(item, dict):
                        continue
                    attempts[str(agent_id)] = AuthAttempt(
                        action=str(item.get("action", "sign_in")),
                        method_id=str(item.get("method_id", "")),
                        ok=bool(item.get("ok", False)),
                        message=str(item.get("message", "")),
                        at=str(item.get("at", "")),
                    )
                settings.auth_attempts = attempts
            elif name == "agent_oauth_tokens":
                oauth_tokens: dict[str, dict[str, str]] = {}
                for agent_id, mapping in (value or {}).items():
                    if not isinstance(mapping, dict):
                        continue
                    oauth_tokens[str(agent_id)] = {
                        str(env_var): str(v) for env_var, v in mapping.items()
                    }
                settings.agent_oauth_tokens = oauth_tokens
            elif name == "default_agent":
                settings.default_agent = str(value) if value else None
            elif spec.type == "bool" or isinstance(getattr(settings, name), bool):
                settings.__dict__[name] = bool(value)
            elif isinstance(getattr(settings, name), str):
                settings.__dict__[name] = str(value)
            elif isinstance(getattr(settings, name), int):
                try:
                    settings.__dict__[name] = int(value)
                except (TypeError, ValueError):
                    pass
            elif isinstance(getattr(settings, name), list):
                settings.__dict__[name] = [str(v) for v in value or []]
        return settings


def load(path: Path | None = None) -> Settings:
    """Read the settings.

    A corrupted file isn't an error: it gets moved aside to
    ``settings.json.broken``, and the human gets a panel with defaults
    instead of a stack trace on open.
    """
    target = path or paths.settings_path()
    if not target.exists():
        return Settings()
    try:
        payload = json.loads(target.read_text("utf-8"))
    except (OSError, ValueError):
        try:
            target.replace(target.with_suffix(target.suffix + ".broken"))
        except OSError:
            pass
        return Settings()
    return Settings.from_dict(payload)


def save(settings: Settings, path: Path | None = None) -> None:
    target = path or paths.settings_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(settings.to_dict(), indent=2, ensure_ascii=False) + "\n", "utf-8")
    os.replace(tmp, target)


def agent_owns_token(agent_id: str, settings: Settings) -> bool:
    """Does the panel itself hold a captured, working OAuth token for
    `agent_id` — `settings.agent_oauth_tokens[agent_id]` non-empty?

    This is a narrower, stronger question than `signed_in_agents`
    (`ui/panel.py::AgentPanel._is_signed_in`'s own docstring: a completed
    turn, which is itself only ever a guess — the protocol has no "am I
    authenticated" query) or `agent_auth_info` (what the agent's BUILD
    advertises, not what account is active). For an agent whose credential
    the panel captured and verified itself (currently `claude-acp` only —
    docs/facts/acp-sdk.md §21/§27: `claude setup-token` prints a token once
    and this is the only place it is ever kept), owning that token IS being
    signed in — a fact read off this settings file, not an inference from
    indirect evidence. `ui/agents.py::_is_agent_signed_in`/`_can_sign_out_
    agent` use this to let a Settings row draw "Sign out" for such an
    agent even though it advertises no `authMethods` and implements no
    protocol `logout` at all — see those functions' own docstrings for why
    that does not contradict the reasoning `AgentPanel._can_sign_out`
    still applies to the agent's own protocol capability.
    """
    return bool(settings.agent_oauth_tokens.get(agent_id))


def diagnostics(settings: Settings) -> str:
    """Text for the "Copy diagnostics" button.

    Everything needed for a bug report, and nothing a human wouldn't want
    to send: no scene paths, no project names, no settings contents.
    """
    import platform
    import sys

    lines = [
        f"houdini-agent-panel {_panel_version()}",
        f"python {sys.version.split()[0]} ({paths.python_tag()})",
        f"os {platform.platform()}",
    ]

    try:
        from .ui.qt import QT_SOURCE, QT_VERSION

        lines.append(f"qt {QT_VERSION} via {QT_SOURCE}")
    except Exception as exc:  # noqa: BLE001 - diagnostics is not allowed to raise
        lines.append(f"qt unavailable: {exc!r}")

    try:
        from . import scene

        lines.append(f"houdini {scene.houdini_version()}")
        lines.append(f"fx port {scene.fx_port()}")
    except Exception as exc:  # noqa: BLE001
        lines.append(f"houdini unavailable: {exc!r}")

    try:
        import fxhoudinimcp

        lines.append(f"fxhoudinimcp {getattr(fxhoudinimcp, '__version__', 'unknown')}")
    except Exception:  # noqa: BLE001
        lines.append("fxhoudinimcp is not importable")

    lines.append(f"default agent: {settings.default_agent or '—'}")
    for agent_id, installed in sorted(settings.installed_agents.items()):
        lines.append(f"agent {agent_id} {installed.version} ({installed.kind})")
    lines.append(f"updates: {settings.check_updates}, announcements: {settings.show_announcements}")

    from . import proxy as proxy_module

    address = proxy_module.effective_proxy(settings)
    lines.append(f"proxy: {proxy_module.sanitize(address) if address else '—'}")
    lines.append(f"ca bundle: {proxy_module.effective_ca_bundle(settings) or '—'}")

    lines.append(f"telemetry: {settings.telemetry}")
    return "\n".join(lines)


def _panel_version() -> str:
    try:
        from importlib.metadata import version

        return version("houdini-agent-panel")
    except Exception:  # noqa: BLE001 - metadata may be missing when run from a --target tree
        from . import __version__

        return __version__

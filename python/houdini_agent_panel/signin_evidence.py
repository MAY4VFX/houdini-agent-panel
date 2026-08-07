"""Concrete, checkable evidence that an agent already has credentials — so
the panel never nags an artist who is already signed in.

Written for the owner's own report: he picked Claude Agent, typed "hi",
waited 1m41s, and got a five-line explanation of how to sign in — only to
sign in successfully, because he already was and the wait was just Claude
opening a session it was always going to fail at the first prompt. The fix
is to offer sign-in at connect time instead of after a wasted turn
(`ui/panel.py::_on_connected`). Doing that safely needs a way to tell
"probably not signed in" from "we just have no record of it" — the
panel's OWN record (`settings.signed_in_agents`) only knows about agents
THIS install has seen complete a turn, so a fresh install on a machine
where the artist has used the CLI for months would show nothing and must
not be told to sign in when they already are. That has happened to the
owner twice in one week already; a wrong guess in this direction is worse
than staying quiet.

Every check here answers "is there a credential ON DISK or in this
agent's OWN ENV VAR that would let it authenticate" — never "is it
CURRENTLY valid" (a stale or revoked token still counts as evidence: the
agent's own first prompt is what proves validity, same as
`AgentPanel._is_signed_in`'s own docstring argues for `signed_in_agents`
itself). A false "looks signed in" costs nothing here — the panel stays
quiet, same as it already would once `signed_in_agents` catches up. A
false "not signed in" is what actually costs something: the nag this
module exists to avoid.

Measured on a real, in-use machine (mayfx02) for every check below except
where a comment says otherwise — nothing here is a guess about a file
format never actually seen.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

#: The macOS Keychain service Claude Code's own CLI writes to — found with
#: `security dump-keychain` on a machine signed in through the desktop app:
#: one `"svce"="Claude Code-credentials"` entry, account "Claude Key". This
#: is the case `~/.claude/.credentials.json` alone misses — the exact
#: machine this module was written on has no such file and IS signed in,
#: entirely through this keychain entry.
#:
#: NOT what `claude setup-token` writes — corrected by docs/facts/acp-
#: sdk.md §21, which found `setup-token` writes NEITHER this file NOR a
#: keychain entry: it mints a subscription-scoped OAuth token, prints it
#: once, and exits. `_CLAUDE_OAUTH_TOKEN_ENV_VAR` below is that flow's own
#: evidence instead.
_CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"
#: What `claude setup-token` tells the artist to set (docs/facts/acp-
#: sdk.md §21, confirmed from the real bundled binary's own string table,
#: not guessed). This module stays free of any `ui/` import (Qt-free,
#: checkable without a `QApplication`, same as its own module docstring
#: already promises) — kept as its own literal rather than imported from
#: `ui/terminal_login.py::_OAUTH_TOKEN_ENV_VAR`, which captures the SAME
#: value under the same name; a test on either side would catch the two
#: drifting apart.
_CLAUDE_OAUTH_TOKEN_ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"


def _read_json_object(path: Path) -> dict | None:
    try:
        payload = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _claude_keychain_entry_exists() -> bool | None:
    """Existence only — never the secret itself.

    `security find-generic-password` without `-w` looks up the entry's
    metadata and never touches its data, so this needs no Keychain access
    prompt (measured: 17ms, exit 0, on a machine where the entry exists;
    22ms, exit 44, where it doesn't — neither blocked or prompted).
    `None` means "not checkable here", kept distinct from `False` so a
    non-Mac or a machine without `/usr/bin/security` falls through to the
    other checks instead of being told "not found".
    """
    if sys.platform != "darwin":
        return None
    security = shutil.which("security")
    if security is None:
        return None
    try:
        result = subprocess.run(
            [security, "find-generic-password", "-s", _CLAUDE_KEYCHAIN_SERVICE],
            capture_output=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.returncode == 0


def _claude(home: Path, env: dict[str, str], agent_oauth_tokens: dict[str, dict[str, str]]) -> bool:
    # `~/.claude/.credentials.json`: what a desktop-app / interactive
    # `claude login` writes — NOT what `setup-token` writes (docs/facts/
    # acp-sdk.md §21 corrects an earlier wrong assumption here: `setup-
    # token` writes no credentials file at all, it prints a token once and
    # exits). `ANTHROPIC_API_KEY`: a different wallet entirely — API
    # billing, not the subscription this module is otherwise checking for
    # (§21; see `_no_methods_advice`'s own rewrite). `CLAUDE_CODE_OAUTH_
    # TOKEN` covers it two ways: the artist's own shell profile (same
    # category as the `ANTHROPIC_API_KEY` check right above it) via `env`,
    # or a token THIS panel already captured and stored (`settings.py::
    # Settings.agent_oauth_tokens`) but the artist's shell was never told
    # about — `env` alone would miss that one entirely.
    if (home / ".claude" / ".credentials.json").exists():
        return True
    if env.get("ANTHROPIC_API_KEY") or env.get(_CLAUDE_OAUTH_TOKEN_ENV_VAR):
        return True
    if agent_oauth_tokens.get("claude-acp", {}).get(_CLAUDE_OAUTH_TOKEN_ENV_VAR):
        return True
    return bool(_claude_keychain_entry_exists())


def _codex(home: Path, env: dict[str, str]) -> bool:
    # Exact names measured directly: a signed-out `codex-acp` fails a
    # prompt with "CODEX_API_KEY or OPENAI_API_KEY is not set" (docs/
    # facts/acp-sdk.md §11 table). `~/.codex/auth.json` measured non-empty
    # on a signed-in machine — keys `auth_mode`, `OPENAI_API_KEY`,
    # `tokens`, `last_refresh`; existence and non-emptiness is the bar
    # here, not validating any of those fields.
    if env.get("CODEX_API_KEY") or env.get("OPENAI_API_KEY"):
        return True
    return bool(_read_json_object(home / ".codex" / "auth.json"))


def _opencode(home: Path, env: dict[str, str]) -> bool:
    # opencode's own multi-provider credential store, measured non-empty
    # on a signed-in machine: `{"anthropic": {...}, "kimi-for-coding":
    # {...}, ...}` — one entry per provider it has been signed into, not
    # one fixed shape. Any provider present is evidence; opencode itself
    # decides which one a session actually uses. No env var checked here:
    # unlike Claude/Codex, nothing in docs/facts pins one variable name to
    # opencode's own adapter.
    return bool(_read_json_object(home / ".local" / "share" / "opencode" / "auth.json"))


def _grok(home: Path, env: dict[str, str]) -> bool:
    # `~/.grok/auth.json` measured non-empty on a signed-in machine: keyed
    # by the OAuth issuer URL (`"https://auth.x.ai::<uuid>"`), token data
    # under it. `XAI_API_KEY` is x.ai's own documented variable name for
    # API-key auth — real, but not independently confirmed read by this
    # specific adapter the way Codex's pair was, so it is checked as a
    # second, weaker signal, not the primary one.
    if env.get("XAI_API_KEY"):
        return True
    return bool(_read_json_object(home / ".grok" / "auth.json"))


def _gemini(home: Path, env: dict[str, str]) -> bool:
    # `GEMINI_API_KEY` is the case `shellenv.py`'s own module docstring
    # was written about (a real report: the panel couldn't see it, the
    # artist's terminal could). `GOOGLE_CLOUD_PROJECT` is that same
    # report's other half — Vertex/ADC auth rather than an API key; set
    # without ever running `gcloud auth application-default login` would
    # be unusual, so it's included as a weaker signal, not equated with
    # the API key. `~/.gemini/oauth_creds.json` is gemini-cli's documented
    # OAuth token cache — the path is real, but NOT measured populated
    # here: this machine's `~/.gemini/google_accounts.json` shows
    # `"active": null`, i.e. signed out at the OAuth layer specifically,
    # so there was nothing populated to confirm the shape of. Existence
    # alone is still checked, on the same "false positive costs nothing"
    # reasoning as the rest of this module.
    if env.get("GEMINI_API_KEY") or env.get("GOOGLE_CLOUD_PROJECT"):
        return True
    return (home / ".gemini" / "oauth_creds.json").exists()


def _kimi(env: dict[str, str]) -> bool:
    # `MOONSHOT_API_KEY` is Moonshot AI's (Kimi's maker) own documented
    # variable name — included on the same unconfirmed-but-safe basis as
    # x.ai's above. `~/.kimi-code/config.toml` was measured and rejected
    # as a file check: its `providers.*.api_key` entries configure upstream
    # LLM backends Kimi CLI can route THROUGH (this machine's is an
    # OpenRouter key), which is a real, populated value but not confirmed
    # to mean the kimi-acp ADAPTER itself is signed in — docs/facts/acp-
    # sdk.md §14 documents `kimi login` as a separate device-code OAuth
    # flow, and where THAT persists its own token was not identified
    # within this pass. So: env var only here, falling back to
    # `settings.signed_in_agents` the same as any agent with nothing
    # reliably checkable.
    return bool(env.get("MOONSHOT_API_KEY") or env.get("KIMI_API_KEY"))


#: One check per known agent id (`registry.FEATURED_AGENT_IDS`) — kept as
#: plain functions, not a class, matching `AgentPanel._NO_METHODS_ADVICE`'s
#: own shape for the same kind of per-agent, static knowledge. An id with
#: no entry here has nothing checkable at all; `has_credential_evidence`
#: answers `False` for it, same as a check that ran and found nothing —
#: the caller's fallback to `settings.signed_in_agents` covers both alike.
_CHECKS: dict[str, Callable[[Path, dict[str, str]], bool]] = {
    "codex-acp": _codex,
    "opencode": _opencode,
    "grok-build": _grok,
    "gemini": _gemini,
}


def has_credential_evidence(
    agent_id: str,
    *,
    env: dict[str, str],
    home: Path | None = None,
    agent_oauth_tokens: dict[str, dict[str, str]] | None = None,
) -> bool:
    """Is there a real, checkable reason to believe `agent_id` is already
    signed in — a credential file it would read, or its own env var,
    present in `env` (the SAME composed environment the agent process
    actually gets: `shellenv.merged`/`ui/terminal_login.py::TerminalLogin
    Worker.build_env`, not `os.environ` alone, which is missing whatever
    only the artist's shell profile sets).

    `home` defaults to `Path.home()`; overridable so tests never touch a
    real `~`. `agent_oauth_tokens` defaults to nothing checkable — pass
    `settings.load().agent_oauth_tokens` (`claude-acp`'s own special case,
    `_claude`'s own docstring, docs/facts/acp-sdk.md §21) to catch a token
    this panel already captured but never told the artist's shell about;
    an explicit parameter rather than this module reaching for `settings`
    itself keeps it free of that import, same as the rest of the module.
    """
    resolved_home = home if home is not None else Path.home()
    if agent_id == "kimi":
        return _kimi(env)
    if agent_id == "claude-acp":
        return _claude(resolved_home, env, agent_oauth_tokens or {})
    check = _CHECKS.get(agent_id)
    if check is None:
        return False
    return check(resolved_home, env)

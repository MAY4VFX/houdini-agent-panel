"""settings.py: round-tripping `config_options_by_agent` (the artist's last
pick per agent — model, effort — remembered across a Houdini restart, see
`ui/panel.py::AgentPanel._reapply_remembered_config`)."""

from __future__ import annotations

from houdini_agent_panel import settings as settings_module
from houdini_agent_panel.settings import Settings


def test_config_options_by_agent_defaults_to_empty():
    assert Settings().config_options_by_agent == {}


def test_config_options_by_agent_round_trips(tmp_path):
    path = tmp_path / "settings.json"
    current = Settings()
    current.config_options_by_agent["claude-acp"] = {"model": "sonnet", "effort": "high"}
    settings_module.save(current, path)

    reloaded = settings_module.load(path)
    assert reloaded.config_options_by_agent == {"claude-acp": {"model": "sonnet", "effort": "high"}}


def test_config_options_by_agent_ignores_malformed_entries(tmp_path):
    """Hand-edited settings.json shouldn't crash the panel — same tolerance
    as every other field (`Settings.from_dict`'s own docstring)."""
    path = tmp_path / "settings.json"
    path.write_text(
        '{"config_options_by_agent": {"claude-acp": "not-a-dict", "codex-acp": {"model": 5}}}',
        "utf-8",
    )
    reloaded = settings_module.load(path)
    assert reloaded.config_options_by_agent == {"codex-acp": {"model": "5"}}


def test_agent_auth_info_round_trips(tmp_path):
    """`agent_auth_info` is what lets a Settings row offer Sign in/Sign out
    for an agent that isn't the one connected right now (issue #33) — it
    has to survive a Houdini restart, not just live in memory."""
    path = tmp_path / "settings.json"
    current = Settings()
    current.agent_auth_info["codex-acp"] = settings_module.AgentAuthInfo(
        methods=[
            settings_module.AgentAuthMethod(id="chat-gpt", name="ChatGPT"),
            settings_module.AgentAuthMethod(id="api-key", name="API key", description="env var"),
        ],
        supports_logout=True,
    )
    settings_module.save(current, path)

    reloaded = settings_module.load(path)
    info = reloaded.agent_auth_info["codex-acp"]
    assert [m.id for m in info.methods] == ["chat-gpt", "api-key"]
    assert info.methods[1].description == "env var"
    assert info.supports_logout is True


def test_agent_auth_info_ignores_malformed_entries(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        '{"agent_auth_info": {"codex-acp": "not-a-dict", '
        '"claude-acp": {"methods": [{"name": "no id"}, {"id": "m", "name": "ok"}], '
        '"supports_logout": true}}}',
        "utf-8",
    )
    reloaded = settings_module.load(path)
    assert "codex-acp" not in reloaded.agent_auth_info
    info = reloaded.agent_auth_info["claude-acp"]
    # The entry with no "id" is dropped, same tolerance as every other field.
    assert [m.id for m in info.methods] == ["m"]


def test_agent_oauth_tokens_defaults_to_empty():
    assert Settings().agent_oauth_tokens == {}


def test_agent_oauth_tokens_round_trips(tmp_path):
    """A token minted by a terminal-auth command that prints it once and
    writes it nowhere else (Claude's `setup-token`, docs/facts/acp-sdk.md
    §21) — the only chance to catch it is `TerminalLoginWorker.token_
    captured`; it has to survive a Houdini restart the same as everything
    else here, or the artist has to re-mint one every launch."""
    path = tmp_path / "settings.json"
    current = Settings()
    current.agent_oauth_tokens["claude-acp"] = {"CLAUDE_CODE_OAUTH_TOKEN": "fake-not-a-real-token"}
    settings_module.save(current, path)

    reloaded = settings_module.load(path)
    assert reloaded.agent_oauth_tokens == {"claude-acp": {"CLAUDE_CODE_OAUTH_TOKEN": "fake-not-a-real-token"}}


def test_agent_oauth_tokens_ignores_malformed_entries(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        '{"agent_oauth_tokens": {"claude-acp": "not-a-dict", '
        '"codex-acp": {"SOME_TOKEN": 5}}}',
        "utf-8",
    )
    reloaded = settings_module.load(path)
    assert "claude-acp" not in reloaded.agent_oauth_tokens
    assert reloaded.agent_oauth_tokens == {"codex-acp": {"SOME_TOKEN": "5"}}


def test_agent_owns_token_false_when_nothing_stored():
    assert settings_module.agent_owns_token("claude-acp", Settings()) is False


def test_agent_owns_token_true_once_a_token_is_stored():
    """The fact this bug fix rests on: once the panel has captured and
    verified a token for an agent (docs/facts/acp-sdk.md §21/§27), "signed
    in" is no longer a guess about a completed turn — the credential is
    right here in `agent_oauth_tokens`."""
    current = Settings()
    current.agent_oauth_tokens["claude-acp"] = {"CLAUDE_CODE_OAUTH_TOKEN": "fake-not-a-real-token"}
    assert settings_module.agent_owns_token("claude-acp", current) is True


def test_agent_owns_token_false_for_an_empty_entry():
    """An agent id present with an empty mapping (e.g. after being forgotten
    by popping individual env vars rather than the whole entry) must not
    read as "owns a token" — only a non-empty mapping is a real credential."""
    current = Settings()
    current.agent_oauth_tokens["claude-acp"] = {}
    assert settings_module.agent_owns_token("claude-acp", current) is False


def test_auth_attempts_round_trip(tmp_path):
    """The last sign-in/out attempt per agent — shown beside the Settings
    row that started it (issue #33)."""
    path = tmp_path / "settings.json"
    current = Settings()
    current.auth_attempts["codex-acp"] = settings_module.AuthAttempt(
        action="sign_in", method_id="chat-gpt", ok=False,
        message="Internal error: CODEX_API_KEY or OPENAI_API_KEY is not set",
        at="2026-08-05T00:00:00+00:00",
    )
    settings_module.save(current, path)

    reloaded = settings_module.load(path)
    attempt = reloaded.auth_attempts["codex-acp"]
    assert attempt.action == "sign_in"
    assert attempt.ok is False
    assert "CODEX_API_KEY" in attempt.message

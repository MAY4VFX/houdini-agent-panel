"""Credential evidence, checked before ever assuming "not signed in".

The owner picked Claude Agent, typed "hi", waited 1m41s, and got told to
sign in — while already signed in. `settings.signed_in_agents` only knows
about agents THIS install has watched complete a turn, so a fresh install
on a machine the artist has used the CLI on for months would show nothing
and must not be nagged. These tests pin the concrete, checkable evidence
this module looks at instead of that guess, and that it never touches a
real `~`.
"""

from __future__ import annotations

import json

import pytest

from houdini_agent_panel import signin_evidence as sie

#: Captured before the autouse fixture below ever patches the name, so the
#: two tests that exercise the real function still can.
_real_claude_keychain_entry_exists = sie._claude_keychain_entry_exists


@pytest.fixture(autouse=True)
def no_real_keychain(monkeypatch):
    """The real Keychain check is exercised in its own test below; every
    other test stays deterministic across machines by pinning it off."""
    monkeypatch.setattr(sie, "_claude_keychain_entry_exists", lambda: False)


# --- claude ------------------------------------------------------------


def test_claude_credentials_file_is_evidence(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / ".credentials.json").write_text("{}")

    assert sie.has_credential_evidence("claude-acp", env={}, home=tmp_path)


def test_claude_api_key_env_is_evidence(tmp_path):
    assert sie.has_credential_evidence(
        "claude-acp", env={"ANTHROPIC_API_KEY": "sk-ant-..."}, home=tmp_path
    )


def test_claude_keychain_entry_is_evidence(tmp_path, monkeypatch):
    """The case the file/env checks alone miss: signed in through the
    desktop app, which writes to the macOS Keychain
    ("Claude Code-credentials", measured with `security dump-keychain`),
    never `~/.claude/.credentials.json`."""
    monkeypatch.setattr(sie, "_claude_keychain_entry_exists", lambda: True)

    assert sie.has_credential_evidence("claude-acp", env={}, home=tmp_path)


def test_claude_nothing_present_is_not_evidence(tmp_path):
    assert not sie.has_credential_evidence("claude-acp", env={}, home=tmp_path)


def test_keychain_check_never_reads_the_secret_itself(monkeypatch):
    """`-w` would print the password; this must never pass it."""
    if sie.sys.platform != "darwin":
        pytest.skip("Keychain check only runs on macOS")
    captured = {}

    def fake_run(args, **kwargs):
        captured["args"] = args

        class _Result:
            returncode = 0

        return _Result()

    monkeypatch.setattr(sie.subprocess, "run", fake_run)
    monkeypatch.setattr(sie.shutil, "which", lambda name: "/usr/bin/security")

    assert _real_claude_keychain_entry_exists() is True
    assert "-w" not in captured["args"]
    assert captured["args"][-2:] == ["-s", sie._CLAUDE_KEYCHAIN_SERVICE]


def test_keychain_check_is_not_checkable_off_mac(monkeypatch):
    monkeypatch.setattr(sie.sys, "platform", "linux")

    assert _real_claude_keychain_entry_exists() is None


# --- codex ---------------------------------------------------------------


def test_codex_auth_file_is_evidence(tmp_path):
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text(
        json.dumps({"auth_mode": "chatgpt", "tokens": {}})
    )

    assert sie.has_credential_evidence("codex-acp", env={}, home=tmp_path)


def test_codex_env_vars_are_evidence(tmp_path):
    assert sie.has_credential_evidence("codex-acp", env={"CODEX_API_KEY": "x"}, home=tmp_path)
    assert sie.has_credential_evidence("codex-acp", env={"OPENAI_API_KEY": "x"}, home=tmp_path)


def test_codex_empty_auth_file_is_not_evidence(tmp_path):
    """An empty object is not a signed-in account — same "existence AND
    non-emptiness" bar every JSON check in this module holds to."""
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").write_text("{}")

    assert not sie.has_credential_evidence("codex-acp", env={}, home=tmp_path)


def test_codex_nothing_present_is_not_evidence(tmp_path):
    assert not sie.has_credential_evidence("codex-acp", env={}, home=tmp_path)


# --- opencode --------------------------------------------------------------


def test_opencode_auth_file_is_evidence(tmp_path):
    path = tmp_path / ".local" / "share" / "opencode"
    path.mkdir(parents=True)
    (path / "auth.json").write_text(json.dumps({"anthropic": {"type": "oauth"}}))

    assert sie.has_credential_evidence("opencode", env={}, home=tmp_path)


def test_opencode_nothing_present_is_not_evidence(tmp_path):
    assert not sie.has_credential_evidence("opencode", env={}, home=tmp_path)


# --- grok --------------------------------------------------------------------


def test_grok_auth_file_is_evidence(tmp_path):
    (tmp_path / ".grok").mkdir()
    (tmp_path / ".grok" / "auth.json").write_text(
        json.dumps({"https://auth.x.ai::abc": {"token": "x"}})
    )

    assert sie.has_credential_evidence("grok-build", env={}, home=tmp_path)


def test_grok_env_var_is_evidence(tmp_path):
    assert sie.has_credential_evidence("grok-build", env={"XAI_API_KEY": "x"}, home=tmp_path)


def test_grok_nothing_present_is_not_evidence(tmp_path):
    assert not sie.has_credential_evidence("grok-build", env={}, home=tmp_path)


# --- gemini ------------------------------------------------------------------


def test_gemini_api_key_env_is_evidence(tmp_path):
    assert sie.has_credential_evidence("gemini", env={"GEMINI_API_KEY": "x"}, home=tmp_path)


def test_gemini_cloud_project_env_is_evidence(tmp_path):
    assert sie.has_credential_evidence(
        "gemini", env={"GOOGLE_CLOUD_PROJECT": "my-project"}, home=tmp_path
    )


def test_gemini_oauth_file_is_evidence(tmp_path):
    (tmp_path / ".gemini").mkdir()
    (tmp_path / ".gemini" / "oauth_creds.json").write_text("{}")

    assert sie.has_credential_evidence("gemini", env={}, home=tmp_path)


def test_gemini_nothing_present_is_not_evidence(tmp_path):
    assert not sie.has_credential_evidence("gemini", env={}, home=tmp_path)


# --- kimi: no file check, env only -----------------------------------------


def test_kimi_env_var_is_evidence(tmp_path):
    assert sie.has_credential_evidence("kimi", env={"MOONSHOT_API_KEY": "x"}, home=tmp_path)
    assert sie.has_credential_evidence("kimi", env={"KIMI_API_KEY": "x"}, home=tmp_path)


def test_kimi_config_toml_is_never_read(tmp_path):
    """Measured and deliberately rejected: `~/.kimi-code/config.toml`'s
    `providers.*.api_key` configures an upstream LLM backend (this
    machine's is an OpenRouter key) — real and populated, but not
    confirmed to mean the kimi-acp ADAPTER itself is signed in. So it must
    never be read here, not even a config.toml with a real-looking key."""
    kimi_dir = tmp_path / ".kimi-code"
    kimi_dir.mkdir()
    (kimi_dir / "config.toml").write_text(
        '[providers.openrouter]\ntype = "api"\napi_key = "sk-or-v1-fake"\n'
    )

    assert not sie.has_credential_evidence("kimi", env={}, home=tmp_path)


# --- unknown agent -----------------------------------------------------------


def test_unknown_agent_has_no_evidence(tmp_path):
    assert not sie.has_credential_evidence("some-future-agent", env={}, home=tmp_path)

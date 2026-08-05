"""`client._terminal_auth_from`: telling a real "spawn this" auth method
apart from everything that only looks like one.

Kimi's `login` method carries a nested `field_meta["terminal-auth"]` key
with the exact command to run for a second, independent login process
(docs/facts/acp-sdk.md §13-14) — `authenticate()` on the ACP channel never
answers it at all. Three other agents attach `_meta` to a method for
completely unrelated reasons (a credential provider tag, a gateway
protocol tag), and opencode describes the same "run this in a terminal"
shape as kimi in plain prose with no structured data behind it at all. All
of that has to come out `terminal_auth is None` — only kimi's shape (and
the SDK's own, unmeasured but schema-defined, `type: "terminal"` variant)
may return something.
"""

from __future__ import annotations

from types import SimpleNamespace

from houdini_agent_panel.client import TerminalAuth, _terminal_auth_from


def test_kimis_nested_meta_shape_is_recognised():
    method = SimpleNamespace(
        id="login",
        name="Login with Kimi account",
        description="Run `kimi login` command in the terminal, then follow the instructions to finish login.",
        field_meta={
            "terminal-auth": {
                "command": "/home/may/.local/share/houdini-agent-panel/agents/kimi/1.49.0/kimi",
                "args": ["login"],
                "label": "Kimi Code Login",
                "env": {},
                "type": "terminal",
            }
        },
    )

    result = _terminal_auth_from(method)

    assert result == TerminalAuth(
        command="/home/may/.local/share/houdini-agent-panel/agents/kimi/1.49.0/kimi",
        args=["login"],
        env={},
        label="Kimi Code Login",
    )


def test_the_sdks_own_typed_shape_is_recognised_with_no_command():
    """Unmeasured in practice (no agent probed uses it) — the schema
    defines `TerminalAuthMethod` as "run the agent's own binary again with
    these args", so there is no `command` field to read; `command=None`
    is the signal to the caller that it has to supply its own."""
    method = SimpleNamespace(
        id="terminal", name="Terminal login", description=None,
        type="terminal", args=["--login"], env={"FOO": "bar"},
    )

    result = _terminal_auth_from(method)

    assert result == TerminalAuth(command=None, args=["--login"], env={"FOO": "bar"})


def test_opencodes_identical_looking_prose_is_not_mistaken_for_one():
    """Measured (docs/facts/acp-sdk.md §14): opencode's `auth login` is an
    interactive arrow-key TUI menu, not a stream a client can spawn and
    read — and its method carries no `_meta` at all to detect in the first
    place. The description reads just like kimi's; this must never scrape
    it for a command."""
    method = SimpleNamespace(
        id="opencode-login", name="opencode login",
        description="Run `opencode auth login` in the terminal",
        field_meta=None,
    )

    assert _terminal_auth_from(method) is None


def test_unrelated_meta_tags_do_not_look_like_terminal_auth():
    """codex's `api-key` and gemini's `gemini-api-key`/`gateway` all carry
    `_meta` for something else entirely (a provider or protocol tag) —
    "has _meta at all" must never be the detector."""
    method = SimpleNamespace(
        id="api-key", name="API Key", description=None,
        field_meta={"api-key": {"provider": "openai"}},
    )

    assert _terminal_auth_from(method) is None


def test_a_method_with_no_meta_and_no_type_has_no_terminal_auth():
    method = SimpleNamespace(id="chat-gpt", name="ChatGPT", description=None)

    assert _terminal_auth_from(method) is None

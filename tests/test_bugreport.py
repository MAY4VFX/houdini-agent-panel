"""`bugreport.py`: redaction, context gathering, and sending — the part
that runs before `ui/bugreport_view.py` ever shows anything, and the part
that actually leaves the machine.

`post_report` is tested against a REAL local HTTP server (`http.server`,
same "real process over a mock" preference `test_terminal_login_worker.py`
already uses for a spawned subprocess) — the endpoint this ships against
is explicitly not live yet, so "a request that never reaches the server"
is the one failure path actually exercisable today, and a mock wouldn't
prove urllib's own proxy/error handling does the right thing for real.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from houdini_agent_panel import bugreport


# --- redaction -------------------------------------------------------------


def test_a_github_token_is_redacted():
    text = "clone failed with token ghp_abcdefghijklmnopqrstuvwxyz0123456789AB in the URL"
    redacted, changed = bugreport.redact_secrets(text)
    assert "ghp_" not in redacted
    assert "[REDACTED]" in redacted
    assert changed is True


def test_an_aws_key_id_is_redacted():
    redacted, changed = bugreport.redact_secrets("export AWS_ACCESS_KEY_ID=AKIAABCDEFGHIJKLMNOP")
    assert "AKIA" not in redacted
    assert changed is True


def test_a_pem_private_key_block_is_redacted():
    text = (
        "-----BEGIN PRIVATE KEY-----\n"
        "MIIBVgIBADANBgkqhkiG9w0BAQEFAASCAT8wggE7AgEAAkEA\n"
        "-----END PRIVATE KEY-----"
    )
    redacted, changed = bugreport.redact_secrets(text)
    assert "MIIBVgIBADANBgkqhkiG9w0BAQEFAASCAT8wggE7AgEAAkEA" not in redacted
    assert changed is True


def test_a_key_value_assignment_is_redacted():
    redacted, changed = bugreport.redact_secrets('api_key: "sk-verylongsecretvalue1234567890"')
    assert "verylongsecretvalue" not in redacted
    assert changed is True


def test_ordinary_text_is_left_alone():
    """The point of matching only KNOWN shapes, not a second guard that
    mangles legitimate report text (same reasoning as the service's own
    copy of this list) — a stack trace or a plain URL must survive
    untouched."""
    text = "Traceback (most recent call last):\n  File \"panel.py\", line 42, in _boot\nValueError: bad session id"
    redacted, changed = bugreport.redact_secrets(text)
    assert redacted == text
    assert changed is False


def test_empty_text_is_not_flagged_as_changed():
    redacted, changed = bugreport.redact_secrets("")
    assert redacted == ""
    assert changed is False


# --- gathering context -------------------------------------------------


def test_gather_system_fields_never_raises_even_if_houdini_version_fails(monkeypatch):
    """`diagnostics()`'s own rule applies here too: a bug report must not
    itself crash the panel. `scene.houdini_version()` failing (fx not
    running, `hou` unavailable outside Houdini) must still leave the
    other three fields populated."""
    from houdini_agent_panel import scene

    monkeypatch.setattr(scene, "houdini_version", lambda: (_ for _ in ()).throw(RuntimeError("no hou")))

    fields = bugreport.gather_system_fields("claude-acp")

    assert fields.panel_version
    assert fields.houdini_version == ""
    assert fields.os
    assert fields.agent_id == "claude-acp"


def test_read_log_tail_returns_only_the_last_n_lines(tmp_path):
    log = tmp_path / "panel.log"
    log.write_text("\n".join(f"line {i}" for i in range(100)) + "\n")

    tail, redacted = bugreport.read_log_tail(log, max_lines=10)

    lines = tail.splitlines()
    assert len(lines) == 10
    assert lines[0] == "line 90"
    assert lines[-1] == "line 99"
    assert redacted is False


def test_read_log_tail_redacts_secrets_and_says_so(tmp_path):
    log = tmp_path / "panel.log"
    log.write_text("normal line\ntoken ghp_abcdefghijklmnopqrstuvwxyz0123456789AB leaked here\n")

    tail, redacted = bugreport.read_log_tail(log, max_lines=10)

    assert "ghp_" not in tail
    assert "[REDACTED]" in tail
    assert redacted is True


def test_read_log_tail_missing_file_is_empty_not_an_error(tmp_path):
    tail, redacted = bugreport.read_log_tail(tmp_path / "does-not-exist.log")
    assert tail == ""
    assert redacted is False


def _entry(kind: str, text: str):
    return SimpleNamespace(kind=kind, text=text)


def test_conversation_tail_only_includes_user_and_agent_turns():
    """Not `tool`/`plan`/`activity`/`thought` — those read as internal
    bookkeeping, not "the conversation" a report is meant to show."""
    entries = [
        _entry("user", "the undo button hangs"),
        _entry("tool", "Read scene.py"),
        _entry("thought", "let me check the undo stack"),
        _entry("agent", "Looking into it now."),
    ]
    text, _redacted = bugreport.conversation_tail_text(entries)
    assert "the undo button hangs" in text
    assert "Looking into it now." in text
    assert "Read scene.py" not in text
    assert "let me check the undo stack" not in text


def test_conversation_tail_keeps_only_the_last_n_messages():
    entries = [_entry("user", f"message {i}") for i in range(10)]
    text, _redacted = bugreport.conversation_tail_text(entries, max_messages=3)
    assert "message 9" in text
    assert "message 7" in text
    assert "message 6" not in text


def test_conversation_tail_truncates_one_very_long_message_without_crowding_out_others():
    entries = [
        _entry("user", "short question"),
        _entry("agent", "x" * 5000),
    ]
    text, _redacted = bugreport.conversation_tail_text(entries, max_chars_per_message=100)
    assert "short question" in text
    assert "x" * 100 in text
    assert "x" * 101 not in text
    assert text.rstrip().endswith("…")


def test_conversation_tail_redacts_secrets_and_says_so():
    entries = [_entry("user", "my token is ghp_abcdefghijklmnopqrstuvwxyz0123456789AB")]
    text, redacted = bugreport.conversation_tail_text(entries)
    assert "ghp_" not in text
    assert redacted is True
    assert "[REDACTED]" in text


# --- sending, against a real local HTTP server ------------------------


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D102 - silence the test server
        pass

    def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler's own naming
        length = int(self.headers.get("Content-Length", "0"))
        self.server.last_body = json.loads(self.rfile.read(length))  # type: ignore[attr-defined]
        status, payload = self.server.response  # type: ignore[attr-defined]
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture
def fake_server():
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.response = (201, {"issue_url": "https://github.com/MAY4VFX/houdini-agent-panel/issues/1"})
    server.last_body = None
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join(timeout=3)


def _url(server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}/v1/reports"


def test_a_successful_post_returns_the_issue_url(fake_server):
    issue_url = bugreport.post_report(
        _url(fake_server),
        {"project": "houdini-agent-panel", "title": "t", "body": "b" * 20},
        env={},
    )
    assert issue_url == "https://github.com/MAY4VFX/houdini-agent-panel/issues/1"
    assert fake_server.last_body["project"] == "houdini-agent-panel"


def test_a_server_rejection_names_its_own_detail_message(fake_server):
    """422/404/429/502/503 all use FastAPI's `{"detail": "..."}` shape —
    the artist has to see the SERVER's own reason, not a generic
    "something went wrong"."""
    fake_server.response = (422, {"detail": "body is too short (min 10 characters)"})

    with pytest.raises(bugreport.BugReportError) as exc_info:
        bugreport.post_report(_url(fake_server), {"project": "x", "title": "t", "body": "b"}, env={})

    message = str(exc_info.value)
    assert "422" in message
    assert "body is too short" in message


def test_a_413_uses_the_error_key_not_detail(fake_server):
    """The one response shape that differs: the request-size middleware
    (`limit_body_size`) returns `{"error": "..."}`, not `{"detail": ...}`
    — checked here for real, not assumed from reading the other repo's
    source, since a wrong assumption here would swallow the message on
    exactly the response most likely to be silently dropped."""
    fake_server.response = (413, {"error": "payload too large"})

    with pytest.raises(bugreport.BugReportError) as exc_info:
        bugreport.post_report(_url(fake_server), {"project": "x", "title": "t", "body": "b"}, env={})

    message = str(exc_info.value)
    assert "413" in message
    assert "payload too large" in message


def test_a_connection_that_never_reaches_the_server_is_named_as_such():
    """The endpoint this ships against is explicitly not live yet — this
    is the failure path actually exercisable today, and it must read as
    "could not reach", not a stack trace or a bare timeout."""
    with pytest.raises(bugreport.BugReportError) as exc_info:
        bugreport.post_report(
            "http://127.0.0.1:1/v1/reports",  # nothing listens on port 1
            {"project": "houdini-agent-panel", "title": "t", "body": "b" * 20},
            env={},
            timeout=3.0,
        )
    assert "could not reach" in str(exc_info.value).lower()


def test_a_success_response_missing_issue_url_is_still_an_error(fake_server):
    fake_server.response = (201, {"ok": True})

    with pytest.raises(bugreport.BugReportError):
        bugreport.post_report(_url(fake_server), {"project": "x", "title": "t", "body": "b"}, env={})


def test_the_proxy_env_var_is_actually_consulted(fake_server):
    """`env`'s `HTTPS_PROXY`/`HTTP_PROXY` — the composed environment
    `TerminalLoginWorker.build_env` produces — must be the thing that
    decides where the request actually goes, not just accepted and
    ignored. Points at a target that would fail to resolve on its own,
    and the real local server as the "proxy": the request only lands if
    the proxy setting was genuinely used to route it there."""
    issue_url = bugreport.post_report(
        "http://this-host-does-not-exist.invalid/v1/reports",
        {"project": "houdini-agent-panel", "title": "t", "body": "b" * 20},
        env={"HTTP_PROXY": f"http://127.0.0.1:{fake_server.server_address[1]}"},
        timeout=5.0,
    )
    assert issue_url == "https://github.com/MAY4VFX/houdini-agent-panel/issues/1"

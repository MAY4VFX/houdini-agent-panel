"""`BugReportWorker`: the `Worker` wrapper around `bugreport.post_report`
— against the same real local HTTP server `test_bugreport.py` uses, since
`post_report` itself is already proven there and this only has to prove
the Qt wiring around it doesn't lose anything.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from houdini_agent_panel.ui.bugreport_worker import BugReportWorker


def _wait_until(app, condition, *, timeout_ms: int = 5000) -> None:
    from PySide6 import QtTest

    elapsed = 0
    step = 20
    while not condition() and elapsed < timeout_ms:
        app.processEvents()
        QtTest.QTest.qWait(step)
        elapsed += step
    assert condition(), "condition did not become true in time"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa: D102
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
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
    server.response = (201, {"issue_url": "https://github.com/MAY4VFX/houdini-agent-panel/issues/7"})
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    thread.join(timeout=3)


def _url(server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}/v1/reports"


def test_success_emits_the_issue_url(qapp, fake_server, monkeypatch):
    from houdini_agent_panel import shellenv as shellenv_module

    monkeypatch.setattr(shellenv_module, "capture", lambda **_: {})
    worker = BugReportWorker(
        _url(fake_server),
        {"project": "houdini-agent-panel", "title": "t", "body": "b" * 20},
    )
    succeeded: list[str] = []
    failed: list[str] = []
    worker.succeeded.connect(succeeded.append)
    worker.failed.connect(failed.append)
    worker.start()

    _wait_until(qapp, lambda: succeeded or failed)
    worker.wait(3000)

    assert failed == []
    assert succeeded == ["https://github.com/MAY4VFX/houdini-agent-panel/issues/7"]


def test_a_server_error_becomes_a_failed_signal_with_the_real_reason(qapp, fake_server, monkeypatch):
    from houdini_agent_panel import shellenv as shellenv_module

    monkeypatch.setattr(shellenv_module, "capture", lambda **_: {})
    fake_server.response = (422, {"detail": "title is too short (min 3 characters)"})
    worker = BugReportWorker(_url(fake_server), {"project": "x", "title": "t", "body": "b"})
    succeeded: list[str] = []
    failed: list[str] = []
    worker.succeeded.connect(succeeded.append)
    worker.failed.connect(failed.append)
    worker.start()

    _wait_until(qapp, lambda: succeeded or failed)
    worker.wait(3000)

    assert succeeded == []
    assert "title is too short" in failed[0]


def test_an_unreachable_endpoint_becomes_a_failed_signal(qapp, monkeypatch):
    """The one path actually exercisable today, against the real
    not-yet-live default — the worker must surface it as `failed`, not
    hang or die silently (`ui/worker.py`'s whole reason for existing)."""
    from houdini_agent_panel import shellenv as shellenv_module

    monkeypatch.setattr(shellenv_module, "capture", lambda **_: {})
    worker = BugReportWorker(
        "http://127.0.0.1:1/v1/reports",  # nothing listens on port 1
        {"project": "houdini-agent-panel", "title": "t", "body": "b" * 20},
    )
    succeeded: list[str] = []
    failed: list[str] = []
    worker.succeeded.connect(succeeded.append)
    worker.failed.connect(failed.append)
    worker.start()

    _wait_until(qapp, lambda: succeeded or failed, timeout_ms=8000)
    worker.wait(3000)

    assert succeeded == []
    assert "could not reach" in failed[0].lower()

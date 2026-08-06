"""`BugReportView`: the screen behind "nothing is sent that the artist has
not had the chance to read" — every attachment is real, editable, visible
text; redaction happens twice (gather time and again immediately before
sending); nothing typed is lost on failure.
"""

from __future__ import annotations

from houdini_agent_panel import bugreport
from houdini_agent_panel.ui.bugreport_view import BugReportView
from houdini_agent_panel.ui.qt import QtWidgets


def _wait_until(app, condition, *, timeout_ms: int = 5000) -> None:
    from PySide6 import QtTest

    elapsed = 0
    step = 20
    while not condition() and elapsed < timeout_ms:
        app.processEvents()
        QtTest.QTest.qWait(step)
        elapsed += step
    assert condition(), "condition did not become true in time"


def _open(view: BugReportView, **overrides) -> None:
    # `isVisible()` on a child reflects the WHOLE ancestor chain in Qt —
    # a `setVisible(True)` on `_copy_button` reports False in any test
    # that checks it unless the top-level widget was actually shown.
    view.show()
    fields = overrides.pop(
        "system_fields",
        bugreport.SystemFields(panel_version="0.5.1", houdini_version="20.5.445", os="macOS 26.5", agent_id="claude-acp"),
    )
    defaults = dict(
        system_fields=fields,
        log_tail="line one\nline two",
        log_redacted=False,
        conversation_tail="You: it crashed\n\nAgent: looking into it",
        conversation_redacted=False,
        attachment_prefs={},
        endpoint="http://127.0.0.1:1/v1/reports",
    )
    defaults.update(overrides)
    view.open_for(**defaults)


def test_open_for_populates_every_field_and_clears_the_previous_report(qapp):
    view = BugReportView()
    view._title_edit.setText("leftover from before")
    view._description_edit.setPlainText("leftover description")

    _open(view)

    assert view._title_edit.text() == ""
    assert view._description_edit.toPlainText() == ""
    assert "Houdini version: 20.5.445" in view._system_section.text()
    assert view._log_section.text() == "line one\nline two"
    assert "it crashed" in view._conversation_section.text()


def test_all_three_attachments_are_included_by_default(qapp):
    view = BugReportView()
    _open(view, attachment_prefs={})

    assert view._system_section.is_included()
    assert view._log_section.is_included()
    assert view._conversation_section.is_included()


def test_a_remembered_preference_excludes_an_attachment_on_open(qapp):
    """The NDA case this feature exists to answer: removed once, it stays
    removed on the next report without a second fight."""
    view = BugReportView()
    _open(view, attachment_prefs={"conversation": False})

    assert view._conversation_section.is_included() is False
    assert view._system_section.is_included() is True


def test_toggling_an_attachment_emits_the_current_preferences(qapp):
    view = BugReportView()
    _open(view)
    changes: list[dict] = []
    view.attachments_changed.connect(changes.append)

    view._conversation_section._toggle_button.click()

    assert changes[-1]["conversation"] is False
    assert changes[-1]["log"] is True
    assert view._conversation_section.text() == "You: it crashed\n\nAgent: looking into it", (
        "removing an attachment must not lose its text — it can be restored"
    )


def test_removed_attachment_is_excluded_from_the_composed_report(qapp):
    view = BugReportView()
    _open(view)
    view._description_edit.setPlainText("the real description, long enough to pass validation")
    view._conversation_section._toggle_button.click()  # remove it

    title, body, fields = view._compose_full_report()

    assert "it crashed" not in body
    assert "the real description" in body


def test_system_fields_round_trip_through_the_editable_text_into_the_payload(qapp):
    """The four structured fields are shown as real editable text (not a
    checkbox standing in for them) and sent exactly as shown — editing
    the text IS editing what gets sent, no separate hidden model."""
    view = BugReportView()
    _open(view)
    view._description_edit.setPlainText("a description that is definitely long enough")
    view._system_section._text_edit.setPlainText(
        "Panel version: 9.9.9\nHoudini version: 99.0\nOS: TestOS\nAgent: test-agent"
    )

    _title, _body, fields = view._compose_full_report()

    assert fields == {
        "panel_version": "9.9.9",
        "houdini_version": "99.0",
        "os": "TestOS",
        "agent_id": "test-agent",
    }


def test_a_short_title_is_rejected_before_anything_is_sent(qapp, monkeypatch):
    from houdini_agent_panel.ui import bugreport_worker as worker_module

    started = []
    monkeypatch.setattr(worker_module.BugReportWorker, "work", lambda self: started.append(True))
    view = BugReportView()
    _open(view)
    view._title_edit.setText("ab")  # below TITLE_MIN
    view._description_edit.setPlainText("a description that is definitely long enough")

    view._on_send_clicked()

    assert started == []
    assert "title" in view._status_label.text().lower()


def test_a_credential_typed_by_the_artist_is_redacted_and_sending_is_held(qapp, monkeypatch):
    """The second redaction pass — over what the ARTIST typed, not just
    what was gathered automatically. Must not send on the same click that
    discovers something to redact."""
    from houdini_agent_panel.ui import bugreport_worker as worker_module

    started = []
    monkeypatch.setattr(worker_module.BugReportWorker, "work", lambda self: started.append(True))
    view = BugReportView()
    _open(view)
    view._title_edit.setText("a real bug title")
    view._description_edit.setPlainText(
        "it happened right after I pasted ghp_abcdefghijklmnopqrstuvwxyz0123456789AB into the log"
    )

    view._on_send_clicked()

    assert started == [], "must not send on the same click that finds something to redact"
    assert "ghp_" not in view._description_edit.toPlainText()
    assert "[REDACTED]" in view._description_edit.toPlainText()
    assert "redacted" in view._status_label.text().lower()

    # Pressing Send again, now that the redacted text is what's on screen,
    # actually sends. `Worker.start()` is async — give the background
    # thread a turn to actually run the stubbed `work()`.
    view._on_send_clicked()
    worker = view._worker
    _wait_until(qapp, lambda: started)
    assert started == [True]
    if worker is not None:
        worker.wait(3000)
        qapp.processEvents()


def test_success_shows_the_issue_url(qapp, monkeypatch):
    from houdini_agent_panel.ui import bugreport_worker as worker_module

    def fake_work(self):
        self.succeeded.emit("https://github.com/MAY4VFX/houdini-agent-panel/issues/42")

    monkeypatch.setattr(worker_module.BugReportWorker, "work", fake_work)
    view = BugReportView()
    _open(view)
    view._title_edit.setText("a real bug title")
    view._description_edit.setPlainText("a description that is definitely long enough to pass")

    view._on_send_clicked()
    worker = view._worker
    _wait_until(qapp, lambda: "issues/42" in view._status_label.text())

    assert "https://github.com/MAY4VFX/houdini-agent-panel/issues/42" in view._status_label.text()
    assert view._send_button.isEnabled()
    if worker is not None:
        worker.wait(3000)
        qapp.processEvents()


def test_failure_preserves_the_typed_text_and_offers_a_copy_button(qapp, monkeypatch):
    from houdini_agent_panel.ui import bugreport_worker as worker_module
    from houdini_agent_panel.ui.worker import WorkerStopped  # noqa: F401 - not raised here, just documents the shape

    def fake_work(self):
        raise RuntimeError("Could not reach http://127.0.0.1:1/v1/reports: connection refused")

    monkeypatch.setattr(worker_module.BugReportWorker, "work", fake_work)
    view = BugReportView()
    _open(view)
    view._title_edit.setText("a real bug title")
    view._description_edit.setPlainText("a description that is definitely long enough to pass")

    view._on_send_clicked()
    worker = view._worker
    _wait_until(qapp, lambda: view._copy_button.isVisible())

    assert view._title_edit.text() == "a real bug title"
    assert "definitely long enough" in view._description_edit.toPlainText()
    assert "could not reach" in view._status_label.text().lower()
    assert view._send_button.isEnabled()
    if worker is not None:
        worker.wait(3000)
        qapp.processEvents()


def test_copy_button_puts_the_full_report_on_the_clipboard(qapp, monkeypatch):
    from houdini_agent_panel.ui import bugreport_worker as worker_module

    def fake_work(self):
        raise RuntimeError("boom")

    monkeypatch.setattr(worker_module.BugReportWorker, "work", fake_work)
    view = BugReportView()
    _open(view)
    view._title_edit.setText("copy me")
    view._description_edit.setPlainText("a description that is definitely long enough to pass")
    view._on_send_clicked()
    worker = view._worker
    from PySide6 import QtTest

    elapsed = 0
    while not view._copy_button.isVisible() and elapsed < 5000:
        qapp.processEvents()
        QtTest.QTest.qWait(20)
        elapsed += 20

    view._on_copy_clicked()

    clipboard = QtWidgets.QApplication.clipboard()
    assert "copy me" in clipboard.text()
    assert "a description that is definitely long enough" in clipboard.text()
    if worker is not None:
        worker.wait(3000)
        qapp.processEvents()

"""Tests for `ui/permissions.py::PermissionRow`. Needs `QApplication` (the `qapp` fixture)."""

from __future__ import annotations

from houdini_agent_panel.transcript_model import PermissionView
from houdini_agent_panel.ui.permissions import PermissionRow


def _view(**overrides) -> PermissionView:
    defaults = dict(
        request_key="req1",
        tool_title="rm -rf build/",
        options=[
            ("allow_once", "Allow", "allow_once"),
            ("allow_always", "Always Allow", "allow_always"),
            ("reject_once", "Reject", "reject_once"),
        ],
        answered=None,
    )
    defaults.update(overrides)
    return PermissionView(**defaults)


def test_buttons_built_in_order_from_options(qapp):
    row = PermissionRow(_view())
    assert list(row._buttons.keys()) == ["allow_once", "allow_always", "reject_once"]
    assert row.maximumWidth() == 400
    assert row.minimumWidth() == 280


def test_button_labels_match_option_names_exactly(qapp):
    row = PermissionRow(_view())
    assert row._buttons["allow_once"].text() == "Allow"
    assert row._buttons["allow_always"].text() == "Always Allow"
    assert row._buttons["reject_once"].text() == "Reject"


def test_no_extra_buttons_added(qapp):
    row = PermissionRow(_view())
    assert len(row._buttons) == 3


def test_clicking_button_emits_answered_with_matching_option_id(qapp):
    row = PermissionRow(_view())
    seen = []
    row.answered.connect(lambda key, option_id: seen.append((key, option_id)))

    row._buttons["allow_once"].click()

    assert seen == [("req1", "allow_once")]


def test_after_answering_all_buttons_are_disabled(qapp):
    row = PermissionRow(_view())
    row._buttons["reject_once"].click()

    assert all(not button.isEnabled() for button in row._buttons.values())


def test_second_click_does_not_emit_again(qapp):
    row = PermissionRow(_view())
    seen = []
    row.answered.connect(lambda key, option_id: seen.append((key, option_id)))

    row._buttons["allow_once"].click()
    # The button is disabled, but a programmatic .click() can still reach the
    # slot — check that a second answer doesn't get through logically either.
    row._on_clicked("allow_always")

    assert seen == [("req1", "allow_once")]


def test_row_constructed_already_answered_shows_history_disabled(qapp):
    row = PermissionRow(_view(answered="allow_once"))

    assert all(not button.isEnabled() for button in row._buttons.values())
    # The row isn't fully shown (it has no window parent) — a child's
    # isVisible() is always False in PySide without a shown ancestor, so we
    # check our own visibility flag via isHidden().
    assert row._status_label.isHidden() is False
    assert "Allow" in row._status_label.text()


def test_apply_view_reflects_answer_that_arrived_externally(qapp):
    row = PermissionRow(_view())
    assert row._status_label.isHidden() is True

    answered_view = _view(answered="reject_once")
    row.apply_view(answered_view)

    assert row._status_label.isHidden() is False
    assert all(not button.isEnabled() for button in row._buttons.values())


def test_apply_view_is_a_noop_when_still_unanswered(qapp):
    row = PermissionRow(_view())
    row.apply_view(_view())
    assert row._status_label.isHidden() is True
    assert all(button.isEnabled() for button in row._buttons.values())

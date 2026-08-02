"""Standalone preview is composed from real widgets and requires no Houdini."""

from __future__ import annotations

from PySide6 import QtWidgets

from houdini_agent_panel.dev_preview import PreviewPanel
from houdini_agent_panel.ui.chips import ChoiceButton


def test_preview_builds_real_panel_without_native_combobox(qapp):
    preview = PreviewPanel()
    preview.resize(900, 700)
    preview.show()
    qapp.processEvents()

    assert preview.header._rail.width() == 736
    assert preview.composer._surface.width() == 736
    anchor = preview.composer.popover_anchor_rect(preview)
    assert preview.permission.width() <= 400
    assert abs(preview.permission.geometry().center().x() - anchor.center().x()) <= 1
    assert preview.permission.geometry().bottom() < anchor.top()
    assert preview.findChildren(QtWidgets.QComboBox) == []
    assert not hasattr(preview.header, "_session_combo")
    assert [entry.kind for entry in preview._model.entries()[:3]] == [
        "user",
        "activity",
        "agent",
    ]


def test_closed_choice_controls_create_no_eager_native_popup_windows(qapp):
    """Hidden top-level Qt.Popup surfaces can flash as UI fragments on macOS."""
    preview = PreviewPanel()
    preview.resize(900, 700)
    preview.show()
    qapp.processEvents()

    choices = preview.findChildren(ChoiceButton)
    assert choices
    assert all(choice._popup is None for choice in choices)
    assert preview.header._agent_popup is None


def test_conversation_icon_opens_in_panel_drawer(qapp):
    preview = PreviewPanel()
    preview.resize(900, 700)
    preview.show()
    qapp.processEvents()

    preview.header._conversations_button.click()
    qapp.processEvents()

    assert preview.conversations.isVisible()
    assert not preview.conversations.isWindow()
    assert list(preview.conversations._buttons) == ["preview", "materials", "lighting"]

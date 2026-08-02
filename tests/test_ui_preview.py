"""Standalone preview is composed from real widgets and requires no Houdini."""

from __future__ import annotations

from PySide6 import QtWidgets

from houdini_agent_panel.dev_preview import PreviewPanel


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
    assert [entry.kind for entry in preview._model.entries()[:3]] == [
        "user",
        "activity",
        "agent",
    ]

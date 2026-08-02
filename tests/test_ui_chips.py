"""Тесты `ui/chips.py` — `HeaderBar` и `ModeChip`. Нужен `QApplication` (фикстура `qapp`)."""

from __future__ import annotations

from PySide6 import QtCore, QtWidgets

from houdini_agent_panel.sessions import SessionMode, SessionState
from houdini_agent_panel.ui.chips import HeaderBar, ModeChip


def _state(session_id: str, title: str = "Новый разговор") -> SessionState:
    return SessionState(session_id=session_id, title=title, cwd="/tmp/shot", created_at=0.0)


# --- HeaderBar -----------------------------------------------------------


def test_set_agent_sets_button_text(qapp):
    header = HeaderBar()
    header.set_agent("Claude Code", None)
    assert header._agent_button.text() == "Claude Code"


def test_header_uses_centered_precision_rail_and_no_native_combobox(qapp):
    header = HeaderBar()
    header.resize(900, 38)
    header.show()
    qapp.processEvents()

    rail_pos = header._rail.mapTo(header, QtCore.QPoint(0, 0))
    right_gutter = header.width() - rail_pos.x() - header._rail.width()
    assert header._rail.width() == 736
    assert abs(rail_pos.x() - right_gutter) <= 1
    assert header.findChildren(QtWidgets.QComboBox) == []


def test_set_cwd_sets_label_text(qapp):
    header = HeaderBar()
    header.set_cwd("/Users/artist/shot010")
    assert header._cwd_label.text() == "/Users/artist/shot010"


def test_agent_button_click_with_fewer_than_two_installed_opens_management(qapp):
    """0 or 1 installed agent: nothing to switch between, so the chip skips
    the popup and goes straight to "manage agents"."""
    header = HeaderBar()
    seen = []
    header.manage_agents_clicked.connect(lambda: seen.append(True))
    header._agent_button.click()
    assert seen == [True]

    header.set_agent_menu([("claude-acp", "Claude Agent")], "claude-acp")
    seen.clear()
    header._agent_button.click()
    assert seen == [True]
    assert not header._agent_popup.isVisible()


def test_agent_button_click_with_two_or_more_installed_opens_menu(qapp):
    header = HeaderBar()
    header.set_agent_menu(
        [("claude-acp", "Claude Agent"), ("codex-acp", "Codex")], "claude-acp"
    )

    manage_seen = []
    header.manage_agents_clicked.connect(lambda: manage_seen.append(True))
    header._agent_button.click()

    assert header._agent_popup.isVisible()
    labels = [
        header._agent_popup_layout.itemAt(i).widget().text()
        for i in range(header._agent_popup_layout.count())
        if isinstance(header._agent_popup_layout.itemAt(i).widget(), QtWidgets.QPushButton)
    ]
    assert labels == ["Claude Agent", "Codex", "Manage agents…"]
    assert manage_seen == []


def test_selecting_agent_from_menu_emits_agent_selected(qapp):
    header = HeaderBar()
    header.set_agent_menu(
        [("claude-acp", "Claude Agent"), ("codex-acp", "Codex")], "claude-acp"
    )
    header._agent_button.click()

    selected = []
    header.agent_selected.connect(selected.append)
    buttons = [
        header._agent_popup_layout.itemAt(i).widget()
        for i in range(header._agent_popup_layout.count())
        if isinstance(header._agent_popup_layout.itemAt(i).widget(), QtWidgets.QPushButton)
    ]
    codex_button = next(b for b in buttons if b.text() == "Codex")
    codex_button.click()

    assert selected == ["codex-acp"]
    assert not header._agent_popup.isVisible()


def test_selecting_manage_agents_from_menu_emits_manage_agents_clicked(qapp):
    header = HeaderBar()
    header.set_agent_menu(
        [("claude-acp", "Claude Agent"), ("codex-acp", "Codex")], "claude-acp"
    )
    header._agent_button.click()

    seen = []
    header.manage_agents_clicked.connect(lambda: seen.append(True))
    buttons = [
        header._agent_popup_layout.itemAt(i).widget()
        for i in range(header._agent_popup_layout.count())
        if isinstance(header._agent_popup_layout.itemAt(i).widget(), QtWidgets.QPushButton)
    ]
    manage_button = next(b for b in buttons if b.text() == "Manage agents…")
    manage_button.click()

    assert seen == [True]
    assert not header._agent_popup.isVisible()


def test_new_session_button_click_emits_signal(qapp):
    header = HeaderBar()
    seen = []
    header.new_session_clicked.connect(lambda: seen.append(True))
    header._new_session_button.click()
    assert seen == [True]


def test_settings_button_click_emits_signal(qapp):
    header = HeaderBar()
    seen = []
    header.settings_clicked.connect(lambda: seen.append(True))
    header._settings_button.click()
    assert seen == [True]


def test_set_sessions_populates_combo_in_order(qapp):
    header = HeaderBar()
    states = [_state("s1", "Первый"), _state("s2", "Второй"), _state("s3", "Третий")]
    header.set_sessions(states, "s2")

    combo = header._session_combo
    assert [combo.itemData(i) for i in range(combo.count())] == ["s1", "s2", "s3"]
    assert combo.currentData() == "s2"


def test_set_sessions_does_not_emit_session_selected(qapp):
    """Перестройка списка — не действие человека, сигнала быть не должно."""
    header = HeaderBar()
    seen = []
    header.session_selected.connect(seen.append)
    header.set_sessions([_state("s1"), _state("s2")], "s1")
    assert seen == []


def test_selecting_session_emits_session_selected_with_matching_id(qapp):
    header = HeaderBar()
    header.set_sessions([_state("s1"), _state("s2")], "s1")

    seen = []
    header.session_selected.connect(seen.append)
    header._session_combo.setCurrentIndex(1)
    # activated — сигнал именно о действии пользователя, эмулируем его напрямую.
    header._session_combo.activated.emit(1)

    assert seen == ["s2"]


# --- ModeChip --------------------------------------------------------------


def test_empty_modes_hides_widget_entirely(qapp):
    chip = ModeChip()
    chip.set_modes([], None)
    assert chip.isVisible() is False


def test_non_empty_modes_shows_widget_and_populates_in_order(qapp):
    chip = ModeChip()
    modes = [SessionMode(id="ask", name="Ask"), SessionMode(id="code", name="Code")]
    chip.set_modes(modes, "code")

    assert chip.isVisible() is True
    combo = chip._combo
    assert [combo.itemData(i) for i in range(combo.count())] == ["ask", "code"]
    assert combo.currentData() == "code"
    assert chip.findChildren(QtWidgets.QComboBox) == []


def test_modes_then_empty_hides_again(qapp):
    chip = ModeChip()
    chip.set_modes([SessionMode(id="ask", name="Ask")], "ask")
    assert chip.isVisible() is True

    chip.set_modes([], None)
    assert chip.isVisible() is False


def test_selecting_mode_emits_mode_selected_with_matching_id(qapp):
    chip = ModeChip()
    modes = [SessionMode(id="ask", name="Ask"), SessionMode(id="code", name="Code")]
    chip.set_modes(modes, "ask")

    seen = []
    chip.mode_selected.connect(seen.append)
    chip._combo.setCurrentIndex(1)
    chip._combo.activated.emit(1)

    assert seen == ["code"]


def test_set_modes_does_not_emit_mode_selected(qapp):
    chip = ModeChip()
    seen = []
    chip.mode_selected.connect(seen.append)
    chip.set_modes([SessionMode(id="ask", name="Ask")], "ask")
    assert seen == []

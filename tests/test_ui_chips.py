"""Тесты `ui/chips.py` — `HeaderBar` и `ModeChip`. Нужен `QApplication` (фикстура `qapp`)."""

from __future__ import annotations

from houdini_agent_panel.sessions import SessionMode, SessionState
from houdini_agent_panel.ui.chips import HeaderBar, ModeChip


def _state(session_id: str, title: str = "Новый разговор") -> SessionState:
    return SessionState(session_id=session_id, title=title, cwd="/tmp/shot", created_at=0.0)


# --- HeaderBar -----------------------------------------------------------


def test_set_agent_sets_button_text(qapp):
    header = HeaderBar()
    header.set_agent("Claude Code", None)
    assert header._agent_button.text() == "Claude Code"


def test_set_cwd_sets_label_text(qapp):
    header = HeaderBar()
    header.set_cwd("/Users/artist/shot010")
    assert header._cwd_label.text() == "/Users/artist/shot010"


def test_agent_button_click_emits_agent_clicked(qapp):
    header = HeaderBar()
    seen = []
    header.agent_clicked.connect(lambda: seen.append(True))
    header._agent_button.click()
    assert seen == [True]


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

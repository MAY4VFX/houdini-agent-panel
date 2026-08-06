"""Tests for `ui/chips.py` — `HeaderBar` and `ModeChip`. Needs `QApplication` (the `qapp` fixture)."""

from __future__ import annotations

from PySide6 import QtCore, QtGui, QtWidgets

from houdini_agent_panel.sessions import SessionMode
from houdini_agent_panel.ui import chips as chips_mod, theme
from houdini_agent_panel.ui.chips import ChoiceButton, HeaderBar, ModeChip


class _BrokenHoudiniToolButton(QtWidgets.QToolButton):
    """The fixed 36–38px sizeHint measured in the live Houdini 21 pane."""

    def sizeHint(self):  # noqa: N802
        return QtCore.QSize(38, super().sizeHint().height())


# --- HeaderBar -----------------------------------------------------------


def test_set_agent_sets_button_text(qapp):
    header = HeaderBar()
    header.set_agent("Claude Code", None)
    assert header._agent_button.text() == "Claude Code"


def test_agent_chip_sizes_for_its_text_when_houdini_style_ignores_it(qapp, monkeypatch):
    monkeypatch.setattr(chips_mod.QtWidgets, "QToolButton", _BrokenHoudiniToolButton)
    header = HeaderBar()
    header.set_agent("Claude Agent", None)

    text_width = QtGui.QFontMetrics(header._agent_button.font()).horizontalAdvance("Claude Agent")
    assert header._agent_button.sizeHint().width() >= text_width + 24
    assert header._agent_button.minimumWidth() >= text_width + 24


def test_choice_sizes_for_late_text_when_houdini_style_ignores_it(qapp, monkeypatch):
    monkeypatch.setattr(chips_mod.QtWidgets, "QToolButton", _BrokenHoudiniToolButton)
    choice = ChoiceButton()
    choice.addItem("Opus (1M context)", "opus")

    shown = choice._button.text()
    text_width = QtGui.QFontMetrics(choice._button.font()).horizontalAdvance(shown)
    assert choice._button.sizeHint().width() >= text_width + 16
    assert choice._button.minimumWidth() >= text_width + 16


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


def test_closed_native_popups_are_destroyed_not_left_as_hidden_windows(qapp):
    choice = ChoiceButton()
    choice.addItem("One", "one")
    choice._toggle_popup()
    assert choice._popup is not None and choice._popup.isVisible()
    choice._toggle_popup()
    qapp.processEvents()
    assert choice._popup is None

    header = HeaderBar()
    header.set_agent_menu([("a", "Agent A"), ("b", "Agent B")], "a")
    header._toggle_agent_popup()
    assert header._agent_popup is not None and header._agent_popup.isVisible()
    header._toggle_agent_popup()
    qapp.processEvents()
    assert header._agent_popup is None


def test_choice_button_tooltip_prefers_the_items_own_description(qapp):
    """A model choice named "Default (recommended)" names nothing on its
    own — the agent's description of what it actually is must win over the
    old "repeat the elided name" fallback."""
    choice = ChoiceButton()
    choice.addItem("Default (recommended)", "default", "Opus 5 with 1M context")
    assert choice._button.toolTip() == "Opus 5 with 1M context"


def test_choice_button_tooltip_falls_back_when_no_description(qapp):
    choice = ChoiceButton()
    choice.addItem("Short", "short")
    assert choice._button.toolTip() == ""


def test_choice_popup_is_one_line_per_choice(qapp):
    """Names only. The agent's description is real and lands in the tooltip,
    but a picker with a paragraph under every entry is a document — the
    artist asked for what Claude Code shows: four model names, no prose.
    Built it the other way twice; both were worse."""
    combo = ChoiceButton()
    combo.addItem("Opus (1M context)", "opus[1m]", "Opus 5 with 1M context · Best for everyday tasks")
    combo.addItem("Sonnet", "sonnet", "Sonnet 5 · Efficient for routine tasks")
    popup = combo._ensure_popup()
    combo._rebuild_popup()
    qapp.processEvents()

    buttons = popup.findChildren(QtWidgets.QPushButton)
    assert [b.text() for b in buttons] == ["Opus (1M context)", "Sonnet"]
    assert not popup.findChildren(QtWidgets.QLabel), "no second line may be drawn"
    assert buttons[0].toolTip().startswith("Opus 5 with 1M context"), (
        "the description is kept, just not printed under the name"
    )


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
    assert header._agent_popup is None


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


def test_conversations_button_click_emits_signal(qapp):
    header = HeaderBar()
    seen = []
    header.conversations_clicked.connect(lambda: seen.append(True))
    header._conversations_button.click()
    assert seen == [True]


def test_settings_button_click_emits_signal(qapp):
    """Direct action again, not a menu. The "⋯" briefly opened a two-item
    overflow menu (Settings, "Report a bug…") so the bug reporter had a
    header entry point too — that stopped being needed once bug reports
    moved to their own link under the composer (owner's later placement
    call), leaving Settings as this button's only job. `AgentPanel` is
    the one that turns this single click into open-or-close, comparing it
    against whether Settings is already showing (`_toggle_settings`) —
    this button only ever reports the click itself."""
    header = HeaderBar()
    seen = []
    header.settings_clicked.connect(lambda: seen.append(True))

    header._settings_button.click()

    assert seen == [True]


def test_settings_button_is_checkable_for_the_pressed_look(qapp):
    """`set_settings_open` is what `AgentPanel._show_page` drives (owner:
    the "..." that opens Settings should look pressed while it's open, so
    a second click reads as "close" rather than a guess) — this only
    confirms the button CAN show that state at all."""
    header = HeaderBar()
    assert header._settings_button.isCheckable() is True

    header.set_settings_open(True)
    assert header._settings_button.isChecked() is True

    header.set_settings_open(False)
    assert header._settings_button.isChecked() is False


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


# --- follows the live Houdini theme (no hardcoded accent) ------------------


def test_mode_chip_accent_follows_the_application_palette(qapp):
    """The mode chip's accent text used to be a fixed amber hex — it has to
    track whatever the active Houdini colour scheme's accent is instead
    (Plumtree's pink, or anything else)."""
    palette = QtGui.QPalette()
    palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor("#ff33aa"))
    qapp.setPalette(palette)

    combo = ChoiceButton(accent=True)

    assert theme.to_hex(QtGui.QColor("#ff33aa")) in combo.styleSheet()


def test_choice_popup_stylesheet_refreshes_on_show(qapp):
    """Colours are read fresh on `showEvent`, not cached from construction —
    a widget built under one palette and shown after the palette changed
    must not keep painting the old scheme."""
    combo = ChoiceButton(accent=True)

    palette = QtGui.QPalette()
    palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor("#00ffaa"))
    qapp.setPalette(palette)
    combo.show()
    qapp.processEvents()

    assert theme.to_hex(QtGui.QColor("#00ffaa")) in combo.styleSheet()


def test_agent_chip_fallback_dot_uses_the_theme_accent(qapp):
    palette = QtGui.QPalette()
    palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor("#ff33aa"))
    qapp.setPalette(palette)

    header = HeaderBar()
    header.set_agent("Some Agent", None)  # icon=None draws the fallback dot

    icon = header._agent_button.icon()
    pixmap = icon.pixmap(10, 10)
    image = pixmap.toImage()
    # Sample the dot's centre — it's drawn at (2, 2, 7, 7) in a 10x10 pixmap.
    sampled = image.pixelColor(5, 5)
    assert sampled == QtGui.QColor("#ff33aa")

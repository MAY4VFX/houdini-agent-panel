"""Precision-style header and custom choice controls.

No ``QComboBox``/``QMenu`` is used here.  Both the session picker and mode
picker render their own flat trigger and popup surface so the OS/Qt style can
never leak into the panel.
"""

from __future__ import annotations

from ..sessions import SessionMode, SessionState
from .qt import QtCore, QtGui, QtWidgets, Signal

_RAIL_WIDTH = 736
_AMBER = "#dfa047"


class ChoiceButton(QtWidgets.QWidget):
    """Small custom dropdown with a styled, non-native popup."""

    activated = Signal(int)
    currentIndexChanged = Signal(int)

    def __init__(self, parent=None, *, accent: bool = False) -> None:
        super().__init__(parent)
        self._items: list[tuple[str, object]] = []
        self._current_index = -1
        self._accent = accent

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._button = QtWidgets.QToolButton(self)
        self._button.setObjectName("choiceTriggerAccent" if accent else "choiceTrigger")
        self._button.setAutoRaise(False)
        self._button.setMinimumHeight(29 if accent else 26)
        self._button.clicked.connect(self._toggle_popup)
        layout.addWidget(self._button)

        self._popup = QtWidgets.QFrame(
            None, QtCore.Qt.Popup | QtCore.Qt.FramelessWindowHint
        )
        self._popup.setObjectName("choicePopup")
        self.destroyed.connect(self._popup.deleteLater)
        self._popup_layout = QtWidgets.QVBoxLayout(self._popup)
        self._popup_layout.setContentsMargins(5, 5, 5, 5)
        self._popup_layout.setSpacing(2)

        self.setStyleSheet(
            "QToolButton#choiceTrigger, QToolButton#choiceTriggerAccent {"
            " border: none;"
            " border-radius: 7px;"
            " background: transparent;"
            " padding: 4px 8px;"
            "}"
            "QToolButton#choiceTrigger { color: palette(disabled, text); }"
            f"QToolButton#choiceTriggerAccent {{ color: {_AMBER}; font-weight: 500; }}"
            "QToolButton#choiceTrigger:hover, QToolButton#choiceTriggerAccent:hover {"
            " background: palette(alternate-base);"
            "}"
        )
        self._popup.setStyleSheet(
            "QFrame#choicePopup {"
            " background: #262626;"
            " border: 1px solid #3a3a3a;"
            " border-radius: 10px;"
            "}"
            "QPushButton {"
            " min-height: 30px;"
            " padding: 0 10px;"
            " border: none;"
            " border-radius: 6px;"
            " color: #d7d4ce;"
            " background: transparent;"
            " text-align: left;"
            "}"
            "QPushButton:hover, QPushButton:focus { background: #333333; color: #f2efea; }"
            f"QPushButton[checkedChoice=\"true\"] {{ color: {_AMBER}; background: #332a20; }}"
        )
        self._sync_text()

    # QComboBox-like data API, intentionally tiny and fully under our control.
    def clear(self) -> None:
        self._items.clear()
        self._current_index = -1
        self._rebuild_popup()
        self._sync_text()

    def addItem(self, text: str, data: object = None) -> None:  # noqa: N802 - Qt-like API
        self._items.append((text, data))
        if self._current_index < 0:
            self._current_index = 0
        self._rebuild_popup()
        self._sync_text()

    def count(self) -> int:
        return len(self._items)

    def itemData(self, index: int):  # noqa: N802 - Qt-like API
        return self._items[index][1] if 0 <= index < len(self._items) else None

    def currentData(self):  # noqa: N802 - Qt-like API
        return self.itemData(self._current_index)

    def findData(self, data: object) -> int:  # noqa: N802 - Qt-like API
        return next((i for i, (_text, value) in enumerate(self._items) if value == data), -1)

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802 - Qt-like API
        if not 0 <= index < len(self._items) or index == self._current_index:
            return
        self._current_index = index
        self._sync_text()
        self._rebuild_popup()
        self.currentIndexChanged.emit(index)

    def _sync_text(self) -> None:
        label = self._items[self._current_index][0] if self._current_index >= 0 else ""
        self._button.setText(f"{label}  ⌄" if label else "")

    def _toggle_popup(self) -> None:
        if self._popup.isVisible():
            self._popup.hide()
            return
        self._rebuild_popup()
        width = max(self._button.width(), 180)
        self._popup.setFixedWidth(width)
        self._popup.adjustSize()
        point = self._button.mapToGlobal(QtCore.QPoint(0, self._button.height() + 5))
        screen = QtWidgets.QApplication.screenAt(point)
        below_screen = (
            screen is not None
            and point.y() + self._popup.height() > screen.availableGeometry().bottom()
        )
        if below_screen:
            point.setY(self._button.mapToGlobal(QtCore.QPoint(0, 0)).y() - self._popup.height() - 5)
        self._popup.move(point)
        self._popup.show()

    def _rebuild_popup(self) -> None:
        while self._popup_layout.count():
            item = self._popup_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for index, (label, _data) in enumerate(self._items):
            button = QtWidgets.QPushButton(label, self._popup)
            button.setProperty("checkedChoice", index == self._current_index)
            button.clicked.connect(lambda _checked=False, i=index: self._choose(i))
            self._popup_layout.addWidget(button)

    def _choose(self, index: int) -> None:
        self.setCurrentIndex(index)
        self._popup.hide()
        self.activated.emit(index)


class HeaderBar(QtWidgets.QWidget):
    """Top context rail matching ``houdini-agent-precision.html``."""

    agent_clicked = Signal()
    session_selected = Signal(str)
    new_session_clicked = Signal()
    settings_clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(38)
        outer = QtWidgets.QHBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setAlignment(QtCore.Qt.AlignHCenter)

        self._rail = QtWidgets.QWidget(self)
        self._rail.setFixedHeight(38)
        layout = QtWidgets.QHBoxLayout(self._rail)
        layout.setContentsMargins(0, 5, 0, 5)
        layout.setSpacing(5)
        outer.addWidget(self._rail)

        self._agent_button = QtWidgets.QToolButton(self._rail)
        self._agent_button.setObjectName("contextButton")
        self._agent_button.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self._agent_button.clicked.connect(self.agent_clicked)
        layout.addWidget(self._agent_button)

        self._divider = QtWidgets.QLabel("·", self._rail)
        self._divider.setObjectName("contextDivider")
        layout.addWidget(self._divider)

        self._cwd_label = QtWidgets.QLabel(self._rail)
        self._cwd_label.setObjectName("contextPath")
        self._cwd_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        layout.addWidget(self._cwd_label)
        layout.addStretch(1)

        self._session_combo = ChoiceButton(self._rail)
        self._session_combo.activated.connect(self._on_session_activated)
        layout.addWidget(self._session_combo)

        self._new_session_button = QtWidgets.QToolButton(self._rail)
        self._new_session_button.setObjectName("contextIcon")
        self._new_session_button.setText("+")
        self._new_session_button.setToolTip("Новый разговор")
        self._new_session_button.clicked.connect(self.new_session_clicked)
        layout.addWidget(self._new_session_button)

        self._settings_button = QtWidgets.QToolButton(self._rail)
        self._settings_button.setObjectName("contextIcon")
        self._settings_button.setText("⋯")
        self._settings_button.setToolTip("Настройки")
        self._settings_button.clicked.connect(self.settings_clicked)
        layout.addWidget(self._settings_button)

        self.setStyleSheet(
            "QToolButton#contextButton, QToolButton#contextIcon {"
            " min-height: 26px; border: none; border-radius: 6px;"
            " color: palette(disabled, text); background: transparent; padding: 0 7px;"
            "}"
            "QToolButton#contextButton:hover, QToolButton#contextIcon:hover {"
            " color: palette(text); background: palette(alternate-base);"
            "}"
            "QLabel#contextPath { color: palette(disabled, text); padding: 0 7px; }"
            "QLabel#contextDivider { color: palette(mid); }"
        )

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._rail.setFixedWidth(min(_RAIL_WIDTH, max(0, self.width() - 28)))

    def set_agent(self, name: str, icon: QtGui.QIcon | None) -> None:
        self._agent_button.setText(name)
        if icon is None:
            dot = QtGui.QPixmap(10, 10)
            dot.fill(QtCore.Qt.transparent)
            painter = QtGui.QPainter(dot)
            painter.setRenderHint(QtGui.QPainter.Antialiasing)
            painter.setBrush(QtGui.QColor(_AMBER))
            painter.setPen(QtCore.Qt.NoPen)
            painter.drawEllipse(2, 2, 7, 7)
            painter.end()
            icon = QtGui.QIcon(dot)
        self._agent_button.setIcon(icon)

    def set_cwd(self, path: str) -> None:
        self._cwd_label.setText(path)
        self._cwd_label.setToolTip(path)

    def set_sessions(self, states: list[SessionState], current: str | None) -> None:
        self._session_combo.blockSignals(True)
        try:
            self._session_combo.clear()
            for state in states:
                self._session_combo.addItem(state.title, state.session_id)
            index = self._session_combo.findData(current)
            if index >= 0:
                self._session_combo.setCurrentIndex(index)
        finally:
            self._session_combo.blockSignals(False)

    def _on_session_activated(self, index: int) -> None:
        session_id = self._session_combo.itemData(index)
        if session_id:
            self.session_selected.emit(session_id)


class ModeChip(QtWidgets.QWidget):
    mode_selected = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._combo = ChoiceButton(self, accent=True)
        self._combo.activated.connect(self._on_activated)
        layout.addWidget(self._combo)
        self.setVisible(False)

    def set_modes(self, modes: list[SessionMode], current_id: str | None) -> None:
        self._combo.blockSignals(True)
        try:
            self._combo.clear()
            for mode in modes:
                self._combo.addItem(mode.name, mode.id)
            index = self._combo.findData(current_id)
            if index >= 0:
                self._combo.setCurrentIndex(index)
        finally:
            self._combo.blockSignals(False)
        self.setVisible(bool(modes))

    def _on_activated(self, index: int) -> None:
        mode_id = self._combo.itemData(index)
        if mode_id:
            self.mode_selected.emit(mode_id)


__all__ = ["ChoiceButton", "HeaderBar", "ModeChip"]

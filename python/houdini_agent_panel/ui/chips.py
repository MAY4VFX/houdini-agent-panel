"""Precision-style header and custom choice controls.

No ``QComboBox``/``QMenu`` is used here.  The session picker, mode picker,
and the agent chip's switcher menu all render their own flat trigger and
popup surface so the OS/Qt style can never leak into the panel.
"""

from __future__ import annotations

from ..sessions import SessionMode
from .conversations import sidebar_icon
from .qt import QtCore, QtGui, QtWidgets, Signal

_RAIL_WIDTH = 736
#: Floor for the centered rail. Without it — and without the
#: `minimumSizeHint` override below — the rail's `setFixedWidth` became the
#: header's minimum, the header's minimum became the panel's, and the panel
#: could not be docked into any Houdini pane narrower than 736px.
_MIN_RAIL_WIDTH = 180
_AMBER = "#dfa047"


class ChoiceButton(QtWidgets.QWidget):
    """Small custom dropdown with a styled, non-native popup."""

    activated = Signal(int)
    currentIndexChanged = Signal(int)

    def __init__(self, parent=None, *, accent: bool = False, show_caret: bool = True) -> None:
        super().__init__(parent)
        self._items: list[tuple[str, object]] = []
        self._current_index = -1
        self._accent = accent
        self._show_caret = show_caret

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._button = QtWidgets.QToolButton(self)
        self._button.setObjectName("choiceTriggerAccent" if accent else "choiceTrigger")
        self._button.setAutoRaise(False)
        self._button.setMinimumHeight(29 if accent else 26)
        self._button.clicked.connect(self._toggle_popup)
        layout.addWidget(self._button)

        # A hidden Qt.Popup is still a native top-level surface.  Creating
        # several eagerly makes macOS occasionally composite one for a frame
        # during activation/re-layout.  It must not exist until the click.
        self._popup: QtWidgets.QFrame | None = None
        self._popup_layout: QtWidgets.QVBoxLayout | None = None

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
        self._popup_stylesheet = (
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

    def _ensure_popup(self) -> QtWidgets.QFrame:
        if self._popup is not None:
            return self._popup
        popup = QtWidgets.QFrame(None, QtCore.Qt.Popup | QtCore.Qt.FramelessWindowHint)
        popup.setObjectName("choicePopup")
        popup.setStyleSheet(self._popup_stylesheet)
        popup.installEventFilter(self)
        layout = QtWidgets.QVBoxLayout(popup)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(2)
        self._popup = popup
        self._popup_layout = layout
        return popup

    def eventFilter(self, watched, event):  # noqa: N802 - Qt override
        if watched is self._popup and event.type() == QtCore.QEvent.Hide:
            QtCore.QTimer.singleShot(0, self._release_popup)
        return super().eventFilter(watched, event)

    def _release_popup(self) -> None:
        popup = self._popup
        if popup is None or popup.isVisible():
            return
        self._popup = None
        self._popup_layout = None
        popup.deleteLater()

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
        if not label:
            self._button.setText("")
        elif self._show_caret:
            self._button.setText(f"{label}  ⌄")
        else:
            self._button.setText(label)

    def _toggle_popup(self) -> None:
        popup = self._ensure_popup()
        if popup.isVisible():
            popup.hide()
            return
        self._rebuild_popup()
        width = max(self._button.width(), 180)
        popup.setFixedWidth(width)
        popup.adjustSize()
        point = self._button.mapToGlobal(QtCore.QPoint(0, self._button.height() + 5))
        screen = QtWidgets.QApplication.screenAt(point)
        below_screen = (
            screen is not None
            and point.y() + popup.height() > screen.availableGeometry().bottom()
        )
        if below_screen:
            point.setY(self._button.mapToGlobal(QtCore.QPoint(0, 0)).y() - popup.height() - 5)
        popup.move(point)
        popup.show()

    def _rebuild_popup(self) -> None:
        if self._popup is None or self._popup_layout is None:
            return
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
        if self._popup is not None:
            self._popup.hide()
        self.activated.emit(index)


class _ElidedLabel(QtWidgets.QLabel):
    """A label that gives up width instead of pushing its neighbours away.

    A plain `QLabel` demands enough room for its whole string, and a project
    path is long: the header's "+" and "⋯" buttons were the ones paying for
    it. This one keeps the full text (so `text()` and the tooltip still tell
    the truth) and elides at paint time — from the left, because the tail of
    a $HIP path, the shot folder, is the part worth reading.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)

    def minimumSizeHint(self) -> QtCore.QSize:  # noqa: N802 - Qt override
        return QtCore.QSize(0, super().minimumSizeHint().height())

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802 - Qt override
        del event
        painter = QtGui.QPainter(self)
        area = self.contentsRect()
        metrics = QtGui.QFontMetrics(self.font())
        text = metrics.elidedText(self.text(), QtCore.Qt.ElideLeft, area.width())
        painter.setPen(self.palette().color(QtGui.QPalette.WindowText))
        painter.drawText(area, int(QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter), text)


class HeaderBar(QtWidgets.QWidget):
    """Top context rail matching ``houdini-agent-precision.html``."""

    manage_agents_clicked = Signal()
    sign_in_clicked = Signal()
    agent_selected = Signal(str)
    conversations_clicked = Signal()
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

        self._conversations_button = QtWidgets.QToolButton(self._rail)
        self._conversations_button.setObjectName("contextIcon")
        self._conversations_button.setIcon(sidebar_icon())
        self._conversations_button.setIconSize(QtCore.QSize(18, 18))
        self._conversations_button.setToolTip("Conversations")
        self._conversations_button.clicked.connect(self.conversations_clicked)
        layout.addWidget(self._conversations_button)

        self._agent_button = QtWidgets.QToolButton(self._rail)
        self._agent_button.setObjectName("contextButton")
        self._agent_button.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self._agent_button.clicked.connect(self._on_agent_button_clicked)
        layout.addWidget(self._agent_button)

        self._divider = QtWidgets.QLabel("·", self._rail)
        self._divider.setObjectName("contextDivider")
        layout.addWidget(self._divider)

        self._cwd_label = _ElidedLabel(self._rail)
        self._cwd_label.setObjectName("contextPath")
        # Margins on the widget, not `padding:` in the stylesheet — the custom
        # paintEvent draws into contentsRect(), which only the former moves.
        self._cwd_label.setContentsMargins(7, 0, 7, 0)
        # Colour set on the palette rather than through the stylesheet: the
        # label paints its own (elided) text, and a stylesheet `color:` never
        # reaches a custom paintEvent.
        cwd_palette = self._cwd_label.palette()
        cwd_palette.setColor(
            QtGui.QPalette.WindowText,
            cwd_palette.color(QtGui.QPalette.Disabled, QtGui.QPalette.Text),
        )
        self._cwd_label.setPalette(cwd_palette)
        layout.addWidget(self._cwd_label, 1)
        layout.addStretch(1)

        self._new_conversation_button = QtWidgets.QToolButton(self._rail)
        self._new_conversation_button.setObjectName("contextIcon")
        self._new_conversation_button.setText("+")
        self._new_conversation_button.setToolTip("New conversation")
        self._new_conversation_button.clicked.connect(self.new_session_clicked)
        layout.addWidget(self._new_conversation_button)

        self._settings_button = QtWidgets.QToolButton(self._rail)
        self._settings_button.setObjectName("contextIcon")
        self._settings_button.setText("⋯")
        self._settings_button.setToolTip("Settings")
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
            "QLabel#contextDivider { color: palette(mid); }"
        )

        # Installed agents fed by the panel (see `AgentPanel._refresh_agent_chip_menu`).
        # Empty until the panel's first boot pass — the chip just opens
        # settings until then, same as the "0 or 1 installed" case below.
        self._agent_items: list[tuple[str, str]] = []
        self._can_sign_in = False
        self._agent_current_id: str | None = None

        self._agent_popup: QtWidgets.QFrame | None = None
        self._agent_popup_layout: QtWidgets.QVBoxLayout | None = None
        self._agent_popup_stylesheet = (
            "QFrame#agentPopup {"
            " background: #262626;"
            " border: 1px solid #3a3a3a;"
            " border-radius: 10px;"
            "}"
            "QFrame#agentMenuSeparator {"
            " background: #3a3a3a; max-height: 1px; min-height: 1px; margin: 4px 6px;"
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

    def _ensure_agent_popup(self) -> QtWidgets.QFrame:
        if self._agent_popup is not None:
            return self._agent_popup
        popup = QtWidgets.QFrame(None, QtCore.Qt.Popup | QtCore.Qt.FramelessWindowHint)
        popup.setObjectName("agentPopup")
        popup.setStyleSheet(self._agent_popup_stylesheet)
        popup.installEventFilter(self)
        layout = QtWidgets.QVBoxLayout(popup)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(2)
        self._agent_popup = popup
        self._agent_popup_layout = layout
        return popup

    def eventFilter(self, watched, event):  # noqa: N802 - Qt override
        if watched is self._agent_popup and event.type() == QtCore.QEvent.Hide:
            QtCore.QTimer.singleShot(0, self._release_agent_popup)
        return super().eventFilter(watched, event)

    def _release_agent_popup(self) -> None:
        popup = self._agent_popup
        if popup is None or popup.isVisible():
            return
        self._agent_popup = None
        self._agent_popup_layout = None
        popup.deleteLater()

    def minimumSizeHint(self) -> QtCore.QSize:  # noqa: N802 - Qt override
        hint = super().minimumSizeHint()
        return QtCore.QSize(min(hint.width(), _MIN_RAIL_WIDTH), hint.height())

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._rail.setFixedWidth(max(_MIN_RAIL_WIDTH, min(_RAIL_WIDTH, self.width() - 28)))

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

    def set_agent_menu(self, agents: list[tuple[str, str]], current_id: str | None) -> None:
        """Feed the chip the list of installed agents, as ``(agent_id, label)``.

        With fewer than two installed agents there is nothing to switch
        between, so clicking the chip skips the popup entirely and goes
        straight to "manage agents" — the same "agent can't do it, no
        control gets drawn" rule the rest of the panel follows, applied to
        the switcher itself.
        """
        self._agent_items = list(agents)
        self._agent_current_id = current_id
        self._rebuild_agent_popup()

    def set_cwd(self, path: str) -> None:
        self._cwd_label.setText(path)
        self._cwd_label.setToolTip(path)

    # --- agent chip menu -------------------------------------------------

    def _rebuild_agent_popup(self) -> None:
        if self._agent_popup is None or self._agent_popup_layout is None:
            return
        while self._agent_popup_layout.count():
            item = self._agent_popup_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        for agent_id, label in self._agent_items:
            button = QtWidgets.QPushButton(label, self._agent_popup)
            button.setProperty("checkedChoice", agent_id == self._agent_current_id)
            button.clicked.connect(lambda _checked=False, a=agent_id: self._choose_agent(a))
            self._agent_popup_layout.addWidget(button)
        if self._agent_items:
            separator = QtWidgets.QFrame(self._agent_popup)
            separator.setObjectName("agentMenuSeparator")
            separator.setFrameShape(QtWidgets.QFrame.HLine)
            self._agent_popup_layout.addWidget(separator)
        if self._can_sign_in:
            # Signing in must not depend on the agent asking first. Grok
            # accepts a session and only complains to its own stderr when it
            # turns out it isn't authorized, so waiting for `auth_required`
            # left no way in at all.
            sign_in_button = QtWidgets.QPushButton("Sign in…", self._agent_popup)
            sign_in_button.clicked.connect(self._choose_sign_in)
            self._agent_popup_layout.addWidget(sign_in_button)

        manage_button = QtWidgets.QPushButton("Manage agents…", self._agent_popup)
        manage_button.clicked.connect(self._choose_manage)
        self._agent_popup_layout.addWidget(manage_button)

    def _on_agent_button_clicked(self) -> None:
        if len(self._agent_items) < 2:
            self.manage_agents_clicked.emit()
            return
        self._toggle_agent_popup()

    def _toggle_agent_popup(self) -> None:
        popup = self._ensure_agent_popup()
        if popup.isVisible():
            popup.hide()
            return
        self._rebuild_agent_popup()
        width = max(self._agent_button.width(), 200)
        popup.setFixedWidth(width)
        popup.adjustSize()
        point = self._agent_button.mapToGlobal(QtCore.QPoint(0, self._agent_button.height() + 5))
        screen = QtWidgets.QApplication.screenAt(point)
        below_screen = (
            screen is not None
            and point.y() + popup.height() > screen.availableGeometry().bottom()
        )
        if below_screen:
            point.setY(
                self._agent_button.mapToGlobal(QtCore.QPoint(0, 0)).y()
                - popup.height()
                - 5
            )
        popup.move(point)
        popup.show()

    def set_can_sign_in(self, can_sign_in: bool) -> None:
        """Whether this agent declared any way to sign in at all."""
        self._can_sign_in = bool(can_sign_in)

    def _choose_sign_in(self) -> None:
        self._agent_popup.hide()
        self.sign_in_clicked.emit()

    def _choose_agent(self, agent_id: str) -> None:
        if self._agent_popup is not None:
            self._agent_popup.hide()
        self.agent_selected.emit(agent_id)

    def _choose_manage(self) -> None:
        if self._agent_popup is not None:
            self._agent_popup.hide()
        self.manage_agents_clicked.emit()


class ModeChip(QtWidgets.QWidget):
    mode_selected = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        # No caret: this chip reads as a status pill (current mode), not a
        # dropdown affordance — the mode picker inside it is a bonus, not
        # the point.
        self._combo = ChoiceButton(self, accent=True, show_caret=False)
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

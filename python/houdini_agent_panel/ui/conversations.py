"""In-panel conversation drawer; never a native popup/window."""

from __future__ import annotations

from ..sessions import SessionState
from .qt import QtCore, QtGui, QtWidgets, Signal

_DRAWER_WIDTH = 286


def sidebar_icon() -> QtGui.QIcon:
    """Small split-panel glyph matching modern conversation sidebars."""
    pixmap = QtGui.QPixmap(18, 18)
    pixmap.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
    pen = QtGui.QPen(QtGui.QColor("#aaa7a1"), 1.2)
    painter.setPen(pen)
    painter.setBrush(QtCore.Qt.NoBrush)
    painter.drawRoundedRect(QtCore.QRectF(2.25, 3.25, 13.5, 11.5), 2.0, 2.0)
    painter.drawLine(QtCore.QPointF(7.0, 3.5), QtCore.QPointF(7.0, 14.5))
    painter.end()
    return QtGui.QIcon(pixmap)


class ConversationDrawer(QtWidgets.QFrame):
    session_selected = Signal(str)
    new_session_clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("conversationDrawer")
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self.setFixedWidth(_DRAWER_WIDTH)
        self.hide()

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)

        top = QtWidgets.QHBoxLayout()
        self._close_button = QtWidgets.QToolButton(self)
        self._close_button.setObjectName("drawerIcon")
        self._close_button.setIcon(sidebar_icon())
        self._close_button.setIconSize(QtCore.QSize(18, 18))
        self._close_button.setToolTip("Close conversations")
        self._close_button.clicked.connect(self.close_drawer)
        top.addWidget(self._close_button)
        top.addStretch(1)
        layout.addLayout(top)

        self._new_button = QtWidgets.QPushButton("＋  New conversation", self)
        self._new_button.setObjectName("newConversation")
        self._new_button.clicked.connect(self._on_new_session)
        layout.addWidget(self._new_button)

        heading = QtWidgets.QLabel("Conversations", self)
        heading.setObjectName("drawerHeading")
        layout.addWidget(heading)

        scroll = QtWidgets.QScrollArea(self)
        scroll.setObjectName("drawerScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self._content = QtWidgets.QWidget(scroll)
        self._sessions_layout = QtWidgets.QVBoxLayout(self._content)
        self._sessions_layout.setContentsMargins(0, 0, 0, 0)
        self._sessions_layout.setSpacing(3)
        self._sessions_layout.addStretch(1)
        scroll.setWidget(self._content)
        layout.addWidget(scroll, 1)

        self._buttons: dict[str, QtWidgets.QPushButton] = {}
        self._current_id: str | None = None
        self._animation = QtCore.QPropertyAnimation(self, b"pos", self)
        self._animation.setDuration(170)
        self._animation.setEasingCurve(QtCore.QEasingCurve.OutCubic)
        self._animation.finished.connect(self._on_animation_finished)
        self._closing = False

        self.setStyleSheet(
            "QFrame#conversationDrawer {"
            " background: palette(window);"
            " border: none; border-right: 1px solid palette(mid);"
            "}"
            "QToolButton#drawerIcon {"
            " min-width: 28px; min-height: 28px; border: none; border-radius: 7px;"
            " color: palette(disabled, text); background: transparent;"
            "}"
            "QToolButton#drawerIcon:hover { background: palette(alternate-base); color: palette(text); }"
            "QPushButton#newConversation {"
            " min-height: 34px; border: none; border-radius: 8px; padding: 0 9px;"
            " text-align: left; color: palette(text); background: transparent;"
            "}"
            "QPushButton#newConversation:hover { background: palette(alternate-base); }"
            "QLabel#drawerHeading { color: palette(disabled, text); padding: 12px 8px 4px 8px; }"
            "QScrollArea#drawerScroll { background: transparent; border: none; }"
            "QScrollArea#drawerScroll > QWidget > QWidget { background: transparent; }"
            "QPushButton[conversation=\"true\"] {"
            " min-height: 34px; border: none; border-radius: 8px; padding: 0 9px;"
            " text-align: left; color: palette(disabled, text); background: transparent;"
            "}"
            "QPushButton[conversation=\"true\"]:hover {"
            " color: palette(text); background: palette(alternate-base);"
            "}"
            "QPushButton[currentConversation=\"true\"] {"
            " color: palette(text); background: palette(alternate-base);"
            "}"
        )

    def set_sessions(self, states: list[SessionState], current_id: str | None) -> None:
        self._current_id = current_id
        while self._sessions_layout.count() > 1:
            item = self._sessions_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._buttons.clear()
        for state in reversed(states):
            title = (state.title or "New conversation").splitlines()[0]
            button = QtWidgets.QPushButton(title, self._content)
            button.setProperty("conversation", True)
            button.setProperty("currentConversation", state.session_id == current_id)
            button.setToolTip(state.title or "New conversation")
            button.clicked.connect(
                lambda _checked=False, sid=state.session_id: self._select_session(sid)
            )
            self._buttons[state.session_id] = button
            self._sessions_layout.insertWidget(self._sessions_layout.count() - 1, button)

    def toggle(self) -> None:
        if self.isVisible() and not self._closing:
            self.close_drawer()
        else:
            self.open_drawer()

    def open_drawer(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        self._animation.stop()
        self._closing = False
        self.setFixedHeight(parent.height())
        self.move(-self.width(), 0)
        self.show()
        self.raise_()
        self._animation.setStartValue(QtCore.QPoint(-self.width(), 0))
        self._animation.setEndValue(QtCore.QPoint(0, 0))
        self._animation.start()

    def close_drawer(self) -> None:
        # ``isVisible`` is false while an owning test/host panel is itself
        # hidden, even though the drawer's explicit state is shown.  The
        # drawer state, not the ancestor's exposure, decides whether closing
        # should start.
        if self.isHidden():
            return
        self._animation.stop()
        self._closing = True
        self._animation.setStartValue(self.pos())
        self._animation.setEndValue(QtCore.QPoint(-self.width(), 0))
        self._animation.start()

    def sync_parent_geometry(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        self.setFixedHeight(parent.height())
        if self.isVisible() and self._animation.state() != QtCore.QAbstractAnimation.Running:
            self.move(-self.width() if self._closing else 0, 0)

    def _select_session(self, session_id: str) -> None:
        self.session_selected.emit(session_id)
        self.close_drawer()

    def _on_new_session(self) -> None:
        self.new_session_clicked.emit()
        self.close_drawer()

    def _on_animation_finished(self) -> None:
        if self._closing:
            self.hide()
            self._closing = False


__all__ = ["ConversationDrawer", "sidebar_icon"]

"""In-panel conversation drawer; never a native popup/window."""

from __future__ import annotations

from ..sessions import SessionState
from .qt import QtCore, QtGui, QtWidgets, Signal

_DRAWER_WIDTH = 286
_AMBER = "#dfa047"

#: What's left for the title once the drawer's own margins and the pin/more
#: icon buttons take their share. `QPushButton` doesn't elide overflowing
#: text on its own — without this a long first message pushed the pin and
#: overflow buttons straight out of the (non-scrolling) drawer, off screen.
_TITLE_MAX_WIDTH = 190

#: Diameter of the busy/unread markers on a conversation row.
_DOT_SIZE = 8


def _dot_pixmap(color: QtGui.QColor, size: int = 8) -> QtGui.QPixmap:
    """A small filled circle — the sidebar's busy/unread markers.

    Both share this one shape and the same accent colour; what tells them
    apart is where they sit on the row (see `_build_row`), not a second
    colour — one accent already means "look here" everywhere else in the
    panel (the mode chip, the pin icon).
    """
    pixmap = QtGui.QPixmap(size, size)
    pixmap.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pixmap)
    painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
    painter.setBrush(color)
    painter.setPen(QtCore.Qt.NoPen)
    painter.drawEllipse(0, 0, size, size)
    painter.end()
    return pixmap


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


def summarize_title(text: str, limit: int = 60) -> str:
    """First line of a human message, cut at a word boundary within `limit`.

    A hard `text[:limit]` slice can chop a word in half, which reads like a
    typo in the sidebar. Cutting back to the last space before the limit
    keeps every visible word whole; an ellipsis marks that it was cut.
    """
    first_line = text.strip().splitlines()[0].strip() if text.strip() else ""
    if not first_line:
        return "New conversation"
    if len(first_line) <= limit:
        return first_line
    truncated = first_line[:limit]
    cut = truncated.rfind(" ")
    if cut > 0:
        truncated = truncated[:cut]
    return truncated.rstrip() + "…"


_ROW_MENU_STYLESHEET = (
    "QFrame#rowMenu {"
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
    "QPushButton#rowMenuDelete:hover { background: #3a2323; color: #e3a3a3; }"
)


class ConversationDrawer(QtWidgets.QFrame):
    """Slides in from the left, under the header.

    Two geometry rules, both learned from the panel looking broken with the
    drawer open. It starts BELOW the header (`set_top_inset`), because the
    only control that closes it again is the header's own sidebar toggle —
    a drawer covering its own toggle is a drawer you cannot close. And it
    reports its state through `open_state_changed` so the panel can move the
    conversation column out from under it instead of letting it cover the
    text the artist is reading.
    """

    session_selected = Signal(str)
    session_renamed = Signal(str, str)
    session_removed = Signal(str)
    new_session_clicked = Signal()
    #: True when the drawer starts opening, False when it starts closing.
    #: Fires on the way in/out, not on arrival — the panel reserves space for
    #: it before the slide, so the content never has to be redrawn mid-animation.
    open_state_changed = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("conversationDrawer")
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self.setFixedWidth(_DRAWER_WIDTH)
        self._top = 0
        self.hide()

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 12)
        layout.setSpacing(8)

        # No collapse button in here any more: the header's own sidebar
        # toggle (ui/chips.py `HeaderBar._conversations_button`) already
        # opens and closes this drawer, and it stays reachable even while
        # the drawer is closed. A second copy of the same icon a couple of
        # centimeters away, only usable while the drawer happens to be open,
        # was a redundant control, not a second way in.
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
        self._pin_buttons: dict[str, QtWidgets.QToolButton] = {}
        self._busy_dots: dict[str, QtWidgets.QLabel] = {}
        self._unread_dots: dict[str, QtWidgets.QLabel] = {}
        self._states: dict[str, SessionState] = {}
        self._pinned: set[str] = set()
        self._current_id: str | None = None
        self._active_row_menu: QtWidgets.QFrame | None = None
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
            "QToolButton#rowPin, QToolButton#rowMore {"
            " min-width: 22px; max-width: 22px; min-height: 22px; border: none;"
            " border-radius: 6px; color: palette(disabled, text); background: transparent;"
            " padding: 0;"
            "}"
            "QToolButton#rowPin:hover, QToolButton#rowMore:hover {"
            " color: palette(text); background: palette(alternate-base);"
            "}"
            f"QToolButton#rowPin[pinned=\"true\"] {{ color: {_AMBER}; }}"
        )

    def set_sessions(self, states: list[SessionState], current_id: str | None) -> None:
        self._current_id = current_id
        self._states = {state.session_id: state for state in states}
        # A pinned session that no longer exists (deleted elsewhere) has
        # nothing left to point at — drop it so the set doesn't grow forever.
        self._pinned &= self._states.keys()
        while self._sessions_layout.count() > 1:
            item = self._sessions_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # `takeAt` only stops the layout from managing it — the
                # widget itself stays visible at its old geometry until
                # `deleteLater` actually runs on a later event-loop pass.
                # Two rebuilds in the same tick (pin toggle right after the
                # initial `set_sessions`) used to show the old row bleeding
                # through the new one until then.
                widget.hide()
                widget.deleteLater()
        self._buttons.clear()
        self._pin_buttons.clear()
        self._busy_dots.clear()
        self._unread_dots.clear()
        ordered = sorted(
            states, key=lambda s: (s.session_id not in self._pinned, -s.created_at)
        )
        for state in ordered:
            row = self._build_row(state)
            self._sessions_layout.insertWidget(self._sessions_layout.count() - 1, row)

    def _build_row(self, state: SessionState) -> QtWidgets.QWidget:
        title = summarize_title(state.title) if state.title else "New conversation"
        row = QtWidgets.QWidget(self._content)
        row_layout = QtWidgets.QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(2)

        # Busy — the agent is working on THIS conversation right now, even
        # though it isn't the one on screen. Ahead of the title: it's the
        # first thing that should catch the eye scanning down the list.
        busy_dot = QtWidgets.QLabel(row)
        busy_dot.setObjectName("rowBusyDot")
        busy_dot.setFixedSize(_DOT_SIZE + 4, _DOT_SIZE + 4)
        busy_dot.setAlignment(QtCore.Qt.AlignCenter)
        busy_dot.setPixmap(_dot_pixmap(QtGui.QColor(_AMBER)))
        busy_dot.setToolTip("The agent is working on this conversation")
        busy_dot.setVisible(state.busy)
        row_layout.addWidget(busy_dot)

        button = QtWidgets.QPushButton(row)
        button.setProperty("conversation", True)
        button.setProperty("currentConversation", state.session_id == self._current_id)
        button.setToolTip(state.title or "New conversation")
        metrics = QtGui.QFontMetrics(button.font())
        button.setText(metrics.elidedText(title, QtCore.Qt.ElideRight, _TITLE_MAX_WIDTH))
        button.clicked.connect(
            lambda _checked=False, sid=state.session_id: self._select_session(sid)
        )
        row_layout.addWidget(button, 1)

        # Unread — a reply landed while this conversation wasn't open.
        # Separate from "busy": a session can finish a turn and sit unread
        # long after the agent stopped working on it. Cleared the moment the
        # artist opens it (`AgentPanel._show_session`), never here.
        unread_dot = QtWidgets.QLabel(row)
        unread_dot.setObjectName("rowUnreadDot")
        unread_dot.setFixedSize(_DOT_SIZE + 4, _DOT_SIZE + 4)
        unread_dot.setAlignment(QtCore.Qt.AlignCenter)
        unread_dot.setPixmap(_dot_pixmap(QtGui.QColor(_AMBER)))
        unread_dot.setToolTip("Unread reply")
        unread_dot.setVisible(state.unread)
        row_layout.addWidget(unread_dot)

        pinned = state.session_id in self._pinned
        pin_button = QtWidgets.QToolButton(row)
        pin_button.setObjectName("rowPin")
        pin_button.setProperty("pinned", pinned)
        pin_button.setText("⚑" if pinned else "⚐")
        pin_button.setToolTip("Unpin" if pinned else "Pin")
        pin_button.clicked.connect(
            lambda _checked=False, sid=state.session_id: self._toggle_pin(sid)
        )
        row_layout.addWidget(pin_button)

        more_button = QtWidgets.QToolButton(row)
        more_button.setObjectName("rowMore")
        more_button.setText("⋯")
        more_button.setToolTip("More")
        more_button.clicked.connect(
            lambda _checked=False, b=more_button, sid=state.session_id: self._open_row_menu(b, sid)
        )
        row_layout.addWidget(more_button)

        self._buttons[state.session_id] = button
        self._pin_buttons[state.session_id] = pin_button
        self._busy_dots[state.session_id] = busy_dot
        self._unread_dots[state.session_id] = unread_dot
        return row

    def set_top_inset(self, top: int) -> None:
        """Where the drawer's top edge sits — right under the panel header.

        Anything above stays visible and clickable, which is what keeps the
        header's sidebar toggle reachable while the drawer is open.
        """
        top = max(0, int(top))
        if top == self._top:
            return
        self._top = top
        self.sync_parent_geometry()

    def is_open(self) -> bool:
        return not self.isHidden() and not self._closing

    def toggle(self) -> None:
        if self.is_open():
            self.close_drawer()
        else:
            self.open_drawer()

    def open_drawer(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        self._animation.stop()
        self._closing = False
        self.setFixedHeight(max(0, parent.height() - self._top))
        self.move(-self.width(), self._top)
        self.show()
        self.raise_()
        self.open_state_changed.emit(True)
        self._animation.setStartValue(QtCore.QPoint(-self.width(), self._top))
        self._animation.setEndValue(QtCore.QPoint(0, self._top))
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
        self.open_state_changed.emit(False)
        self._animation.setStartValue(self.pos())
        self._animation.setEndValue(QtCore.QPoint(-self.width(), self._top))
        self._animation.start()

    def sync_parent_geometry(self) -> None:
        parent = self.parentWidget()
        if parent is None:
            return
        self.setFixedHeight(max(0, parent.height() - self._top))
        if self.isVisible() and self._animation.state() != QtCore.QAbstractAnimation.Running:
            self.move(-self.width() if self._closing else 0, self._top)

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

    # --- pin / rename / delete ---------------------------------------------

    def _toggle_pin(self, session_id: str) -> None:
        if session_id in self._pinned:
            self._pinned.discard(session_id)
        else:
            self._pinned.add(session_id)
        self.set_sessions(list(self._states.values()), self._current_id)

    def _open_row_menu(self, anchor: QtWidgets.QToolButton, session_id: str) -> None:
        state = self._states.get(session_id)
        title = state.title if state is not None else ""
        menu = QtWidgets.QFrame(None, QtCore.Qt.Popup | QtCore.Qt.FramelessWindowHint)
        menu.setObjectName("rowMenu")
        menu.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
        menu.setStyleSheet(_ROW_MENU_STYLESHEET)
        menu_layout = QtWidgets.QVBoxLayout(menu)
        menu_layout.setContentsMargins(5, 5, 5, 5)
        menu_layout.setSpacing(2)

        rename_button = QtWidgets.QPushButton("Rename…", menu)
        delete_button = QtWidgets.QPushButton("Delete", menu)
        delete_button.setObjectName("rowMenuDelete")
        menu_layout.addWidget(rename_button)
        menu_layout.addWidget(delete_button)

        rename_button.clicked.connect(lambda: (menu.close(), self._start_rename(session_id, title)))
        delete_button.clicked.connect(lambda: (menu.close(), self._confirm_delete(session_id, title)))

        width = max(anchor.width(), 150)
        menu.setFixedWidth(width)
        menu.adjustSize()
        point = anchor.mapToGlobal(QtCore.QPoint(0, anchor.height() + 4))
        menu.move(point)
        # Kept alive by this reference until it closes (WA_DeleteOnClose then
        # frees the underlying widget) — an unparented Qt.Popup with no
        # Python reference can be garbage-collected mid-click.
        self._active_row_menu = menu
        menu.show()

    def _start_rename(self, session_id: str, title: str) -> None:
        text, ok = QtWidgets.QInputDialog.getText(
            self, "Rename conversation", "Name", QtWidgets.QLineEdit.Normal, title or ""
        )
        if ok and text.strip():
            self.session_renamed.emit(session_id, text.strip())

    def _confirm_delete(self, session_id: str, title: str) -> None:
        label = summarize_title(title) if title else "New conversation"
        reply = QtWidgets.QMessageBox.question(
            self,
            "Delete conversation",
            f"Delete “{label}”? This can't be undone.",
            QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.Cancel,
            QtWidgets.QMessageBox.Cancel,
        )
        if reply == QtWidgets.QMessageBox.Yes:
            self.session_removed.emit(session_id)


__all__ = ["ConversationDrawer", "sidebar_icon", "summarize_title"]

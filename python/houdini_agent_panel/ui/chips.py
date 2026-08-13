"""Precision-style header and custom choice controls.

No ``QComboBox``/``QMenu`` is used here.  The session picker, mode picker,
and the agent chip's switcher menu all render their own flat trigger and
popup surface so the OS/Qt style can never leak into the panel.
"""

from __future__ import annotations

from ..sessions import SessionMode
from . import theme
from .conversations import compose_icon as _compose_icon, sidebar_icon
from .qt import QtCore, QtGui, QtWidgets, Signal

_RAIL_WIDTH = 736
#: Floor for the centered rail. Without it — and without the
#: `minimumSizeHint` override below — the rail's `setFixedWidth` became the
#: header's minimum, the header's minimum became the panel's, and the panel
#: could not be docked into any Houdini pane narrower than 736px.
_MIN_RAIL_WIDTH = 180


#: How wide a chip label may get before it is elided. Model names run
#: long enough to push the whole row past the panel edge.
_MAX_CHOICE_LABEL_PX = 210
_MIN_POPUP_WIDTH = 220
#: Beyond this the choice list scrolls rather than running off screen.
_MAX_POPUP_HEIGHT = 360


class _ContentSizedToolButton(QtWidgets.QToolButton):
    """A tool button whose hint accounts for its own text.

    Houdini 21's pane style was measured returning 36–38px from the native
    ``QToolButton.sizeHint()`` regardless of whether the button said
    ``Claude Agent`` or ``Opus (1M context)``. The layout faithfully gave
    every control that width, leaving only ellipses. Keep the host's height
    and chrome, but provide the missing content width ourselves. The
    containing panel still advertises a narrow 180px minimum, so a
    genuinely narrow dock can compress the row without making the pane itself
    refuse to dock.
    """

    def _content_width(self, horizontal_padding: int) -> int:
        text = self.text()
        if not text:
            return 0
        width = QtGui.QFontMetrics(self.font()).horizontalAdvance(text) + horizontal_padding
        if not self.icon().isNull():
            style = self.toolButtonStyle()
            if style == QtCore.Qt.ToolButtonTextBesideIcon:
                width += self.iconSize().width() + 4
            elif style == QtCore.Qt.ToolButtonTextUnderIcon:
                width = max(width, self.iconSize().width() + horizontal_padding)
        return width

    def sizeHint(self) -> QtCore.QSize:  # noqa: N802 - Qt override
        hint = super().sizeHint()
        width = self._content_width(16)
        if not width:
            return hint
        return QtCore.QSize(max(hint.width(), width), hint.height())

    def reserve_content_width(self, horizontal_padding: int = 16) -> None:
        """Make Houdini's layout honour the content-aware hint.

        Its pane layout was also measured compressing these controls to the
        native 36–38px minimum while hundreds of stretch pixels remained
        unused. Reserving the hint moves that space from the stretch to the
        label. The panel/composer's own ``minimumSizeHint`` caps still let a
        genuinely narrow pane dock at 180px.
        """
        width = self._content_width(horizontal_padding)
        self.setMinimumWidth(max(self.sizeHint().width(), width) if width else 0)


class ChoiceButton(QtWidgets.QWidget):
    """Small custom dropdown with a styled, non-native popup."""

    activated = Signal(int)
    currentIndexChanged = Signal(int)

    def __init__(self, parent=None, *, accent: bool = False, show_caret: bool = True) -> None:
        super().__init__(parent)
        self._items: list[tuple[str, object, str]] = []  # text, data, description
        self._current_index = -1
        self._accent = accent
        self._show_caret = show_caret

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self._button = _ContentSizedToolButton(self)
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
        self._popup_scroll: QtWidgets.QScrollArea | None = None

        self._apply_theme()
        self._sync_text()

    def _apply_theme(self) -> None:
        """(Re)build every colour here from the live theme.

        Called from `__init__` and `showEvent` — never computed once and
        cached, so a panel opened under a different Houdini colour scheme (or
        a different `QApplication` palette in the dev preview/tests) gets
        that scheme's own accent instead of a colour frozen at import time.
        There is no known Houdini signal for "the colour scheme changed
        while this widget is already open" — see `HeaderBar._apply_theme`
        for the same caveat.
        """
        accent = theme.to_hex(theme.accent_color())
        self.setStyleSheet(
            "QToolButton#choiceTrigger, QToolButton#choiceTriggerAccent {"
            " border: none;"
            " border-radius: 7px;"
            " background: transparent;"
            " padding: 4px 8px;"
            "}"
            "QToolButton#choiceTrigger { color: palette(disabled, text); }"
            f"QToolButton#choiceTriggerAccent {{ color: {accent}; font-weight: 500; }}"
            "QToolButton#choiceTrigger:hover, QToolButton#choiceTriggerAccent:hover {"
            " background: palette(alternate-base);"
            "}"
        )
        self._popup_stylesheet = theme.popup_stylesheet("choicePopup")
        if self._popup is not None:
            self._popup.setStyleSheet(self._popup_stylesheet)

    def showEvent(self, event: QtGui.QShowEvent) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._apply_theme()

    def _ensure_popup(self) -> QtWidgets.QFrame:
        if self._popup is not None:
            return self._popup
        popup = QtWidgets.QFrame(None, QtCore.Qt.Popup | QtCore.Qt.FramelessWindowHint)
        popup.setObjectName("choicePopup")
        popup.setStyleSheet(self._popup_stylesheet)
        popup.installEventFilter(self)
        # A scroll area, because an agent can offer more choices than fit on
        # screen: OpenCode lists every model of every configured provider,
        # and without this the list simply runs off the bottom with no way
        # to reach the rest.
        outer = QtWidgets.QVBoxLayout(popup)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QtWidgets.QScrollArea(popup)
        scroll.setObjectName("choiceScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        # Parented at construction, not after. A parentless QWidget IS a
        # top-level window in Qt, and on macOS it gets a native one the
        # moment it is realised — `setWidget` below reparents it, but by
        # then the window has been created and macOS never reclaims a native
        # window once made. This is the same defect that filled the screen
        # with stray panes from `transcript.py`, in a second place.
        content = QtWidgets.QWidget(scroll)
        layout = QtWidgets.QVBoxLayout(content)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(2)
        scroll.setWidget(content)
        outer.addWidget(scroll)
        self._popup = popup
        self._popup_scroll = scroll
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

    def addItem(self, text: str, data: object = None, description: str = "") -> None:  # noqa: N802 - Qt-like API
        """`description` is the AGENT's own word on what this choice is —
        e.g. a model choice named "Default (recommended)" by the agent,
        with no description, names nothing; with one, it says "Opus 5 with
        1M context · Best for everyday, complex tasks". Shown as the
        trigger's tooltip when this is the current choice (`_sync_text`)
        and as a second line under its name in the popup (`_rebuild_popup`)
        — never invented here, only carried through from the caller."""
        self._items.append((text, data, description))
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
        return next(
            (i for i, (_text, value, _description) in enumerate(self._items) if value == data), -1
        )

    def setCurrentIndex(self, index: int) -> None:  # noqa: N802 - Qt-like API
        if not 0 <= index < len(self._items) or index == self._current_index:
            return
        self._current_index = index
        self._sync_text()
        self._rebuild_popup()
        self.currentIndexChanged.emit(index)

    def _sync_text(self) -> None:
        """Set the trigger's text, elided to fit.

        Model names are not short — "Unchained API/Huihui Qwen3 Coder 30B
        Abliterated" is a real one — and a chip that grows to fit pushed the
        whole row past the panel edge, where there is nothing to scroll it
        back with. The full name stays in the tooltip and in the popup.

        The tooltip itself prefers the current choice's own `description`
        over the elided-name fallback: a name like "Default (recommended)"
        never got any clearer by repeating it in full, but the agent's own
        description of what that choice actually is ("Opus 5 with 1M
        context…") does.
        """
        label, _data, description = (
            self._items[self._current_index] if self._current_index >= 0 else ("", None, "")
        )
        if not label:
            self._button.setText("")
            self._button.setToolTip("")
            return
        metrics = QtGui.QFontMetrics(self._button.font())
        shown = metrics.elidedText(label, QtCore.Qt.ElideMiddle, _MAX_CHOICE_LABEL_PX)
        self._button.setText(f"{shown}  ⌄" if self._show_caret else shown)
        self._button.setToolTip(description or (label if shown != label else ""))
        # A docked H20.5/H21 Python Pane Tab paints 16px of inset on each
        # side but reports neither through sizeHint()/sizeFromContents().
        self._button.reserve_content_width(32)
        self._button.updateGeometry()

    def _toggle_popup(self) -> None:
        popup = self._ensure_popup()
        if popup.isVisible():
            popup.hide()
            return
        self._rebuild_popup()
        width = max(self._button.width(), _MIN_POPUP_WIDTH)
        popup.setFixedWidth(width)
        popup.adjustSize()
        # Cap the height so a long list scrolls instead of leaving the screen.
        content_height = self._popup_layout.sizeHint().height() + 10
        popup.setFixedHeight(min(content_height, _MAX_POPUP_HEIGHT))
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
        # Straight from the live palette, same reasoning as `sidebar_icon`
        # and `theme.popup_background` — not `theme.color()`'s `hou.qt`-
        # first path.
        # One line per choice: its name, nothing else. The agent's
        # `description` is real and useful, but it belongs in the tooltip,
        # not under every entry — a picker with a paragraph beneath each item
        # is a document, and the artist asked for the list Claude Code shows:
        # four model names and no prose. Tried the other way and it was
        # worse, twice.
        for index, (label, _data, _description) in enumerate(self._items):
            button = QtWidgets.QPushButton(label, self._popup)
            button.setProperty("checkedChoice", index == self._current_index)
            button.setToolTip(_description)
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
    agent_selected = Signal(str)
    conversations_clicked = Signal()
    new_session_clicked = Signal()
    #: One click opens Settings, the SAME click closes it — the owner's
    #: own words: "the three dots it opens from should be linked to it,
    #: and pressing them again hides it". `AgentPanel` owns the toggle
    #: logic (whether Settings is currently open); this only reports the
    #: click. Used to open a two-item overflow menu (Settings, Report a
    #: bug…) — that stopped being needed once the bug-report entry point
    #: moved to its own link under the composer, leaving Settings as the
    #: button's only job, so it went back to being a direct action.
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

        self._agent_button = _ContentSizedToolButton(self._rail)
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
        # The same compose glyph as the drawer's "New chat" button: one
        # action, one shape, wherever it appears. A bare "+" next to a
        # conversation list reads as "add a row" — it is the generic add of
        # every toolbar in Houdini and says nothing about starting to talk.
        self._new_conversation_button.setText("")
        self._new_conversation_button.setIcon(_compose_icon())
        self._new_conversation_button.setToolTip("New conversation")
        self._new_conversation_button.clicked.connect(self.new_session_clicked)
        layout.addWidget(self._new_conversation_button)

        self._settings_button = QtWidgets.QToolButton(self._rail)
        self._settings_button.setObjectName("contextIcon")
        self._settings_button.setText("⋯")
        self._settings_button.setToolTip("Settings")
        # Checkable so `set_settings_open` can give it a pressed/active
        # look while Settings is open — the same click toggles it closed,
        # and a control that does two opposite things on repeated clicks
        # has to visibly say which one is coming next, not leave it a
        # guess (see `set_settings_open`'s own docstring).
        self._settings_button.setCheckable(True)
        self._settings_button.clicked.connect(self.settings_clicked.emit)
        layout.addWidget(self._settings_button)

        self.setStyleSheet(
            "QToolButton#contextButton, QToolButton#contextIcon {"
            " min-height: 26px; border: none; border-radius: 6px;"
            " color: palette(disabled, text); background: transparent; padding: 0 7px;"
            "}"
            "QToolButton#contextButton:hover, QToolButton#contextIcon:hover,"
            " QToolButton#contextIcon:checked {"
            " color: palette(text); background: palette(alternate-base);"
            "}"
            "QLabel#contextDivider { color: palette(mid); }"
        )

        # Installed agents fed by the panel (see `AgentPanel._refresh_agent_chip_menu`).
        # Empty until the panel's first boot pass — the chip just opens
        # settings until then, same as the "0 or 1 installed" case below.
        self._agent_items: list[tuple[str, str]] = []
        self._agent_current_id: str | None = None

        self._agent_popup: QtWidgets.QFrame | None = None
        self._agent_popup_layout: QtWidgets.QVBoxLayout | None = None
        self._agent_popup_stylesheet = theme.popup_stylesheet("agentPopup")
        #: Whether the chip currently shows a real registry icon rather than
        #: the synthesized accent dot (`set_agent`'s `icon=None` case) — the
        #: dot has to be redrawn on a theme refresh, a real icon doesn't.
        self._agent_has_custom_icon = False

    def _apply_theme(self) -> None:
        """(Re)build every colour here from the live theme — see
        `ChoiceButton._apply_theme` for why this isn't computed once and
        cached. There is no known Houdini signal for "the colour scheme
        changed while this panel is already open" — a fresh tab (or this
        widget's `showEvent`, e.g. the pane becoming visible again) is what
        picks up a scheme switched in the meantime, not a live repaint.
        """
        self._agent_popup_stylesheet = theme.popup_stylesheet("agentPopup")
        if self._agent_popup is not None:
            self._agent_popup.setStyleSheet(self._agent_popup_stylesheet)
        if not self._agent_has_custom_icon:
            self._agent_button.setIcon(self._fallback_agent_icon())

    def showEvent(self, event: QtGui.QShowEvent) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._apply_theme()

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

    def set_new_session_busy(self, busy: bool) -> None:
        """The compose button's own feedback for "a `session/new` is
        already in flight" — disabled the instant the click that started
        it lands, not up to 20s later when `_report_stalled_new_session`
        finally has something to say. Without this, an agent whose
        `session/new` legitimately takes tens of seconds (a heavy MCP
        fleet, `claude_show_host_mcp_servers` on) looked exactly like a
        button that ate the first click, and a second one opened a real,
        separate second session instead of nothing (see the panel's own
        `_new_session_pending` guard, which this mirrors).
        """
        self._new_conversation_button.setEnabled(not busy)
        self._new_conversation_button.setToolTip(
            "Opening a new conversation…" if busy else "New conversation"
        )

    def set_settings_open(self, is_open: bool) -> None:
        """Keeps the "…" button's own pressed look in sync with whether
        Settings is actually open — driven by `AgentPanel._show_page`, the
        one funnel every way of opening OR closing Settings already goes
        through (the button's own click, Escape, an agent switch landing
        back on the transcript). The button toggling what it looks like
        only on ITS OWN click would drift the moment any of those other
        routes closed Settings without going through this button again.
        """
        self._settings_button.setChecked(is_open)

    def minimumSizeHint(self) -> QtCore.QSize:  # noqa: N802 - Qt override
        hint = super().minimumSizeHint()
        return QtCore.QSize(min(hint.width(), _MIN_RAIL_WIDTH), hint.height())

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._rail.setFixedWidth(max(_MIN_RAIL_WIDTH, min(_RAIL_WIDTH, self.width() - 28)))

    def set_agent(self, name: str, icon: QtGui.QIcon | None) -> None:
        self._agent_button.setText(name)
        self._agent_has_custom_icon = icon is not None
        self._agent_button.setIcon(icon if icon is not None else self._fallback_agent_icon())
        self._agent_button.reserve_content_width()
        self._agent_button.updateGeometry()

    def _fallback_agent_icon(self) -> QtGui.QIcon:
        """A small accent-coloured dot — used until the registry's real icon
        for the current agent arrives. The colour is the theme's own accent,
        read fresh every time this is drawn (`_apply_theme`), not a fixed
        amber baked in once."""
        dot = QtGui.QPixmap(10, 10)
        dot.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(dot)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setBrush(theme.accent_color())
        painter.setPen(QtCore.Qt.NoPen)
        painter.drawEllipse(2, 2, 7, 7)
        painter.end()
        return QtGui.QIcon(dot)

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
            separator.setProperty("popupSeparator", True)
            separator.setFrameShape(QtWidgets.QFrame.HLine)
            self._agent_popup_layout.addWidget(separator)

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

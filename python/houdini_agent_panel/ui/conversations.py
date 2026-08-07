"""In-panel conversation drawer; never a native popup/window."""

from __future__ import annotations

import contextlib

from ..sessions import SessionState
from . import theme
from .qt import QtCore, QtGui, QtWidgets, Signal

#: The drawer's width when there's room for it — i.e. when it fits inside
#: `TranscriptView.current_gutter()`, the margin that's already empty on
#: either side of the 736px reading column. Below that it shrinks; see
#: `set_available_width`.
_DRAWER_IDEAL_WIDTH = 286

#: What's left for the title once the drawer's own margins and the pin/more
#: icon buttons take their share, AT the ideal width. `QPushButton` doesn't
#: elide overflowing text on its own — without accounting for this, a long
#: first message pushed the pin and overflow buttons straight out of the
#: (non-scrolling) drawer, off screen. The icons and margins don't shrink
#: with the drawer — only the title does — so this same 96px
#: (`_DRAWER_IDEAL_WIDTH` minus this) is subtracted at any width; see
#: `_build_row`.
_TITLE_MAX_WIDTH_IDEAL = 190
_ROW_CHROME_WIDTH = _DRAWER_IDEAL_WIDTH - _TITLE_MAX_WIDTH_IDEAL

#: A third of `summarize_title`'s own 60-character default limit — not a
#: separately chosen number. Narrow enough to give real width back to the
#: reading column; wide enough that a title still reads as a title
#: ("Set up a full pyro…") instead of two words and an ellipsis.
_FLOOR_TITLE_CHARS = 20

#: The same kind of prompt `test_long_title_is_elided_not_left_to_overflow_
#: the_drawer` already uses as this codebase's reference "realistically
#: long" title — reused here rather than inventing a second one, so the
#: floor this measures is grounded in an example that's actually plausible
#: for this project's own artists, not a generic placeholder string.
_FLOOR_TITLE_SAMPLE = (
    "Set up a full pyro simulation with a custom velocity field and volume shader chain"
)


def _drawer_floor_width() -> int:
    """The narrowest the drawer is allowed to shrink to before it stops.

    Below this, `set_available_width` no longer tracks the available
    gutter — the drawer holds this width and the panel accepts a small
    overlap with the reading column instead (see its own docstring for
    why that's the better tradeoff of the two the owner was offered).
    Measured, not guessed: `_ROW_CHROME_WIDTH` plus the actual rendered
    width of a title truncated to `_FLOOR_TITLE_CHARS`, against the live
    application font.
    """
    metrics = QtGui.QFontMetrics(QtWidgets.QApplication.font())
    sample = summarize_title(_FLOOR_TITLE_SAMPLE, limit=_FLOOR_TITLE_CHARS)
    return _ROW_CHROME_WIDTH + metrics.horizontalAdvance(sample)


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
    # Muted like every other chrome icon in the header (`palette(disabled,
    # text)`) — read fresh each call, so a caller that redraws this on a
    # theme refresh (`HeaderBar._apply_theme`) actually gets a new tone.
    # Straight from the palette, not `theme.color()`'s `hou.qt`-first path —
    # see `theme.popup_background`'s docstring for why.
    pen = QtGui.QPen(theme.palette().color(QtGui.QPalette.Disabled, QtGui.QPalette.Text), 1.2)
    painter.setPen(pen)
    painter.setBrush(QtCore.Qt.NoBrush)
    painter.drawRoundedRect(QtCore.QRectF(2.25, 3.25, 13.5, 11.5), 2.0, 2.0)
    painter.drawLine(QtCore.QPointF(7.0, 3.5), QtCore.QPointF(7.0, 14.5))
    painter.end()
    return QtGui.QIcon(pixmap)


def compose_icon(size: int = 16) -> QtGui.QIcon:
    """A speech bubble with a pencil crossing its top-right corner — the
    "new chat" glyph, drawn rather than shipped.

    The bubble is what makes it read as a conversation. A first attempt used
    a plain rounded rectangle with its corner left open for the pencil, and
    without the tail it read as "open in a new window" — the artist said so
    on sight, and they were right: that outline is the external-link glyph
    in every icon set there is. The tail is not decoration, it is the whole
    difference between "chat" and "link".

    Drawn from the palette for the same reason as `sidebar_icon`: an image
    would need a light copy and a dark copy and would still be wrong under a
    Houdini theme preset.
    """
    pixmap = QtGui.QPixmap(size, size)
    pixmap.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(pixmap)
    try:
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        u = size / 16.0
        pen = QtGui.QPen(theme.palette().color(QtGui.QPalette.Text), 1.35 * u)
        pen.setJoinStyle(QtCore.Qt.RoundJoin)
        pen.setCapStyle(QtCore.Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.NoBrush)

        # The bubble, open at its top-right corner so the pencil can cross
        # it, and with the tail hanging off the bottom edge.
        bubble = QtGui.QPainterPath()
        bubble.moveTo(9.2 * u, 2.1 * u)          # top edge, stopping short of the corner
        bubble.lineTo(4.0 * u, 2.1 * u)
        bubble.quadTo(1.9 * u, 2.1 * u, 1.9 * u, 4.2 * u)   # top-left corner
        bubble.lineTo(1.9 * u, 9.6 * u)
        bubble.quadTo(1.9 * u, 11.7 * u, 4.0 * u, 11.7 * u)  # bottom-left corner
        bubble.lineTo(6.3 * u, 11.7 * u)
        bubble.lineTo(7.9 * u, 14.1 * u)          # the tail
        bubble.lineTo(9.5 * u, 11.7 * u)
        bubble.lineTo(12.0 * u, 11.7 * u)
        bubble.quadTo(14.1 * u, 11.7 * u, 14.1 * u, 9.6 * u)  # bottom-right corner
        bubble.lineTo(14.1 * u, 7.0 * u)          # right edge, stopping short of the corner
        painter.drawPath(bubble)

        # The pencil: an outlined body with a nib, not a stroked diagonal.
        # Two strokes crossing at the top read as a crossbar at any size
        # above about 24px — the reference is a closed shape, so this is one.
        pencil = QtGui.QPainterPath()
        pencil.moveTo(7.6 * u, 9.1 * u)           # the nib, pointing back into the bubble
        pencil.lineTo(8.4 * u, 8.8 * u)           # one flank of the body
        pencil.lineTo(14.4 * u, 2.8 * u)
        pencil.quadTo(15.1 * u, 2.1 * u, 14.4 * u, 1.4 * u)   # the rounded butt end
        pencil.quadTo(13.7 * u, 0.7 * u, 13.0 * u, 1.4 * u)
        pencil.lineTo(7.0 * u, 7.4 * u)           # the other flank
        pencil.closeSubpath()                      # back to the nib
        painter.drawPath(pencil)
    finally:
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
        return "New chat"
    if len(first_line) <= limit:
        return first_line
    truncated = first_line[:limit]
    cut = truncated.rfind(" ")
    if cut > 0:
        truncated = truncated[:cut]
    return truncated.rstrip() + "…"


def scope_label_text(count: int) -> str:
    """What `ConversationDrawer._scope_label` says for `count` conversations
    currently shown — a pure function of the number, so the wording is
    checkable without building the widget.

    Says "here" rather than naming the scene folder again: the header
    already shows the full `$HIP` path (`HeaderBar.set_cwd`) right above the
    drawer's toggle, so repeating it would be the same fact twice in two
    places at once, not two different facts. `count == 0` is the fallback
    for when there's no agent to name yet (before one is chosen) — see
    `empty_scope_text` for the case that matters more, once there is one.
    """
    if count == 0:
        return "No conversations here yet"
    if count == 1:
        return "1 conversation here"
    return f"{count} conversations here"


def empty_scope_text(
    agent_label: str, *, other_agents_here: int = 0, this_agent_elsewhere: int = 0
) -> str:
    """What the drawer says with NOTHING to show, once the caller (`Agent
    Panel`, the only thing that knows both the current agent and folder)
    has a label to give it.

    Names BOTH filters — the scene folder AND the agent — rather than
    reading as an unexplained absence. Measured for real, twice: an
    artist opened a scene, saw one empty drawer, and reported his
    conversations missing — the drawer was correct both times (dumping
    the store: 41 conversations in that folder, all belonging to a
    DIFFERENT agent; the 2 that did belong to this one lived in a
    different folder entirely). "No conversations here yet" was true but
    said nothing about WHY, and a correct answer that reads as data loss
    is worse than useless.

    `other_agents_here`/`this_agent_elsewhere` are cheap counts the
    caller already had cause to compute (`AgentPanel._compute_empty_scope_text`'s
    own docstring has the cost accounting) — shown only when nonzero, so
    a genuinely first-ever conversation still gets the plain sentence,
    not a suspicious "0 elsewhere" that invites the exact suspicion this
    exists to prevent.

    `agent_label` empty (no agent chosen yet) falls back to the older,
    agent-agnostic wording — there's nothing to name.
    """
    if not agent_label:
        return scope_label_text(0)
    base = f"No conversations for {agent_label} in this scene folder"
    hints = []
    if other_agents_here:
        hints.append(
            "1 here for another agent" if other_agents_here == 1
            else f"{other_agents_here} here for other agents"
        )
    if this_agent_elsewhere:
        hints.append(
            f"1 for {agent_label} in another folder" if this_agent_elsewhere == 1
            else f"{this_agent_elsewhere} for {agent_label} in other folders"
        )
    if not hints:
        return base
    return base + " — " + ", ".join(hints)


def _row_menu_stylesheet() -> str:
    """Same recipe as every other popup surface (`theme.popup_stylesheet`),
    plus the delete action's own muted-warning tone instead of a fixed red —
    `QPalette` has no "error" role (see `theme.status_color`'s own docstring
    for why this codebase never invents one), so the delete button reuses
    the same `pending`-style tone permission prompts use for a reject
    button, on the shared hover background rather than a second fixed hue.
    """
    warning = theme.to_hex(theme.status_color("pending"))
    hover_bg = theme.to_hex(theme.popup_hover_background())
    return theme.popup_stylesheet("rowMenu") + (
        f"QPushButton#rowMenuDelete {{ color: {warning}; }}"
        f"QPushButton#rowMenuDelete:hover {{ background: {hover_bg}; color: {warning}; }}"
    )


class ConversationDrawer(QtWidgets.QFrame):
    """Slides in from the left, under the header, and lives in the margin
    that's already empty there rather than moving anything.

    Wide panels already leave a quiet gutter on either side of the 736px
    reading column (`TranscriptView`'s own doing) — the drawer draws inside
    THAT margin (`set_available_width`), so opening it moves nothing: the
    feed and composer were never using those pixels to begin with. A push-
    based version of this (reserving the drawer's width as the body's own
    left margin) shipped first and was rejected twice over — first for the
    jump it caused opening instantly, then again after fixing the jump,
    because the owner didn't want the content moving AT ALL, smoothly or
    not. This is what stayed.

    Two more geometry rules, both learned from the panel looking broken
    with the drawer open. It starts BELOW the header (`set_top_inset`),
    because the only control that closes it again is the header's own
    sidebar toggle — a drawer covering its own toggle is a drawer you
    cannot close. And it reports its state through `open_state_changed` so
    the panel can keep other floating chrome (the permission popover) on
    top of it, not so the panel can move anything out of its way.
    """

    session_selected = Signal(str)
    session_renamed = Signal(str, str)
    session_removed = Signal(str)
    new_session_clicked = Signal()
    #: True when the drawer starts opening, False when it starts closing.
    open_state_changed = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("conversationDrawer")
        self.setAttribute(QtCore.Qt.WA_StyledBackground, True)
        self.setFixedWidth(_DRAWER_IDEAL_WIDTH)
        self._top = 0
        #: The one row menu, built on first use and reused forever after —
        #: see `_open_row_menu` for why it is never rebuilt.
        self._row_menu: QtWidgets.QFrame | None = None
        self._row_menu_rename: QtWidgets.QPushButton | None = None
        self._row_menu_delete: QtWidgets.QPushButton | None = None
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
        self._new_button = QtWidgets.QPushButton("  New chat", self)
        self._new_button.setObjectName("newConversation")
        self._new_button.setIcon(compose_icon())
        self._new_button.clicked.connect(self._on_new_session)
        layout.addWidget(self._new_button)

        # No "Conversations" heading — that idea was tried and rejected: the
        # button above already says what this column is for, and the rows
        # below are self-evidently the conversations, so a static label
        # between them just took a line for something nobody was in doubt
        # about. This one is different in kind, not a second attempt at the
        # same thing: it's the only place that says how many conversations
        # are being shown, which was previously invisible — an artist who
        # opened a scene with real history on disk had no way to tell "the
        # drawer legitimately has one conversation" apart from "the drawer
        # is supposed to have four and only found one" (the restore bug
        # `_restore_conversations`/`scene.watch_hip_dir_changes` fixed). Set
        # from `set_sessions`, not computed here: the count is exactly
        # `len(states)`, already the drawer's own truth, so there is no
        # second source to go stale against it.
        #
        # The EMPTY case is different — `scope_label_text(0)` alone reads
        # as an absence, not an explanation, and an artist who opened a
        # scene with real history elsewhere (a different agent, a
        # different folder) read a CORRECT empty drawer as data loss,
        # twice. `_empty_scope_text` is what `AgentPanel._refresh_sessions`
        # supplies instead, ahead of an empty `set_sessions` call — it
        # names both filters (agent and folder), because the drawer itself
        # knows neither on its own. Cross-scope counts are the panel's
        # call too, deliberately: a full, unfiltered `conversations_store.
        # load()` measured ~25-30ms at the store's worst case (50
        # conversations x 400 entries) — cheap for the rare "the list just
        # went empty" case this is reserved for, not something to re-pay
        # on every `set_sessions()` call, which fires on far more than
        # just that.
        self._empty_scope_text = scope_label_text(0)
        self._scope_label = QtWidgets.QLabel(self)
        self._scope_label.setObjectName("drawerScopeLabel")
        self._scope_label.setContentsMargins(9, 0, 9, 0)
        # The short, populated-case text ("4 conversations here") never
        # needed this — the empty-case text naming both an agent and a
        # cross-scope count routinely runs past the drawer's own width,
        # and clipped silently (measured: rendered and looked, the exact
        # rule this project holds itself to for anything visual) until
        # this was added.
        self._scope_label.setWordWrap(True)
        self._scope_label.setText(self._empty_scope_text)
        layout.addWidget(self._scope_label)

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

        self._apply_theme()

    def _apply_theme(self) -> None:
        """(Re)build the drawer's colours from the live theme.

        Everything here is `palette(...)` already except the pinned icon's
        accent, which — same reasoning as `ChoiceButton._apply_theme` — is
        read fresh rather than baked in once. Called from `__init__` and
        `showEvent`; also re-draws the rows so their busy/unread dots (which
        paint the accent onto a `QPixmap`, not through the stylesheet) pick
        up the refreshed colour too.
        """
        accent = theme.to_hex(theme.accent_color())
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
            "QLabel#drawerScopeLabel {"
            " color: palette(disabled, text); font-size: 11px; padding: 2px 0 4px 0;"
            "}"
            "QScrollArea#drawerScroll { background: transparent; border: none; }"
            "QScrollArea#drawerScroll > QWidget > QWidget { background: transparent; }"
            + theme.scrollbar_stylesheet("QScrollArea#drawerScroll ")
            + (
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
            f"QToolButton#rowPin[pinned=\"true\"] {{ color: {accent}; }}"
            )
        )
        if self._states:
            self.set_sessions(list(self._states.values()), self._current_id)

    def showEvent(self, event: QtGui.QShowEvent) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._apply_theme()

    def set_empty_scope_text(self, text: str) -> None:
        """What the scope label shows the NEXT time `set_sessions` is
        called with an empty list — see `_empty_scope_text`'s own comment
        in `__init__` for why this comes from the caller rather than being
        computed here. Setting this alone doesn't repaint anything; it
        only takes effect once `set_sessions` runs.
        """
        self._empty_scope_text = text

    def set_sessions(self, states: list[SessionState], current_id: str | None) -> None:
        self._current_id = current_id
        self._states = {state.session_id: state for state in states}
        self._scope_label.setText(scope_label_text(len(states)) if states else self._empty_scope_text)
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
                # A real, reproducible native crash lived here: rebuilding
                # this list down to EMPTY (the last conversation deleted)
                # could later segfault in unrelated code, the first time
                # anything else pumped the event loop hard — confirmed with
                # lldb, not guessed: `deleteChildren()`, cascading from
                # THIS widget's own deferred delete, crashed inside
                # `QObjectPrivate::setParent_helper` while tearing down a
                # `QToolButton` row-pin/row-more button that still had a
                # live `clicked` connection to a lambda closing over this
                # drawer. Isolated by elimination, not by argument:
                # shrinking to a SMALLER NON-EMPTY list never crashed;
                # deferring the `deleteLater()` call itself (a `QTimer.
                # singleShot(0, ...)`) changed nothing, since `deleteLater`
                # already posts its event asynchronously regardless of
                # when it's called; the one change that reliably closed it,
                # every time, was severing the connection BEFORE deletion
                # rather than leaving Qt's own destructor to walk a live
                # one during `deleteChildren()`. Scoped to `QAbstractButton`
                # because that covers every signal `_build_row` actually
                # connects today (the row's own button, `rowPin`,
                # `rowMore`) — revisit this if a future row starts wiring
                # up a different widget type's signal.
                for button in widget.findChildren(QtWidgets.QAbstractButton):
                    with contextlib.suppress(RuntimeError, TypeError):
                        button.clicked.disconnect()
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
        title = summarize_title(state.title) if state.title else "New chat"
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
        busy_dot.setPixmap(_dot_pixmap(theme.accent_color()))
        busy_dot.setToolTip("The agent is working on this conversation")
        busy_dot.setVisible(state.busy)
        row_layout.addWidget(busy_dot)

        button = QtWidgets.QPushButton(row)
        button.setProperty("conversation", True)
        button.setProperty("currentConversation", state.session_id == self._current_id)
        button.setToolTip(state.title or "New chat")
        metrics = QtGui.QFontMetrics(button.font())
        # The icons and margins around it don't shrink with the drawer —
        # only the title does (`set_available_width`) — so what's left for
        # it is the CURRENT width minus that fixed chrome, not the width at
        # the ideal size.
        title_max_width = max(20, self.width() - _ROW_CHROME_WIDTH)
        button.setText(metrics.elidedText(title, QtCore.Qt.ElideRight, title_max_width))
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
        unread_dot.setPixmap(_dot_pixmap(theme.accent_color()))
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

    def set_available_width(self, gutter: int) -> None:
        """How much room the panel has for this drawer without covering the
        reading column — `TranscriptView.current_gutter()`.

        Shrinks to fit inside `gutter`; never grows past `_DRAWER_IDEAL_
        WIDTH` even if there's more room than that (a wider conversation
        list past the point every title already fits buys nothing). Below
        `_drawer_floor_width()` it stops shrinking — see that function —
        which means the caller ends up overlapping the reading column by a
        few pixels in that one tail case, in exchange for the reading
        column never getting permanently narrower for it. Rebuilds the
        rows when the width actually changes, so their titles re-elide to
        the new available space (`_build_row`) — a few dozen rows at most,
        not the feed, so this is cheap regardless of conversation count.
        """
        target = min(_DRAWER_IDEAL_WIDTH, max(_drawer_floor_width(), int(gutter)))
        if target == self.width():
            return
        self.setFixedWidth(target)
        if self._states:
            self.set_sessions(list(self._states.values()), self._current_id)

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
        # The drawer stays open, like the "New chat" button above it. It
        # closed here on the reasoning that the artist asked for this one
        # conversation and now wants to read it — which turned out to be the
        # wrong model of the thing: this is a panel, not a menu. Reading one
        # conversation is very often followed by reading another, and the
        # toggle in the header is there for whoever actually wants it gone.
        self.session_selected.emit(session_id)

    def _on_new_session(self) -> None:
        # The drawer stays open. Picking an existing conversation closes it
        # because the artist asked for that one and wants to read it; asking
        # for a NEW chat is the one action most likely to be repeated, and
        # closing the list means reopening it to do the same thing again.
        # The new chat appears in the list they are still looking at.
        self.new_session_clicked.emit()

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
        # ONE menu, reused for every row, never rebuilt. It used to be
        # created fresh per click with `WA_DeleteOnClose`, which reads as
        # tidy and is the opposite: measured in a live Houdini, showing a
        # top-level window costs one native window and destroying the widget
        # releases none of them. That made this a leak of exactly one window
        # per click on "…", for the lifetime of the process.
        menu = self._row_menu
        if menu is None:
            menu = QtWidgets.QFrame(None, QtCore.Qt.Popup | QtCore.Qt.FramelessWindowHint)
            menu.setObjectName("rowMenu")
            menu_layout = QtWidgets.QVBoxLayout(menu)
            menu_layout.setContentsMargins(5, 5, 5, 5)
            menu_layout.setSpacing(2)
            self._row_menu_rename = QtWidgets.QPushButton("Rename…", menu)
            self._row_menu_delete = QtWidgets.QPushButton("Delete", menu)
            self._row_menu_delete.setObjectName("rowMenuDelete")
            menu_layout.addWidget(self._row_menu_rename)
            menu_layout.addWidget(self._row_menu_delete)
            self._row_menu = menu
        # Restyled on every open so a theme change is picked up, and
        # rewired because the same two buttons now act on a different row.
        menu.setStyleSheet(_row_menu_stylesheet())
        rename_button = self._row_menu_rename
        delete_button = self._row_menu_delete
        for button in (rename_button, delete_button):
            try:
                button.clicked.disconnect()
            except (RuntimeError, TypeError):
                pass
        rename_button.clicked.connect(lambda: (menu.close(), self._start_rename(session_id, title)))
        delete_button.clicked.connect(lambda: (menu.close(), self._confirm_delete(session_id, title)))

        width = max(anchor.width(), 150)
        menu.setFixedWidth(width)
        menu.adjustSize()
        point = anchor.mapToGlobal(QtCore.QPoint(0, anchor.height() + 4))
        menu.move(point)
        self._active_row_menu = menu
        menu.show()

    def _start_rename(self, session_id: str, title: str) -> None:
        text, ok = QtWidgets.QInputDialog.getText(
            self, "Rename conversation", "Name", QtWidgets.QLineEdit.Normal, title or ""
        )
        if ok and text.strip():
            self.session_renamed.emit(session_id, text.strip())

    def _confirm_delete(self, session_id: str, title: str) -> None:
        label = summarize_title(title) if title else "New chat"
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

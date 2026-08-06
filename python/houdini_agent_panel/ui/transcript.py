"""`TranscriptView` — the panel's feed (docs/architecture.md §10, §8).

Renders `TranscriptModel.entries()` row by row, with no frames around
messages. Two requirements are critical to how the panel feels (design.md,
"The middle"):

- `refresh(entry_id)` patches ONE entry in place instead of rebuilding the
  other widgets — a full redraw (`refresh(None)`) happens only when the
  session changes. Redrawing the whole feed on every streamed chunk is
  visible to the eye and slow.
- Auto-scroll to the bottom only kicks in if the human was already there:
  someone who scrolled up to read stays where they are instead of being
  dragged down to the latest chunk.
"""

from __future__ import annotations

import re

from ..transcript_model import Entry, TranscriptModel
from . import theme
from .qt import QtCore, QtGui, QtWidgets, Signal
from .thinking import ThinkingIndicator

#: How many pixels from the bottom still count as "at the bottom" — a small
#: allowance for layout rounding, so auto-scroll doesn't break over one pixel.
_BOTTOM_EPSILON = 4

#: `QTextEdit.setMarkdown` has existed since Qt 5.14 — PySide2 5.15.15
#: (H20.5) and PySide6 6.8.3 (H22) definitely have it (facts/houdini.md §3),
#: but we check at runtime rather than trust a version: degrading to plain
#: text beats crashing if someone's build turns out to lack the method.
_HAS_MARKDOWN = hasattr(QtWidgets.QTextEdit, "setMarkdown")

#: Triple backticks, with an optional language on the same line. An unclosed
#: fence (the agent hasn't streamed the trailing ```` ``` ```` yet) matches to
#: the end of the text — so half-arrived code renders as code instead of raw
#: backticks in the middle of a sentence.
_CODE_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\r?\n(.*?)(?:```|\Z)", re.DOTALL)


def _split_markdown_segments(text: str) -> list[tuple[str, str]]:
    """Split text into alternating ``("text", ...)`` / ``("code", ...)`` chunks.

    A code block renders as a SEPARATE widget with its own horizontal scroll
    and no word wrap (wrapping would ruin VEX/Python indentation) — so it
    can't simply be part of one markdown document of prose, where word wrap
    has to keep working as usual.
    """
    segments: list[tuple[str, str]] = []
    pos = 0
    for match in _CODE_FENCE_RE.finditer(text):
        before = text[pos : match.start()]
        if before.strip():
            segments.append(("text", before))
        segments.append(("code", match.group(1)))
        pos = match.end()
    tail = text[pos:]
    if tail.strip() or not segments:
        segments.append(("text", tail))
    return segments


class TranscriptView(QtWidgets.QScrollArea):
    #: The gutter (see `current_gutter`) whenever it actually changes size.
    #: `AgentPanel` uses this to keep the conversation drawer's width in
    #: sync — as a signal, not a value it pulls on its own resize, because
    #: a parent's resizeEvent can run before a child's own layout has
    #: actually been applied, and reading `current_gutter()` at that moment
    #: would see last frame's number. Pushed here, at the one point this
    #: value is actually known correct, that ordering problem doesn't exist.
    gutter_changed = Signal(int)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        # Long tool output must not stretch the panel horizontally — text
        # inside rows wraps by word, so a horizontal scrollbar is never needed.
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)

        self._content = QtWidgets.QWidget(self)
        self._layout = QtWidgets.QVBoxLayout(self._content)
        self._layout.setContentsMargins(14, 39, 14, 8)
        self._layout.setSpacing(14)
        # Activity rows stay in the chronology: user -> Worked for… -> answer.
        self._layout.addStretch(1)
        self.setWidget(self._content)
        # Same scrollbar as the drawer and the popups: no stepper arrows, a
        # handle with enough contrast to find. Reapplied on `showEvent` so a
        # tab reopened under a different Houdini theme picks up its tones.
        self.setStyleSheet(theme.scrollbar_stylesheet())

        self._model: TranscriptModel | None = None
        self._rows: dict[str, QtWidgets.QWidget] = {}
        #: The last gutter `resizeEvent` computed — see `current_gutter()`.
        #: Matches its own floor (14) as a default so a caller asking before
        #: the first resize gets a sane, conservative answer.
        self._gutter = 14

        # A per-instance, self-owned timer for the deferred autoscroll below
        # — NOT the static `QTimer.singleShot(0, self._scroll_to_bottom)`,
        # which schedules its callback on the application's event loop with
        # no owner of its own. If this view is destroyed before a 0ms shot
        # fires (a background session's view torn down between two panel
        # switches, or simply a test's widget going out of scope), that
        # dangling call into an already-deleted `QScrollArea` crashed the
        # whole process instead of raising a catchable error. Parenting the
        # timer to `self` makes Qt's own ownership take care of it: the timer
        # dies together with the view, so a pending shot simply never fires.
        self._scroll_timer = QtCore.QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.timeout.connect(self._scroll_to_bottom)

        # Following the bottom is a MODE, not a measurement taken per update.
        # It used to be re-derived on every refresh from "is the bar at the
        # bottom right now", and that answer is wrong for one frame every
        # time content grows faster than layout settles — the bar sits below
        # the new maximum through no fault of the reader. One such frame and
        # the feed decided the artist had scrolled up, and it never followed
        # again for the rest of the answer. Reported exactly that way: the
        # agent talks, the view stands still.
        #
        # So the mode changes only when the ARTIST moves the view, which is
        # the only thing that should ever turn following off.
        self._follow_bottom = True
        self._scrolling_ourselves = False
        bar = self.verticalScrollBar()
        bar.valueChanged.connect(self._on_scroll_value_changed)
        # Content growing while we are following must pull the view along,
        # however many layout passes it takes to settle.
        bar.rangeChanged.connect(self._on_scroll_range_changed)

    # --- public API ----------------------------------------------------

    def set_model(self, model: TranscriptModel) -> None:
        self._model = model
        self.refresh(None)

    def reset_thinking_after_tool(self) -> None:
        for row in reversed(tuple(self._rows.values())):
            if isinstance(row, _ActivityRow) and row.indicator.is_active():
                row.indicator.reset_after_tool()
                return

    def refresh(self, entry_id: str | None = None) -> None:
        if self._model is None:
            return
        if entry_id is None:
            self._rebuild_all()
        else:
            self._refresh_one(entry_id)
        if self._follow_bottom:
            # Deferred, not immediate: growing content (a streamed chunk
            # resizing its `QTextBrowser`) only widens the scrollbar's range
            # on a LATER layout pass — scrolling to `maximum()` right now
            # would still snap to the range from before this update, one
            # chunk behind. `start(0)` queues the actual scroll for the next
            # event-loop turn, after that layout has caught up.
            self._scroll_timer.start(0)

    # --- rebuilding -------------------------------------------------------

    def _rebuild_all(self) -> None:
        seen_widgets: set[int] = set()
        for row in self._rows.values():
            # A run of tool calls shares ONE `_ToolGroupRow` across several
            # entry ids — remove/delete it once, not once per id.
            if id(row) in seen_widgets:
                continue
            seen_widgets.add(id(row))
            self._layout.removeWidget(row)
            row.hide()  # before orphaning: a parentless widget is a window
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()

        entries = [e for e in self._model.entries() if e.kind != "permission"]
        position = 0
        index = 0
        while index < len(entries):
            entry = entries[index]
            if entry.kind == "tool":
                group = [entry]
                index += 1
                while index < len(entries) and entries[index].kind == "tool":
                    group.append(entries[index])
                    index += 1
                row = self._build_tool_widget(group)
                for grouped in group:
                    self._rows[grouped.id] = row
            else:
                row = self._make_row(entry)
                self._rows[entry.id] = row
                index += 1
            self._layout.insertWidget(position, row)
            position += 1

    def _build_tool_widget(self, group: list[Entry]) -> QtWidgets.QWidget:
        """One tool call stays a bare `_ToolCallRow` — identical to a lone
        call today. Only a run of two or more gets the group's extra chrome
        (a summary line plus a click to reveal the list)."""
        if len(group) == 1:
            return _ToolCallRow(group[0])
        return _ToolGroupRow(
            [_ToolCallRow(entry, self._content) for entry in group], self._content
        )

    def _refresh_one(self, entry_id: str) -> None:
        entries = self._model.entries()
        entry = next((e for e in entries if e.id == entry_id), None)

        if entry is not None and entry.kind == "permission":
            row = self._rows.pop(entry_id, None)
            if row is not None:
                self._layout.removeWidget(row)
                row.hide()  # before orphaning: a parentless widget is a window
                row.setParent(None)
                row.deleteLater()
            return

        if entry is None:
            # The protocol doesn't delete entries today, but we don't crash
            # if that ever changes — the row simply leaves the stage.
            row = self._rows.pop(entry_id, None)
            if row is not None:
                self._layout.removeWidget(row)
                row.hide()  # before orphaning: a parentless widget is a window
                row.setParent(None)
                row.deleteLater()
            return

        row = self._rows.get(entry_id)
        if row is not None:
            self._update_row(row, entry)
            return

        visible = [e for e in entries if e.kind != "permission"]
        position = visible.index(entry)

        if entry.kind == "tool":
            # Consecutive tool calls collapse into one block (design.md, "the
            # middle"): if the entry immediately before this one in the model
            # is also a tool call — and nothing else came in between — this
            # new call joins its row instead of getting one of its own.
            previous = visible[position - 1] if position > 0 else None
            previous_row = self._rows.get(previous.id) if previous is not None else None
            if previous is not None and previous.kind == "tool" and previous_row is not None:
                if isinstance(previous_row, _ToolGroupRow):
                    previous_row.add_tool(entry)
                    self._rows[entry_id] = previous_row
                    return
                # `previous_row` is still a bare `_ToolCallRow` — this is the
                # SECOND call in the run, so it graduates into a group
                # covering both. The existing widget is reused rather than
                # rebuilt, so an already-expanded first call stays expanded.
                widget_position = self._layout.indexOf(previous_row)
                self._layout.removeWidget(previous_row)
                group = _ToolGroupRow(
                    [previous_row, _ToolCallRow(entry, self._content)], self._content
                )
                self._rows[previous.id] = group
                self._rows[entry_id] = group
                self._layout.insertWidget(widget_position, group)
                return
            row = _ToolCallRow(entry, self._content)
        else:
            row = self._make_row(entry)

        # A new row goes at the position among already-drawn WIDGETS (not
        # model entries — several entries can share one `_ToolGroupRow`).
        widget_position = 0
        seen_widgets: set[int] = set()
        for candidate in visible[:position]:
            candidate_row = self._rows.get(candidate.id)
            if candidate_row is None or id(candidate_row) in seen_widgets:
                continue
            seen_widgets.add(id(candidate_row))
            widget_position += 1
        self._rows[entry_id] = row
        self._layout.insertWidget(widget_position, row)

    # --- building rows by kind ---------------------------------------------

    def _make_row(self, entry: Entry) -> QtWidgets.QWidget:
        # Every row is built WITH its parent, never adopted afterwards. A
        # parentless QWidget is a top-level window, and macOS hands Qt a real
        # native window for it the moment it exists — reparenting it into a
        # layout a line later does not always give that window back. One per
        # feed row, on a panel that draws a row per message and per tool
        # call, is how a Houdini that had been open for an afternoon came to
        # own three hundred stray windows.
        if entry.kind == "activity":
            return _ActivityRow(entry, self._content)
        if entry.kind == "plan":
            return _PlanRow(entry, self._content)
        return _MessageRow(entry, self._content)

    def _update_row(self, row: QtWidgets.QWidget, entry: Entry) -> None:
        if isinstance(row, _ToolGroupRow):
            row.update_tool(entry)
        else:
            row.update_from(entry)

    # --- auto-scroll -------------------------------------------------------

    def _is_at_bottom(self) -> bool:
        bar = self.verticalScrollBar()
        return bar.value() >= bar.maximum() - _BOTTOM_EPSILON

    def _scroll_to_bottom(self) -> None:
        bar = self.verticalScrollBar()
        self._scrolling_ourselves = True
        try:
            bar.setValue(bar.maximum())
        finally:
            self._scrolling_ourselves = False

    def _on_scroll_value_changed(self, value: int) -> None:
        """Only the artist's own scrolling decides whether we follow."""
        if self._scrolling_ourselves:
            return
        bar = self.verticalScrollBar()
        self._follow_bottom = value >= bar.maximum() - _BOTTOM_EPSILON

    def _on_scroll_range_changed(self, _minimum: int, _maximum: int) -> None:
        """The feed grew. If we are following, go with it.

        This is what makes following survive a slow layout: the range widens
        one pass later than the content arrives, and this fires then.
        """
        if self._follow_bottom:
            self._scroll_timer.start(0)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        # Both supplied references use a 706–736 px reading rail.  Wider
        # Houdini panes add quiet gutters instead of stretching prose forever.
        gutter = max(14, (self.viewport().width() - 736) // 2)
        if gutter != self._gutter:
            self._gutter = gutter
            self.gutter_changed.emit(gutter)
        margins = self._layout.contentsMargins()
        self._layout.setContentsMargins(gutter, margins.top(), gutter, margins.bottom())

    def current_gutter(self) -> int:
        """The empty margin left of the reading column at the current width.

        `AgentPanel` reads this to size the conversation drawer so it fits
        INSIDE this already-empty space instead of covering the reading
        column — the drawer lives in a margin that exists whether or not
        it's open, so opening it never has to move anything else.

        This is the actual last-applied margin, not a second computation of
        the same formula: `Composer`'s own centering converges to the same
        number at any width wide enough for a drawer to matter (both cap at
        a 736px rail and share the same 14px floor), and asking the
        transcript directly — which measures against its `viewport()`, a
        few pixels narrower than the raw widget width whenever its
        scrollbar is showing — is the more conservative of the two, so it's
        the one bound the drawer needs.
        """
        return self._gutter


class _ActivityRow(QtWidgets.QWidget):
    """Spinner while active; compact Worked-for divider after completion."""

    def __init__(self, entry: Entry, parent=None) -> None:
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(0)
        self.indicator = ThinkingIndicator(self)
        self.indicator.setMinimumHeight(30)
        layout.addWidget(self.indicator)
        rule = QtWidgets.QFrame(self)
        rule.setFrameShape(QtWidgets.QFrame.HLine)
        rule.setObjectName("activityRule")
        layout.addWidget(rule)
        self.setStyleSheet("QFrame#activityRule { color: palette(mid); }")
        self.update_from(entry)

    def update_from(self, entry: Entry) -> None:
        activity = entry.activity
        if activity is None:
            self.indicator.clear_activity()
            return
        if activity.finished_at is None:
            if not self.indicator.is_active():
                self.indicator.start(activity.started_at)
            return
        elapsed_ms = max(0, int((activity.finished_at - activity.started_at) * 1000))
        self.indicator.finish(elapsed_ms)


class _MessageRow(QtWidgets.QWidget):
    """A message (user/agent/thought) or an error — no frames, text selectable.

    Who said it has to read at a glance, without effort (design.md asks for
    "no frames" specifically, so we distinguish by colour and indent, not by
    a box): a human's line is muted and indented from the left, the agent's
    reply is normal colour across the full width, a thought is italic and
    muted too but not indented (it isn't a human's question, it's the agent
    thinking).

    Text renders through `QTextDocument.setMarkdown` — agents send markdown
    (backticks, **bold**, lists, ```code```) constantly, and this is the only
    way to show it formatted without feeding untrusted agent text straight
    into `setHtml`. Code blocks are cut out of the markdown and rendered as
    separate monospace widgets with their own horizontal scroll — prose is
    untouched and simply wraps by word.
    """

    def __init__(self, entry: Entry, parent=None) -> None:
        super().__init__(parent)
        self._kind = entry.kind
        self._layout = QtWidgets.QVBoxLayout(self)
        self._layout.setSpacing(theme.SPACING_TIGHT)
        self._apply_kind_margins(entry.kind)
        self._segments: list[QtWidgets.QWidget] = []
        self.update_from(entry)

    def update_from(self, entry: Entry) -> None:
        segments = _split_markdown_segments(entry.text)

        # Streaming usually just appends to the last chunk without changing
        # the number or type of chunks — then updating contents in place is
        # enough, no widgets recreated (the same logic as the rest of the
        # feed: patch, don't rebuild).
        same_shape = len(segments) == len(self._segments) and all(
            isinstance(widget, _CodeBlock) == (kind == "code")
            for widget, (kind, _content) in zip(self._segments, segments)
        )
        if same_shape:
            for widget, (kind, content) in zip(self._segments, segments):
                if kind == "code":
                    widget.set_code(content)
                else:
                    widget.set_text(content)
            return

        for widget in self._segments:
            self._layout.removeWidget(widget)
            widget.hide()  # before orphaning: a parentless widget is a window
            widget.setParent(None)
            widget.deleteLater()
        self._segments = []

        for kind, content in segments:
            if kind == "code":
                widget = _CodeBlock(content, self)
            else:
                widget = _ProseBlock(self)
                self._apply_kind_style(widget, entry.kind)
                widget.set_text(content)
            self._segments.append(widget)
            alignment = QtCore.Qt.AlignRight if entry.kind == "user" else QtCore.Qt.Alignment()
            self._layout.addWidget(widget, 0, alignment)

    def _apply_kind_margins(self, kind: str) -> None:
        # The indent is the visual marker for "a human typed this" — no
        # frames, no boxes.
        indent = theme.SPACING * 4 if kind == "user" else 0
        bottom = 32 if kind == "user" else 0
        self._layout.setContentsMargins(indent, 0, 0, bottom)

    def _apply_kind_style(self, widget: "_ProseBlock", kind: str) -> None:
        font = widget.font()
        palette = widget.palette()
        if kind == "thought":
            # A thought is secondary; the user bubble, by contrast, keeps
            # normal contrast as in the Claude/Codex references.
            palette.setColor(QtGui.QPalette.Text, theme.status_color("pending"))
        elif kind == "user":
            user_text = palette.color(QtGui.QPalette.Text)
            user_text.setAlpha(230)
            palette.setColor(QtGui.QPalette.Text, user_text)
        if kind == "thought":
            font.setItalic(True)
        elif kind == "error":
            font.setBold(True)
        if kind == "user":
            widget.setMaximumWidth(540)
            widget.document().setDocumentMargin(8)
            widget.setStyleSheet(
                "QTextBrowser {"
                " border: none;"
                " border-radius: 12px;"
                " background: palette(alternate-base);"
                " padding: 2px 4px;"
                "}"
            )
        widget.setFont(font)
        widget.setPalette(palette)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if self._kind == "user":
            maximum = max(220, int(self.width() * 0.74))
            for widget in self._segments:
                widget.setMaximumWidth(maximum)


class _ProseBlock(QtWidgets.QTextBrowser):
    """One chunk of a message's markdown prose — no frame, word wrap on.

    Height follows the content: an inner vertical scrollbar isn't needed, the
    feed already scrolls as a whole (`TranscriptView`), and a second scroll
    inside a message row would be one level of scrolling too many.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setReadOnly(True)
        # Links open in the external browser: the panel is neither a file
        # manager nor a web view.
        self.setOpenExternalLinks(True)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        # No fill of its own — the feed's background shows through, no box.
        self.setAutoFillBackground(False)
        self.viewport().setAutoFillBackground(False)
        self.document().documentLayout().documentSizeChanged.connect(self._sync_height)

    def set_text(self, text: str) -> None:
        if _HAS_MARKDOWN:
            self.setMarkdown(text)
        else:  # pragma: no cover — present on every target Qt (5.14+, facts/houdini.md §3)
            self.setPlainText(text)
        self._sync_height()

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self.document().setTextWidth(self.viewport().width())
        self._sync_height()

    def _sync_height(self, *_args: object) -> None:
        self.setFixedHeight(max(int(self.document().size().height()) + 4, 1))


class _CodeBlock(QtWidgets.QPlainTextEdit):
    """A code block from a ```fence``` — monospace, with its own horizontal scroll.

    `NoWrap` is deliberate: wrapping would break VEX/Python indentation. A
    long line scrolls INSIDE this widget (`QPlainTextEdit.sizeHint()` doesn't
    grow with the document — see `_sync_height`, only the layout sets the
    width) instead of pushing the panel wider — which is exactly why
    `TranscriptView` outside keeps `ScrollBarAlwaysOff`.
    """

    def __init__(self, code: str, parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(theme.monospace_font())
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        palette = self.palette()
        # A barely visible backing, from the palette (the role exists for
        # exactly this kind of "alternate" block) — not a frame and not a
        # hardcoded colour: it just tells code apart from the prose around it.
        palette.setColor(QtGui.QPalette.Base, theme.palette().color(QtGui.QPalette.AlternateBase))
        self.setPalette(palette)
        self.set_code(code)

    def set_code(self, code: str) -> None:
        self.setPlainText(code.rstrip("\n"))
        self._sync_height()

    def _sync_height(self) -> None:
        lines = max(self.document().blockCount(), 1)
        self.setFixedHeight(lines * self.fontMetrics().lineSpacing() + 8)


class _ToolTrigger(QtWidgets.QAbstractButton):
    """Flat, fully painted disclosure row — no native Qt arrow/button chrome."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setMinimumHeight(34)

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(5, 0, 5, 0)
        layout.setSpacing(7)
        self._kind = QtWidgets.QLabel(self)
        self._title = QtWidgets.QLabel(self)
        self._status = QtWidgets.QLabel(self)
        self._chevron = QtWidgets.QLabel("›", self)
        layout.addWidget(self._kind)
        layout.addWidget(self._title)
        layout.addStretch(1)
        layout.addWidget(self._status)
        layout.addWidget(self._chevron)

    def set_view(self, *, kind: str, title: str, status: str) -> None:
        self._kind.setText(theme.kind_glyph(kind))
        self._title.setText(title)
        self._status.setText(f"{theme.status_glyph(status)}  {theme.status_label(status)}")
        color = theme.status_color(status)
        palette = self._status.palette()
        palette.setColor(QtGui.QPalette.WindowText, color)
        self._status.setPalette(palette)
        self.setAccessibleName(self.text())
        self.update()

    def text(self) -> str:
        return f"{self._title.text()} — {self._status.text()}"

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802
        del event
        painter = QtGui.QPainter(self)
        if self.underMouse():
            hover = self.palette().color(QtGui.QPalette.AlternateBase)
            painter.fillRect(self.rect(), hover)
        divider = self.palette().color(QtGui.QPalette.Mid)
        divider.setAlpha(155)
        painter.setPen(divider)
        painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)

    def nextCheckState(self) -> None:  # noqa: N802
        super().nextCheckState()
        self._chevron.setText("⌄" if self.isChecked() else "›")


class _ToolCallRow(QtWidgets.QWidget):
    """Collapsible tool-call row: an icon for `kind`, a live status."""

    def __init__(self, entry: Entry, parent=None) -> None:
        super().__init__(parent)
        self.setMaximumWidth(560)
        self._entry_id = entry.id
        self._expanded = False

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACING_TIGHT)

        self._toggle = _ToolTrigger(self)
        self._toggle.clicked.connect(self._on_toggled)
        layout.addWidget(self._toggle)

        self._details = QtWidgets.QLabel(self)
        self._details.setWordWrap(True)
        self._details.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self._details.setFont(theme.monospace_font())
        self._details.setVisible(False)
        layout.addWidget(self._details)

        self.update_from(entry)

    def update_from(self, entry: Entry) -> None:
        tool = entry.tool
        self._tool = tool
        self._toggle.set_view(kind=tool.kind, title=tool.title, status=tool.status)
        if self._expanded:
            self._render_details()

    def _on_toggled(self, checked: bool) -> None:
        self._expanded = checked
        if checked:
            self._render_details()
        self._details.setVisible(checked)

    def _render_details(self) -> None:
        self._details.setText(_format_tool_content(self._tool.content, self._tool.locations))


def _format_tool_content(content: list[dict], locations: list[dict]) -> str:
    parts: list[str] = []
    for item in content:
        item_type = item.get("type")
        if item_type == "diff":
            path = item.get("path", "")
            old_text = item.get("old_text")
            new_text = item.get("new_text", "")
            if old_text:
                parts.append(f"--- {path}\n{old_text}\n+++ {path}\n{new_text}")
            else:
                parts.append(f"+++ {path}\n{new_text}")
        elif item_type == "terminal":
            parts.append(f"[terminal {item.get('terminal_id', '?')}]")
        elif item_type == "content":
            block = item.get("content") or {}
            text = block.get("text")
            parts.append(text if text is not None else str(block))
        else:
            parts.append(str(item))

    if locations:
        paths = ", ".join(loc.get("path", "?") for loc in locations)
        parts.append(f"[files: {paths}]")

    return "\n\n".join(parts) if parts else "(no content)"


class _ToolGroupRow(QtWidgets.QWidget):
    """A run of consecutive tool calls, collapsed into one block.

    "Все тулы сыплются в чат бесконечно" was the complaint: an agent that
    reads five files in a row used to draw five separate `_ToolCallRow`
    widgets, one under another, forever growing the feed. Modeled on how
    Claude Code shows this — collapsed by default to a single summary line
    (whichever step is still running, or a one-line result once the whole
    run is done), with a click revealing the full list.

    Deliberately a VIEW-only grouping: `TranscriptModel` still emits one
    `Entry` per tool call (docs/architecture.md §8) — nothing about the feed
    model changes, only how `TranscriptView` arranges rows on screen. Every
    entry id in the run maps to this SAME widget in `TranscriptView._rows`,
    which is how a `tool_call_update` for any of them finds its way back
    here (`update_tool`).
    """

    def __init__(self, rows: list["_ToolCallRow"], parent=None) -> None:
        super().__init__(parent)
        self.setMaximumWidth(560)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACING_TIGHT)

        self._summary = _ToolTrigger(self)
        self._summary.clicked.connect(self._on_toggled)
        layout.addWidget(self._summary)

        # The full list of steps — hidden until the summary is clicked. Each
        # step is a full `_ToolCallRow`, unmodified (and, for a call that was
        # already on screen before the run grew past one, the SAME instance —
        # see `TranscriptView._refresh_one` — so an already-expanded first
        # call doesn't collapse just because a second one arrived): expanding
        # a step inside the group still shows its diff/content exactly like a
        # standalone tool call always has.
        self._steps = QtWidgets.QWidget(self)
        self._steps_layout = QtWidgets.QVBoxLayout(self._steps)
        self._steps_layout.setContentsMargins(16, 4, 0, 0)
        self._steps_layout.setSpacing(theme.SPACING_TIGHT)
        self._steps.setVisible(False)
        layout.addWidget(self._steps)

        self._step_rows: dict[str, _ToolCallRow] = {}
        self._order: list[str] = []
        self._expanded = False

        for row in rows:
            self._adopt(row)
        self._sync_summary()

    def _adopt(self, row: "_ToolCallRow") -> None:
        row.setParent(self._steps)
        self._steps_layout.addWidget(row)
        self._step_rows[row._entry_id] = row
        self._order.append(row._entry_id)

    def add_tool(self, entry: Entry) -> None:
        # Parented at construction (to `self._steps`, `_adopt`'s own target),
        # not after: a parentless `_ToolCallRow` is a top-level window for
        # the moment between construction and `_adopt`'s `setParent` call,
        # and macOS hands it a real native window right then — this is the
        # site that actually produced the reported burst. A run of N
        # consecutive tool calls (an agent reading/editing many files in one
        # turn, unremarkable at "Claude ships hundreds of tool calls" scale)
        # collapses into one `_ToolGroupRow` after the second call, and every
        # call from the third one on used to go through here: measured with
        # 60 simulated consecutive tool calls, this one line accounted for
        # 58 stray windows (60 - 2, the two calls that already went through
        # an already-parented `_ToolCallRow`) — see
        # test_transcript_tool_group_add_tool_does_not_flash_a_window.
        self._adopt(_ToolCallRow(entry, self._steps))
        self._sync_summary()

    def update_tool(self, entry: Entry) -> None:
        row = self._step_rows.get(entry.id)
        if row is not None:
            row.update_from(entry)
        self._sync_summary()

    def _on_toggled(self, checked: bool) -> None:
        self._expanded = checked
        self._steps.setVisible(checked)

    def _sync_summary(self) -> None:
        views = [self._step_rows[entry_id]._tool for entry_id in self._order]
        running = next(
            (view for view in reversed(views) if view.status in ("pending", "in_progress")),
            None,
        )
        if running is not None:
            title = running.title if len(views) == 1 else f"{running.title} ({len(views)} steps)"
            self._summary.set_view(kind=running.kind, title=title, status=running.status)
            return

        # The whole run has finished. A single call keeps looking exactly
        # like a standalone tool call always has; several collapse into one
        # summary line instead of leaving every finished step on screen.
        last = views[-1]
        if len(views) == 1:
            self._summary.set_view(kind=last.kind, title=last.title, status=last.status)
            return
        status = "failed" if any(view.status == "failed" for view in views) else "completed"
        self._summary.set_view(kind="other", title=f"Ran {len(views)} tools", status=status)


class _PlanRow(QtWidgets.QWidget):
    """The agent's plan — a block listing the steps and their statuses."""

    def __init__(self, entry: Entry, parent=None) -> None:
        super().__init__(parent)
        self._layout = QtWidgets.QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(theme.SPACING_TIGHT)

        self._title = QtWidgets.QLabel("Plan", self)
        font = self._title.font()
        font.setBold(True)
        self._title.setFont(font)
        self._layout.addWidget(self._title)

        self._step_labels: list[QtWidgets.QLabel] = []
        self.update_from(entry)

    def update_from(self, entry: Entry) -> None:
        steps = entry.plan
        # Reuse the QLabels we already have where we can — a plan's step
        # count usually barely changes; on the first render, or when the
        # length does change, we just add or drop what's missing.
        while len(self._step_labels) < len(steps):
            label = QtWidgets.QLabel(self)
            label.setWordWrap(True)
            label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            self._step_labels.append(label)
            self._layout.addWidget(label)
        while len(self._step_labels) > len(steps):
            label = self._step_labels.pop()
            self._layout.removeWidget(label)
            label.hide()  # before orphaning: a parentless widget is a window
            label.setParent(None)
            label.deleteLater()

        for label, step in zip(self._step_labels, steps):
            glyph = {"pending": "○", "in_progress": "◐", "completed": "✓"}.get(step.status, "○")
            label.setText(f"{glyph} {step.content}")


__all__ = ["TranscriptView"]

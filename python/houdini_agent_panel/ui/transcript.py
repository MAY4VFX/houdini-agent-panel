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
from .qt import QtCore, QtGui, QtWidgets
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

        self._model: TranscriptModel | None = None
        self._rows: dict[str, QtWidgets.QWidget] = {}

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
        was_at_bottom = self._is_at_bottom()
        if entry_id is None:
            self._rebuild_all()
        else:
            self._refresh_one(entry_id)
        if was_at_bottom:
            self._scroll_to_bottom()

    # --- rebuilding -------------------------------------------------------

    def _rebuild_all(self) -> None:
        for row in list(self._rows.values()):
            self._layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()

        for entry in self._model.entries():
            if entry.kind == "permission":
                continue
            row = self._make_row(entry)
            self._rows[entry.id] = row
            self._layout.insertWidget(len(self._rows) - 1, row)

    def _refresh_one(self, entry_id: str) -> None:
        entries = self._model.entries()
        entry = next((e for e in entries if e.id == entry_id), None)

        if entry is not None and entry.kind == "permission":
            row = self._rows.pop(entry_id, None)
            if row is not None:
                self._layout.removeWidget(row)
                row.setParent(None)
                row.deleteLater()
            return

        if entry is None:
            # The protocol doesn't delete entries today, but we don't crash
            # if that ever changes — the row simply leaves the stage.
            row = self._rows.pop(entry_id, None)
            if row is not None:
                self._layout.removeWidget(row)
                row.setParent(None)
                row.deleteLater()
            return

        row = self._rows.get(entry_id)
        if row is not None:
            self._update_row(row, entry)
            return

        # A new entry goes at its own position among the drawn ones.
        # TranscriptModel always appends and never reorders, so the position
        # among already-drawn rows matches the entry's index in the model's
        # full list.
        index = sum(
            1
            for candidate in entries[: entries.index(entry)]
            if candidate.kind != "permission"
        )
        row = self._make_row(entry)
        self._rows[entry.id] = row
        self._layout.insertWidget(index, row)

    # --- building rows by kind ---------------------------------------------

    def _make_row(self, entry: Entry) -> QtWidgets.QWidget:
        if entry.kind == "activity":
            return _ActivityRow(entry)
        if entry.kind == "tool":
            return _ToolCallRow(entry)
        if entry.kind == "plan":
            return _PlanRow(entry)
        return _MessageRow(entry)

    def _update_row(self, row: QtWidgets.QWidget, entry: Entry) -> None:
        row.update_from(entry)

    # --- auto-scroll -------------------------------------------------------

    def _is_at_bottom(self) -> bool:
        bar = self.verticalScrollBar()
        return bar.value() >= bar.maximum() - _BOTTOM_EPSILON

    def _scroll_to_bottom(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        # Both supplied references use a 706–736 px reading rail.  Wider
        # Houdini panes add quiet gutters instead of stretching prose forever.
        gutter = max(14, (self.viewport().width() - 736) // 2)
        margins = self._layout.contentsMargins()
        self._layout.setContentsMargins(gutter, margins.top(), gutter, margins.bottom())


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
            label.setParent(None)
            label.deleteLater()

        for label, step in zip(self._step_labels, steps):
            glyph = {"pending": "○", "in_progress": "◐", "completed": "✓"}.get(step.status, "○")
            label.setText(f"{glyph} {step.content}")


__all__ = ["TranscriptView"]

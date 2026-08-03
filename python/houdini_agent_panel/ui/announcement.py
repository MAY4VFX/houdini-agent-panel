"""Strips above the feed (`NoticeStrip`) and above the input field (`BlockingNotice`).

Both classes deliberately know nothing about the network, nothing about
whether the record came from the feed or from a version check, and decide
nothing about `settings.seen_announcements` — that's the caller's business
(see docs/design.md, "Announcements"). All that lives here is the rendering
of something already parsed into `Announcement`/`Update`, plus signals about
which button was pressed.

`NoticeStrip` is a quiet line: it closes on a button and blocks nothing.
`BlockingNotice` is a popup ABOVE the input field: the widget itself blocks
nothing (`Composer.block_input`/`unblock_input` is the caller's job), it only
shows the message and reports button presses.
"""

from __future__ import annotations

from ..announcements import Announcement
from ..updates import Update
from .qt import QtWidgets, Signal


class NoticeStrip(QtWidgets.QWidget):
    """A line above the feed: an ordinary announcement or an update notice.

    `action_clicked(id, url)` — for an announcement `id` is `Announcement.id`
    and `url` is the pressed button's link. For an update `id` is
    `Update.target` (an agent_id or a package name) and `url` is always
    empty: there is no link there, the "Update" button is a signal to the
    caller to run the install itself.
    """

    action_clicked = Signal(str, str)
    dismissed = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._id = ""
        self._buttons_row: QtWidgets.QWidget | None = None

        self._label = QtWidgets.QLabel()
        self._label.setWordWrap(True)

        close_button = QtWidgets.QToolButton()
        close_button.setText("✕")
        close_button.setAutoRaise(True)
        close_button.setToolTip("Dismiss")
        close_button.clicked.connect(self._on_close)
        self._close_button = close_button

        self._layout = QtWidgets.QHBoxLayout(self)
        self._layout.setContentsMargins(6, 4, 6, 4)
        self._layout.addWidget(self._label, 1)
        self._layout.addWidget(close_button)

        self.setVisible(False)

    # --- public --------------------------------------------------------

    def show_notice(self, ann: Announcement) -> None:
        self._id = ann.id
        self._label.setText(ann.title)
        self._set_buttons([(b.label, b.url) for b in ann.buttons])
        self.setVisible(True)

    def show_update(self, update: Update) -> None:
        self._id = update.target
        self._label.setText(
            f"Update available: {update.label} ({update.current} → {update.latest})"
        )
        self._set_buttons([("Update", "")])
        self.setVisible(True)

    # --- internal --------------------------------------------------------

    def _set_buttons(self, buttons: list[tuple[str, str]]) -> None:
        if self._buttons_row is not None:
            self._layout.removeWidget(self._buttons_row)
            # `setParent(None)` detaches IMMEDIATELY (otherwise the widget
            # still counts as a child until the next event-loop pass, and
            # re-showing an announcement would briefly show the old and new
            # buttons at once).
            self._buttons_row.setParent(None)
            self._buttons_row.deleteLater()
            self._buttons_row = None
        if not buttons:
            return
        row = QtWidgets.QWidget()
        row_layout = QtWidgets.QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        for label, url in buttons:
            btn = QtWidgets.QPushButton(label)
            btn.clicked.connect(lambda checked=False, u=url: self.action_clicked.emit(self._id, u))
            row_layout.addWidget(btn)
        # Buttons come right after the text, before the close cross.
        self._layout.insertWidget(1, row)
        self._buttons_row = row

    def _on_close(self) -> None:
        ann_id = self._id
        self.setVisible(False)
        self.dismissed.emit(ann_id)


class BlockingNotice(QtWidgets.QWidget):
    """A popup above the input field — drawn strictly from the buttons it was sent.

    The widget itself never touches the input: that showing a
    `BlockingNotice` must block the composer's field, and pressing a button
    must unblock it, is decided by the code that shows it (which owns both
    the `BlockingNotice` and the `Composer`). Here there is only the message
    and a signal about the pressed button.
    """

    action_clicked = Signal(str, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._id = ""

        self._title = QtWidgets.QLabel()
        self._title.setWordWrap(True)
        self._title.setStyleSheet("font-weight: bold;")

        self._body = QtWidgets.QLabel()
        self._body.setWordWrap(True)

        self._buttons_row = QtWidgets.QWidget()
        self._buttons_layout = QtWidgets.QHBoxLayout(self._buttons_row)
        self._buttons_layout.setContentsMargins(0, 0, 0, 0)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self._title)
        layout.addWidget(self._body)
        layout.addWidget(self._buttons_row)

        self.setVisible(False)

    def show_notice(self, ann: Announcement) -> None:
        self._id = ann.id
        self._title.setText(ann.title)
        self._body.setText(ann.body)
        self._body.setVisible(bool(ann.body))

        while self._buttons_layout.count():
            item = self._buttons_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        for button in ann.buttons:
            btn = QtWidgets.QPushButton(button.label)
            btn.clicked.connect(lambda checked=False, url=button.url: self.action_clicked.emit(self._id, url))
            self._buttons_layout.addWidget(btn)
        self._buttons_layout.addStretch(1)

        self.setVisible(True)

    def hide_notice(self) -> None:
        """Not in the architecture.md contract, but the calling code needs it
        to take the popup down once a pressed button has unblocked the
        input — without this there is no way to close a `BlockingNotice`
        from outside."""
        self.setVisible(False)


__all__ = ["NoticeStrip", "BlockingNotice"]


class ConsentStrip(QtWidgets.QWidget):
    """A one-time question for the artist — today only the telemetry one.

    A strip rather than a modal, deliberately. A dialog in the middle of
    Houdini stops the work and demands an answer right now, and "will you
    share anonymous stats" has no right to do that. The strip waits, blocks
    nothing, and leaves once answered.

    The buttons carry deliberately equal weight: a question about collecting
    data must not have a visually "correct" answer nudging towards yes.
    """

    answered = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        self._label = QtWidgets.QLabel()
        self._label.setWordWrap(True)

        self._yes = QtWidgets.QPushButton("Allow")
        self._no = QtWidgets.QPushButton("No thanks")
        self._yes.clicked.connect(lambda: self._answer(True))
        self._no.clicked.connect(lambda: self._answer(False))

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.addWidget(self._label, 1)
        layout.addWidget(self._no)
        layout.addWidget(self._yes)

        self.setVisible(False)

    def ask(self, question: str) -> None:
        self._label.setText(question)
        self.setVisible(True)

    def _answer(self, allowed: bool) -> None:
        self.setVisible(False)
        self.answered.emit(allowed)

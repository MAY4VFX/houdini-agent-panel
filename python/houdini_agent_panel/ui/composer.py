"""Composer — the panel's input field: growth, attachments, slash commands, voice.

The project rule lives here almost line by line: a control shows up only if
`AgentInfo` (see `docs/architecture.md` §6) actually declared the matching
capability. We decide nothing and invent nothing on top of the protocol — no
`supports_image`/`supports_embedded_context`, no "+" button; no
`availableModes`, no mode chip; no `configOptions`, no model picker; no
`audio` and no whisper endpoint, no microphone.

`submitted` hands out `list[dict]` in ACP content-block shape (see
`docs/facts/acp-sdk.md` §4) — keys exactly as they go on the wire
(`"mimeType"`, not `"mime_type"`), because `client.py` builds pydantic models
out of them via `cls(**block)`, and those models' fields are declared with
camelCase aliases.
"""

from __future__ import annotations

import base64
import mimetypes
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .chips import ChoiceButton, ModeChip
from .qt import QtCore, QtGui, QtWidgets, Signal
from .thinking import _BuddySprite
from .voice import VoiceButton

if TYPE_CHECKING:
    from ..client import AgentInfo
    from ..sessions import AvailableCommand, SessionMode, Usage

_MIN_LINES = 1
_MAX_LINES = 6
_MAX_POPUP_HEIGHT = 360
_DEFAULT_PLACEHOLDER = "What should change in the scene?"
_RAIL_WIDTH = 736
#: The composer never forces the panel wider than this. Its own rail wants
#: 736px, and a `setFixedWidth` child hands its width straight to the parent's
#: minimum — which pinned the whole panel at 736px and made it impossible to
#: dock the panel in a normal, narrow Houdini pane.
_MIN_RAIL_WIDTH = 180


class _GrowingTextEdit(QtWidgets.QPlainTextEdit):
    """The input field: Enter sends, Shift+Enter breaks the line.

    While the slash popup is up (`popup_active`), arrows/Enter/Esc don't edit
    text — they move through the popup and close it. The widget itself knows
    nothing about the popup's contents, it only reports the intent to the
    composer.
    """

    submit_requested = Signal()
    navigate_requested = Signal(int)  # -1 / +1
    escape_requested = Signal()
    accept_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.popup_active = False
        self.setTabChangesFocus(True)
        self.setLineWrapMode(QtWidgets.QPlainTextEdit.WidgetWidth)

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:  # noqa: N802 - Qt override
        key = event.key()
        if self.popup_active and key in (QtCore.Qt.Key_Up, QtCore.Qt.Key_Down):
            self.navigate_requested.emit(-1 if key == QtCore.Qt.Key_Up else 1)
            event.accept()
            return
        if self.popup_active and key == QtCore.Qt.Key_Escape:
            self.escape_requested.emit()
            event.accept()
            return
        if key in (QtCore.Qt.Key_Return, QtCore.Qt.Key_Enter):
            if self.popup_active:
                self.accept_requested.emit()
                event.accept()
                return
            if event.modifiers() & QtCore.Qt.ShiftModifier:
                super().keyPressEvent(event)  # line break
                return
            self.submit_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class _CommandPopup(QtWidgets.QListWidget):
    """The slash-command list above the input field. An ordinary child widget,
    not a system popup — that keeps navigation entirely in `_GrowingTextEdit`'s
    hands (keyboard focus never leaves the input field)."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("commandPalette")
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setUniformItemSizes(True)
        self.setSpacing(1)
        self.setStyleSheet(
            "QListWidget#commandPalette {"
            " background: #282828; border: 1px solid #414141; border-radius: 15px;"
            " padding: 5px; outline: none;"
            "}"
            "QListWidget#commandPalette::item {"
            " min-height: 34px; border: none; border-radius: 8px;"
            "}"
            "QListWidget#commandPalette::item:selected { background: #3a3a3a; }"
        )
        self.hide()

    def set_commands(self, commands: list[Any]) -> None:
        self.clear()
        for cmd in commands:
            item = QtWidgets.QListWidgetItem()
            item.setData(QtCore.Qt.UserRole, cmd.name)
            item.setSizeHint(QtCore.QSize(0, 34))
            self.addItem(item)
            row = QtWidgets.QWidget(self)
            row.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
            row_layout = QtWidgets.QHBoxLayout(row)
            row_layout.setContentsMargins(10, 0, 10, 0)
            row_layout.setSpacing(18)
            name = QtWidgets.QLabel(f"/{cmd.name}", row)
            name.setStyleSheet("color: #e5e3df; background: transparent;")
            row_layout.addWidget(name)
            row_layout.addStretch(1)
            description = QtWidgets.QLabel(cmd.description or "", row)
            description.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            description.setStyleSheet("color: #8f8c87; background: transparent;")
            row_layout.addWidget(description)
            self.setItemWidget(item, row)
        if self.count():
            self.setCurrentRow(0)

    def current_name(self) -> str | None:
        item = self.currentItem()
        return item.data(QtCore.Qt.UserRole) if item is not None else None

    def move_selection(self, delta: int) -> None:
        if self.count() == 0:
            return
        row = (self.currentRow() + delta) % self.count()
        self.setCurrentRow(row)


def _format_tokens(n: int) -> str:
    """Compact number for the token counter: 950, 1.2K, 3M."""
    if n < 1000:
        return str(n)
    for threshold, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= threshold:
            text = f"{n / threshold:.1f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    return str(n)  # pragma: no cover - unreachable for n >= 1000


def build_attachment_block(path: Path, info: "AgentInfo") -> dict | None:
    """File -> a ready ACP content block, shaped by the capability that exists.

    An image with `supports_image` becomes an `image` block. Otherwise, with
    `supports_embedded_context`, an embedded `resource`: a text file as text,
    anything else as a base64 blob. Neither capability fits — `None`: there is
    nothing to attach with, and the agent wouldn't understand the block.
    """
    mime_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
    if info.supports_image and mime_type.startswith("image/"):
        data = path.read_bytes()
        return {
            "type": "image",
            "data": base64.b64encode(data).decode("ascii"),
            "mimeType": mime_type,
        }
    if info.supports_embedded_context:
        uri = path.resolve().as_uri()
        try:
            text = path.read_text("utf-8")
        except (UnicodeDecodeError, OSError):
            data = path.read_bytes()
            return {
                "type": "resource",
                "resource": {
                    "uri": uri,
                    "blob": base64.b64encode(data).decode("ascii"),
                    "mimeType": mime_type,
                },
            }
        return {"type": "resource", "resource": {"uri": uri, "text": text, "mimeType": mime_type}}
    return None


#: Attachment chips sit inside the input card — they must never drive its height.
_ATTACHMENT_CHIP_HEIGHT = 28
_ATTACHMENT_THUMBNAIL = 20


def _attachment_thumbnail(block: dict) -> "QtGui.QPixmap | None":
    """A small preview for an image block, None for anything else."""
    if block.get("type") != "image":
        return None
    data = block.get("data")
    if not isinstance(data, str):
        return None
    try:
        raw = base64.b64decode(data)
    except (ValueError, TypeError):
        return None
    pixmap = QtGui.QPixmap()
    if not pixmap.loadFromData(raw):
        return None
    return pixmap.scaled(
        _ATTACHMENT_THUMBNAIL,
        _ATTACHMENT_THUMBNAIL,
        QtCore.Qt.KeepAspectRatio,
        QtCore.Qt.SmoothTransformation,
    )


def _attachment_label(block: dict) -> str:
    kind = block.get("type")
    if kind == "image":
        return "Image"
    if kind == "audio":
        return "Audio"
    if kind == "resource":
        uri = (block.get("resource") or {}).get("uri", "")
        return uri.rsplit("/", 1)[-1] if uri else "File"
    return "Attachment"


class _ComposerSurface(QtWidgets.QFrame):
    """The rounded input card. A click anywhere on it starts typing.

    The text edit only occupies part of the card — there is padding around
    it and a row of controls below. A click on that padding used to land on
    the frame and do nothing, so the field looked like it needed a
    double-click to wake up. Anywhere that looks like the input field has to
    behave like it.
    """

    def __init__(self, target: QtWidgets.QWidget, parent=None) -> None:
        super().__init__(parent)
        self._target = target
        # A focus proxy is what actually survives Houdini. Intercepting the
        # mouse press alone wasn't enough: Houdini's pane tab eats the first
        # click to activate itself, so the event never reached this widget
        # and the field looked like it needed a double-click. A proxy makes
        # Qt route focus to the input whenever anything hands focus to the
        # card, no matter which click delivered it.
        self.setFocusPolicy(QtCore.Qt.ClickFocus)
        self.setFocusProxy(target)

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:
        super().mousePressEvent(event)
        if self._target.isEnabled():
            self._target.setFocus(QtCore.Qt.MouseFocusReason)


class Composer(QtWidgets.QWidget):
    """Bottom of the panel: growing field, "+", microphone, chips, counter, send/stop."""

    submitted = Signal(list)  # list[dict] — ready ACP content blocks
    cancelled = Signal()
    mode_selected = Signal(str)
    config_option_selected = Signal(str, str)  # config_id, value
    attachment_rejected = Signal(str)
    buddy_selected = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._info: "AgentInfo | None" = None
        self._busy = False
        self._blocked = False
        self._attachments: list[dict] = []
        self._all_commands: list["AvailableCommand"] = []
        self._config_chips: list[ChoiceButton] = []

        self.setAcceptDrops(True)

        # --- attachments (a chip row above the field, only shown when there is something)
        self._attachments_bar = QtWidgets.QWidget()
        # Without a vertical Maximum the bar takes every spare pixel the
        # column has and the whole input card balloons to fill the panel —
        # which is exactly what attaching a file used to do.
        self._attachments_bar.setSizePolicy(
            QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Maximum
        )
        self._attachments_layout = QtWidgets.QHBoxLayout(self._attachments_bar)
        self._attachments_layout.setContentsMargins(0, 0, 0, 4)
        self._attachments_layout.setSpacing(6)
        self._attachments_bar.setVisible(False)

        # --- input field
        self._text_edit = _GrowingTextEdit(self)
        self._text_edit.setObjectName("composerInput")
        self._text_edit.setPlaceholderText(_DEFAULT_PLACEHOLDER)
        self._text_edit.setFrameShape(QtWidgets.QFrame.NoFrame)
        input_palette = self._text_edit.palette()
        placeholder_role = getattr(QtGui.QPalette, "PlaceholderText", QtGui.QPalette.Text)
        input_palette.setColor(placeholder_role, QtGui.QColor("#85827d"))
        self._text_edit.setPalette(input_palette)
        self._text_edit.textChanged.connect(self._on_text_changed)
        self._text_edit.submit_requested.connect(self._submit)
        self._text_edit.navigate_requested.connect(self._on_popup_navigate)
        self._text_edit.escape_requested.connect(self._hide_popup)
        self._text_edit.accept_requested.connect(self._on_popup_accept)

        self._popup = _CommandPopup(self)

        # --- left-hand buttons: attachments, voice
        self._attach_button = QtWidgets.QToolButton()
        self._attach_button.setObjectName("composerTool")
        self._attach_button.setText("+")
        self._attach_button.setToolTip("Attach a file")
        self._attach_button.setVisible(False)
        self._attach_button.clicked.connect(self._on_attach_clicked)

        self._voice_button = VoiceButton(self)
        self._voice_button.setObjectName("composerTool")
        self._voice_button.recorded_audio.connect(self._on_voice_audio)
        self._voice_button.transcribed_text.connect(self._on_voice_text)

        # --- mode chip, straight from the session's availableModes
        self.mode_chip = ModeChip(self)
        self.mode_chip.mode_selected.connect(self.mode_selected.emit)

        # --- the agent's own settings (model, reasoning effort, fast mode…).
        # One chip per option the agent declared, rebuilt whenever it changes
        # its mind. Empty by default: an agent that offers nothing gets no
        # chips rather than an invented "model" dropdown.
        self._config_bar = QtWidgets.QWidget()
        self._config_layout = QtWidgets.QHBoxLayout(self._config_bar)
        self._config_layout.setContentsMargins(0, 0, 0, 0)
        self._config_layout.setSpacing(3)
        self._config_bar.setVisible(False)

        # --- right-hand side: counter, send/stop
        self._usage_label = QtWidgets.QLabel()
        self._usage_label.setVisible(False)

        self._send_button = QtWidgets.QPushButton("↑")
        self._send_button.setObjectName("composerSend")
        self._send_button.setFixedSize(32, 32)
        self._send_button.setToolTip("Send")
        self._send_button.clicked.connect(self._on_send_clicked)

        action_row = QtWidgets.QHBoxLayout()
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(3)
        action_row.addWidget(self.mode_chip)
        action_row.addWidget(self._attach_button)
        action_row.addStretch(1)
        action_row.addWidget(self._config_bar)
        action_row.addWidget(self._usage_label)
        action_row.addSpacing(12)
        action_row.addWidget(self._voice_button)
        action_row.addWidget(self._send_button)

        self._surface = _ComposerSurface(self._text_edit, self)
        self._surface.setObjectName("composerSurface")
        self._surface.setMinimumHeight(99)
        surface_layout = QtWidgets.QVBoxLayout(self._surface)
        surface_layout.setContentsMargins(8, 7, 8, 8)
        surface_layout.setSpacing(0)
        surface_layout.addWidget(self._attachments_bar)
        surface_layout.addWidget(self._text_edit)
        surface_layout.addLayout(action_row)

        main_layout = QtWidgets.QVBoxLayout(self)
        main_layout.setContentsMargins(0, 42, 0, 14)
        main_layout.setAlignment(QtCore.Qt.AlignHCenter)
        main_layout.addWidget(self._surface, 0, QtCore.Qt.AlignHCenter)

        # Houdini hands focus to the panel, not to a specific widget inside
        # it. Without a proxy that focus lands nowhere and the artist has to
        # click again to start typing.
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setFocusProxy(self._text_edit)

        self._buddy = _BuddySprite(self)
        self._buddy.clicked.connect(self.buddy_selected.emit)
        self._buddy.raise_()

        self.setStyleSheet(
            "QFrame#composerSurface {"
            " background: palette(base);"
            " border: 1px solid palette(mid);"
            " border-radius: 18px;"
            "}"
            "QPlainTextEdit#composerInput {"
            " background: transparent;"
            " border: none;"
            " padding: 4px 5px;"
            "}"
            "QPushButton#composerSend {"
            " border: none;"
            " border-radius: 16px;"
            " background: palette(text);"
            " color: palette(base);"
            " font-weight: bold;"
            "}"
            "QPushButton#composerSend:disabled {"
            " background: palette(mid);"
            " color: palette(disabled, text);"
            "}"
            "QToolButton#composerTool {"
            " border: none;"
            " background: transparent;"
            " padding: 4px;"
            "}"
            "QToolButton#composerTool:hover {"
            " background: palette(alternate-base);"
            " border-radius: 6px;"
            "}"
        )

        self._adjust_text_height()

    def minimumSizeHint(self) -> QtCore.QSize:  # noqa: N802 - Qt override
        """Never demand the rail's full width from the panel around us.

        `_surface.setFixedWidth()` makes the surface's minimum equal to its
        maximum, and Qt hands a child's minimum straight up to the parent —
        so the composer used to claim a 736px minimum, the panel inherited
        it, and Houdini could not dock the panel any narrower than that.
        """
        hint = super().minimumSizeHint()
        return QtCore.QSize(min(hint.width(), _MIN_RAIL_WIDTH), hint.height())

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._surface.setFixedWidth(
            max(_MIN_RAIL_WIDTH, min(_RAIL_WIDTH, self.width() - 28))
        )
        # The layout applies geometry only after resizeEvent, so X comes from
        # the same centering rule directly instead of a stale mapTo().
        surface_x = (self.width() - self._surface.width()) // 2
        surface_y = self._surface.y()
        self._buddy.move(
            surface_x + self._surface.width() - self._buddy.width() - 20,
            surface_y - self._buddy.height() + 12,
        )
        self._buddy.raise_()

    # --- public contract (docs/architecture.md §10) -----------------------

    def set_capabilities(self, info: "AgentInfo | None", whisper: str) -> None:
        """Recompute the visibility of "+" and the microphone for a fresh `AgentInfo`.

        `info=None` (the agent disconnected, or hasn't connected yet) hides
        both controls — except when a whisper endpoint alone is enough for
        the microphone, which `VoiceButton.configure` decides, not this.
        """
        self._info = info
        can_attach = info is not None and (info.supports_image or info.supports_embedded_context)
        self._attach_button.setVisible(can_attach)
        supports_audio = info is not None and info.supports_audio
        self._voice_button.configure(supports_audio=supports_audio, whisper_endpoint=whisper)

    def set_modes(self, modes: list["SessionMode"], current_id: str | None) -> None:
        """Facade over `mode_chip.set_modes` — the panel feeds session modes
        here instead of reaching into the nested widget (architecture.md §10:
        widgets talk through public API, never through someone else's
        private or nested attributes)."""
        self.mode_chip.set_modes(modes, current_id)

    def set_config_options(self, options: list) -> None:
        """Draw one chip per agent-side setting, and only for what it sent.

        This is where the model picker lives. ACP has no separate "model"
        method: an agent publishes model, reasoning effort and fast mode as
        session config options, each with its own choices and current value.
        Everything visible here — labels, order, which options exist at all —
        is the agent's, so an agent with no options simply gets no chips.

        Each element is duck-typed: `id`, `name`, `current_value` and
        `choices` of `value`/`name`. That keeps this widget from importing
        `client.py`, exactly like the rest of `ui/`.
        """
        while self._config_layout.count():
            item = self._config_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._config_chips = []

        for option in options:
            choices = list(getattr(option, "choices", ()) or ())
            if len(choices) < 2:
                # Nothing to pick between — a one-entry dropdown is a label
                # pretending to be a control.
                continue
            chip = ChoiceButton(self._config_bar)
            chip.setToolTip(
                getattr(option, "description", "") or getattr(option, "name", "") or ""
            )
            chip.blockSignals(True)
            try:
                for choice in choices:
                    chip.addItem(
                        str(getattr(choice, "name", "") or getattr(choice, "value", "")),
                        str(getattr(choice, "value", "")),
                    )
                index = chip.findData(str(getattr(option, "current_value", "") or ""))
                if index >= 0:
                    chip.setCurrentIndex(index)
            finally:
                chip.blockSignals(False)
            option_id = str(getattr(option, "id", "") or "")
            chip.activated.connect(
                lambda index, c=chip, oid=option_id: self._on_config_activated(c, oid, index)
            )
            self._config_layout.addWidget(chip)
            self._config_chips.append(chip)

        self._config_bar.setVisible(bool(self._config_chips))

    def set_buddy(self, key: str) -> None:
        self._buddy.set_buddy(key)

    def trigger_buddy(self) -> None:
        self._buddy.start_action()

    def popover_anchor_rect(self, target: QtWidgets.QWidget) -> QtCore.QRect:
        """Composer surface in coordinates of an external overlay host."""
        top_left = self._surface.mapTo(target, QtCore.QPoint(0, 0))
        return QtCore.QRect(top_left, self._surface.size())

    def enable_preview_microphone(self) -> None:
        """Show the affordance in the standalone preview without inventing a capability."""
        self._voice_button.setVisible(True)
        self._voice_button.setToolTip("Microphone (no audio backend in preview)")

    def _on_config_activated(self, chip: ChoiceButton, option_id: str, index: int) -> None:
        value = chip.itemData(index)
        if option_id and value:
            self.config_option_selected.emit(option_id, str(value))

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._send_button.setText("■" if busy else "↑")
        self._send_button.setToolTip("Stop" if busy else "Send")

    def set_commands(self, commands: list["AvailableCommand"]) -> None:
        self._all_commands = list(commands)
        if self._popup.isVisible():
            self._update_slash_popup()

    def set_usage(self, usage: "Usage | None") -> None:
        """Token counter, fed either shape that reaches it in practice.

        The real ACP `usage_update` carries `used`/`size` — tokens currently
        in context vs. the whole context window, there is no "total tokens"
        field at all. `sessions.Usage` (`total_tokens`), used by the dev
        preview and by tests, is the simpler synthetic shape. Showing
        `used/size` when it's there is also the more useful number for an
        artist: how full the context window is, not a lifetime counter.
        """
        if usage is None:
            self._usage_label.setVisible(False)
            return
        used = getattr(usage, "used", None)
        size = getattr(usage, "size", None)
        if used is not None and size is not None:
            text = f"{_format_tokens(used)}/{_format_tokens(size)}"
            self._usage_label.setToolTip("Tokens in context / context window size")
        else:
            text = _format_tokens(getattr(usage, "total_tokens", 0))
            self._usage_label.setToolTip("Tokens used")
        self._usage_label.setText(text)
        self._usage_label.setVisible(True)

    def block_input(self, reason: str) -> None:
        """Blocks ONLY the input field and the send/attach/voice buttons — the
        feed, scrolling, closing the panel and the whole rest of Houdini have
        no idea the composer is currently unusable (design.md)."""
        self._blocked = True
        self._text_edit.setEnabled(False)
        self._text_edit.setPlaceholderText(reason)
        self._send_button.setEnabled(False)
        self._attach_button.setEnabled(False)
        self._voice_button.setEnabled(False)

    def unblock_input(self) -> None:
        self._blocked = False
        self._text_edit.setEnabled(True)
        self._text_edit.setPlaceholderText(_DEFAULT_PLACEHOLDER)
        self._send_button.setEnabled(True)
        self._attach_button.setEnabled(True)
        self._voice_button.setEnabled(True)

    def is_input_blocked(self) -> bool:
        """Not part of architecture.md §10, but calling code needs it (the
        panel checks this in tests and, most likely, in its own logic) — a
        plain getter for the state `block_input`/`unblock_input` already keep."""
        return self._blocked

    def hideEvent(self, event: QtGui.QHideEvent) -> None:  # noqa: N802 - Qt override
        """Take the slash palette down with the composer.

        The palette is reparented to the panel so it isn't clipped by the
        short composer widget (`_position_popup`), which also means hiding
        the composer no longer hides it. Switching to settings with a slash
        popup open used to leave a list of commands floating over the
        settings form.
        """
        super().hideEvent(event)
        self._hide_popup()

    # --- attachments: "+", drag & drop ------------------------------------

    def add_attachment(self, path: Path) -> bool:
        """Add a file as an attachment for the next send.

        `False` — the current agent's capabilities don't allow this
        particular file (not an image, and no `embeddedContext`), or the
        agent isn't connected yet.
        """
        if self._info is None:
            return False
        try:
            block = build_attachment_block(Path(path), self._info)
        except OSError:
            # Unreadable file (permissions, a dead symlink, a network share
            # that went away) must not look the same as "the agent refused".
            return False
        if block is None:
            return False
        self._attachments.append(block)
        self._refresh_attachments_bar()
        return True

    def _attachment_filter(self) -> str:
        """A file filter that matches what this agent actually accepts.

        Without it the dialog offers every file on disk and then the panel
        silently drops whatever the agent can't take — which reads as "the
        attach button is broken".
        """
        if self._info is None:
            return "All files (*)"
        images = "Images (*.png *.jpg *.jpeg *.gif *.webp *.bmp *.tif *.tiff)"
        if self._info.supports_embedded_context:
            # The agent takes embedded resources, so anything goes; images
            # are listed first because that's the common case.
            return f"All files (*);;{images}"
        if self._info.supports_image:
            return f"{images};;All files (*)"
        return "All files (*)"

    def _on_attach_clicked(self) -> None:
        if self._info is None:
            self.attachment_rejected.emit("Connect an agent before attaching files.")
            return
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, "Attach files", "", self._attachment_filter()
        )
        rejected: list[str] = []
        for raw_path in paths:
            if not self.add_attachment(Path(raw_path)):
                rejected.append(Path(raw_path).name)
        if rejected:
            # Never drop a file without a word. The agent's capabilities are
            # the reason, and the artist has no way to guess them.
            self.attachment_rejected.emit(
                "This agent can't take: " + ", ".join(rejected)
            )

    def _remove_attachment(self, index: int) -> None:
        if 0 <= index < len(self._attachments):
            del self._attachments[index]
            self._refresh_attachments_bar()

    def _refresh_attachments_bar(self) -> None:
        while self._attachments_layout.count():
            item = self._attachments_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                # `setParent(None)` right away: otherwise the chip still
                # counts as a child of the composer until the next event-loop pass.
                widget.setParent(None)
                widget.deleteLater()
        for index, block in enumerate(self._attachments):
            self._attachments_layout.addWidget(self._build_attachment_chip(index, block))
        # A trailing stretch keeps chips packed to the left instead of
        # spreading across the whole card.
        self._attachments_layout.addStretch(1)
        self._attachments_bar.setVisible(bool(self._attachments))

    def _build_attachment_chip(self, index: int, block: dict) -> QtWidgets.QWidget:
        """One attachment: a thumbnail for images, a name for everything else.

        An image attached with no visible preview leaves the artist guessing
        whether the click even registered — the point of the chip is to prove
        the file is really going along.
        """
        chip = QtWidgets.QFrame()
        chip.setObjectName("attachmentChip")
        chip.setFixedHeight(_ATTACHMENT_CHIP_HEIGHT)
        layout = QtWidgets.QHBoxLayout(chip)
        layout.setContentsMargins(4, 2, 2, 2)
        layout.setSpacing(6)

        thumbnail = _attachment_thumbnail(block)
        if thumbnail is not None:
            preview = QtWidgets.QLabel()
            preview.setPixmap(thumbnail)
            preview.setFixedSize(thumbnail.size())
            layout.addWidget(preview)

        label = QtWidgets.QLabel(_attachment_label(block))
        label.setToolTip(_attachment_label(block))
        layout.addWidget(label)

        remove = QtWidgets.QToolButton()
        remove.setText("✕")
        remove.setAutoRaise(True)
        remove.setToolTip("Remove attachment")
        remove.clicked.connect(lambda checked=False, i=index: self._remove_attachment(i))
        layout.addWidget(remove)
        return chip

    def dragEnterEvent(self, event: QtGui.QDragEnterEvent) -> None:  # noqa: N802
        if self._info is not None and event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QtGui.QDropEvent) -> None:  # noqa: N802
        added_any = False
        rejected: list[str] = []
        for url in event.mimeData().urls():
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            if self.add_attachment(path):
                added_any = True
            else:
                rejected.append(path.name)
        if rejected:
            # Same rule as the "+" button: a file is never dropped without a
            # word. Silence here read as "drag and drop doesn't work".
            self.attachment_rejected.emit("This agent can't take: " + ", ".join(rejected))
        if added_any:
            event.acceptProposedAction()
        else:
            event.ignore()

    # --- voice -------------------------------------------------------------

    def _on_voice_audio(self, block: dict) -> None:
        self._attachments.append(block)
        self._refresh_attachments_bar()

    def _on_voice_text(self, text: str) -> None:
        current = self._text_edit.toPlainText()
        combined = f"{current} {text}".strip() if current else text
        self._text_edit.setPlainText(combined)
        cursor = self._text_edit.textCursor()
        cursor.movePosition(QtGui.QTextCursor.End)
        self._text_edit.setTextCursor(cursor)

    # --- sending -------------------------------------------------------------

    def _gather_blocks(self) -> list[dict]:
        blocks: list[dict] = []
        text = self._text_edit.toPlainText().strip()
        if text:
            blocks.append({"type": "text", "text": text})
        blocks.extend(self._attachments)
        return blocks

    def _submit(self) -> None:
        if self._blocked or self._busy:
            # Never fail in silence. A stuck busy flag turned the send button
            # into a stop button that did nothing when pressed, and from the
            # outside that is indistinguishable from a dead panel.
            self.attachment_rejected.emit(
                "Still waiting on the previous turn. Press stop, or start a new conversation."
                if self._busy
                else "Input is locked by a notice above."
            )
            return
        blocks = self._gather_blocks()
        if not blocks:
            return
        self.submitted.emit(blocks)
        self._text_edit.clear()
        self._attachments = []
        self._refresh_attachments_bar()

    def _on_send_clicked(self) -> None:
        if self._busy:
            self.cancelled.emit()
        else:
            self._submit()

    # --- input field growth --------------------------------------------------

    def _on_text_changed(self) -> None:
        self._adjust_text_height()
        self._update_slash_popup()

    def _adjust_text_height(self) -> None:
        """Grow with the text, then scroll — not the other way round.

        Height has to come from the document's laid-out size, because that is
        the only thing that accounts for WRAPPING. Counting "\n" only sees
        explicit line breaks, so one long paragraph typed without a single
        Enter stayed one line tall and went straight to a scrollbar — which
        is exactly what it looked like from the outside: a field that refuses
        to grow.

        The newline count survives as a fallback: without a real layout pass
        (headless tests, a widget that was never shown) the document reports
        nothing, and a zero-height input field would be worse than an
        approximate one.
        """
        line_height = QtGui.QFontMetrics(self._text_edit.font()).lineSpacing()
        padding = 22
        min_height = max(55, line_height * _MIN_LINES + padding)
        max_height = line_height * _MAX_LINES + padding

        # Count the lines the layout actually produced. `setTextWidth` is not
        # an option here: with WidgetWidth wrapping the widget owns the
        # document's width, and setting it by hand fights that and yields a
        # height that never changes.
        visual_lines = 0
        document = self._text_edit.document()
        block = document.begin()
        while block.isValid():
            layout = block.layout()
            visual_lines += layout.lineCount() if layout is not None else 0
            block = block.next()
        if visual_lines <= 0:
            # No layout pass yet (headless tests, a widget never shown). An
            # approximate height beats a zero-height input field.
            visual_lines = max(1, self._text_edit.toPlainText().count("\n") + 1)
        laid_out = line_height * visual_lines

        new_height = max(min_height, min(laid_out + padding, max_height))
        self._text_edit.setFixedHeight(int(new_height))
        # A scrollbar only once there is genuinely no more room to grow.
        self._text_edit.setVerticalScrollBarPolicy(
            QtCore.Qt.ScrollBarAsNeeded
            if laid_out + padding > max_height
            else QtCore.Qt.ScrollBarAlwaysOff
        )

    # --- slash commands ---------------------------------------------------

    def _slash_query(self) -> str | None:
        text = self._text_edit.toPlainText()
        if not text.startswith("/"):
            return None
        rest = text[1:]
        if " " in rest or "\n" in rest:
            return None
        return rest

    def _update_slash_popup(self) -> None:
        query = self._slash_query()
        if query is None or not self._all_commands:
            self._hide_popup()
            return
        matches = [c for c in self._all_commands if c.name.lower().startswith(query.lower())]
        if not matches:
            self._hide_popup()
            return
        self._popup.set_commands(matches)
        self._position_popup()
        self._popup.show()
        self._popup.raise_()
        self._text_edit.popup_active = True

    def _hide_popup(self) -> None:
        self._popup.hide()
        self._text_edit.popup_active = False

    def _position_popup(self) -> None:
        # The palette belongs to the panel overlay rather than the short
        # composer widget.  Otherwise its upper rows are clipped at the
        # composer's edge and Qt exposes scrollbars for the remaining sliver.
        overlay = self.parentWidget() or self
        if self._popup.parentWidget() is not overlay:
            self._popup.setParent(overlay)
        edit_pos = self._text_edit.mapTo(overlay, QtCore.QPoint(0, 0))
        edit_geo = QtCore.QRect(edit_pos, self._text_edit.size())
        row_height = self._popup.sizeHintForRow(0) if self._popup.count() else 34
        desired = row_height * max(self._popup.count(), 1) + 12
        available = max(row_height + 12, edit_geo.y() - 8)
        height = min(desired, _MAX_POPUP_HEIGHT, available)
        self._popup.setGeometry(
            edit_geo.x(), edit_geo.y() - height - 8, edit_geo.width(), height
        )

    def _on_popup_navigate(self, delta: int) -> None:
        self._popup.move_selection(delta)

    def _on_popup_accept(self) -> None:
        name = self._popup.current_name()
        if name is not None:
            self._text_edit.setPlainText(f"/{name} ")
            cursor = self._text_edit.textCursor()
            cursor.movePosition(QtGui.QTextCursor.End)
            self._text_edit.setTextCursor(cursor)
        self._hide_popup()


__all__ = ["Composer", "build_attachment_block"]

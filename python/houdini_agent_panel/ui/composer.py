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
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import attachments, theme
from .chips import ChoiceButton, ModeChip
from .boot_status import BootStatus
from .qt import QtCore, QtGui, QtWidgets, Signal, clear_layout, discard
from .thinking import BuddyEntrance, _BuddySprite
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
#: Below this surface width, the "Report a bug…" footer link hides rather
#: than clip or wrap — see `_position_bug_report_link`'s own docstring.
#: `_MIN_RAIL_WIDTH` (180) plus enough for the link's own text at the panel's
#: default font, measured, plus a margin so it never sits flush against the
#: surface's own right edge even right at the threshold.
_LINK_MIN_SURFACE_WIDTH = 260


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
        self._commands: list[Any] = []
        self._choice_values: list[str] = []
        self._hint_text: str = ""
        #: Which of the three shapes below is currently on screen — a theme
        #: refresh (`showEvent`) has to re-render the right one, not always
        #: `set_commands` regardless of what's actually showing.
        self._render_kind: str = "commands"
        self._apply_theme()
        self.hide()

    def _apply_theme(self) -> None:
        """(Re)build every colour here from the live theme — see
        `ChoiceButton._apply_theme` for why this isn't a constant baked in
        once. Also rebuilds the visible rows: their labels paint their own
        colour directly, which a plain stylesheet reapply wouldn't touch.
        """
        bg = theme.to_hex(theme.popup_background())
        border = theme.to_hex(theme.popup_border())
        selected_bg = theme.to_hex(theme.popup_hover_background())
        self.setStyleSheet(
            "QListWidget#commandPalette {"
            f" background: {bg}; border: 1px solid {border}; border-radius: 15px;"
            " padding: 5px; outline: none;"
            "}"
            "QListWidget#commandPalette::item {"
            " min-height: 34px; border: none; border-radius: 8px;"
            "}"
            f"QListWidget#commandPalette::item:selected {{ background: {selected_bg}; }}"
        )
        if self._render_kind == "commands" and self._commands:
            self.set_commands(self._commands)
        elif self._render_kind == "choices" and self._choice_values:
            self.set_choice_rows(self._choice_values)
        elif self._render_kind == "hint" and self._hint_text:
            self.set_hint_only(self._hint_text)

    def showEvent(self, event: QtGui.QShowEvent) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._apply_theme()

    def set_commands(self, commands: list[Any]) -> None:
        self._render_kind = "commands"
        self._commands = list(commands)
        self.clear()
        # Straight from the live palette (not `theme.color()`'s `hou.qt`-first
        # path) — see `theme.popup_background`'s docstring for why.
        name_color = theme.to_hex(theme.palette().color(QtGui.QPalette.Text))
        description_color = theme.to_hex(
            theme.palette().color(QtGui.QPalette.Disabled, QtGui.QPalette.Text)
        )
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
            name.setStyleSheet(f"color: {name_color}; background: transparent;")
            row_layout.addWidget(name)
            if _is_marketplace_command(cmd):
                tag = QtWidgets.QLabel("plugin", row)
                tag.setStyleSheet(
                    f"color: {description_color}; background: transparent;"
                    " border: 1px solid palette(mid); border-radius: 4px; padding: 0 4px;"
                )
                row_layout.addWidget(tag)
            row_layout.addStretch(1)
            description = QtWidgets.QLabel(cmd.description or "", row)
            description.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
            description.setStyleSheet(f"color: {description_color}; background: transparent;")
            row_layout.addWidget(description)
            self.setItemWidget(item, row)
        if self.count():
            self.setCurrentRow(0)

    def set_choice_rows(self, values: list[str]) -> None:
        """The argument the artist is typing has a conservatively-parsed
        `<a|b|c>`/`[a|b]` hint (`_parse_enum_hint`) — one selectable row per
        value, same keyboard model as `set_commands` (`current_name`,
        `move_selection`), but the payload is the raw value to insert."""
        self._render_kind = "choices"
        self._choice_values = list(values)
        self.clear()
        name_color = theme.to_hex(theme.palette().color(QtGui.QPalette.Text))
        for value in values:
            item = QtWidgets.QListWidgetItem()
            item.setData(QtCore.Qt.UserRole, value)
            item.setSizeHint(QtCore.QSize(0, 34))
            self.addItem(item)
            label = QtWidgets.QLabel(value, self)
            label.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
            label.setStyleSheet(f"color: {name_color}; background: transparent; padding: 0 10px;")
            self.setItemWidget(item, label)
        if self.count():
            self.setCurrentRow(0)

    def set_hint_only(self, text: str) -> None:
        """A free-text `input.hint` that didn't parse into a selectable list
        (`_parse_enum_hint` returned nothing) — one informational, muted,
        UNSELECTABLE row. There is nothing here to navigate or accept:
        `Composer` keeps `popup_active` False for this mode, so arrow keys
        and Enter behave normally in the text field underneath."""
        self._render_kind = "hint"
        self._hint_text = text
        self.clear()
        muted_color = theme.to_hex(theme.palette().color(QtGui.QPalette.Disabled, QtGui.QPalette.Text))
        item = QtWidgets.QListWidgetItem()
        item.setFlags(item.flags() & ~QtCore.Qt.ItemIsSelectable)
        item.setSizeHint(QtCore.QSize(0, 34))
        self.addItem(item)
        label = QtWidgets.QLabel(text, self)
        label.setWordWrap(True)
        label.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        label.setStyleSheet(f"color: {muted_color}; background: transparent; padding: 4px 10px;")
        self.setItemWidget(item, label)

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
            # `uri` is ImageContentBlock's own optional field ("where this
            # image came from") — not something invented on top of the
            # protocol. The pixels still travel in `data`; this is what
            # lets the chip and the sent message say `render.exr` instead
            # of a flat "Image", and it is the only trace of the file's
            # name that survives into the saved conversation.
            "uri": path.resolve().as_uri(),
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
    """A chip-sized preview for an image block, None for anything else."""
    return attachments.pixmap(block, _ATTACHMENT_THUMBNAIL)


def _attachment_label(block: dict) -> str:
    return attachments.label(block)


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


#: Options that stay on the bar under the input. Matched by id fragment
#: rather than an exact list, because the same setting is named differently
#: by different agents ("effort" in Claude, "reasoning effort" in Codex) and
#: hardcoding both spellings would break on the third agent.
_BAR_OPTION_HINTS = ("model", "effort")


def _named_choices(choices: list, current_value: str = "") -> tuple[list, str]:
    """Drop a choice that is an alias for one already in the list, and say
    which value now stands for `current_value`.

    Claude offers "Default (recommended)" alongside "Opus (1M context)", both
    carrying the identical description — "Opus 5 with 1M context · Best for
    everyday, complex tasks" — because they are the same model. Shown as
    written, the picker lists one model twice under the same subtitle and asks
    the artist to choose between a thing and itself. Claude Code's own picker
    doesn't do this: four models, no defaults.

    A choice is dropped when another shares its description word for word.
    That test is the agent's own statement that the two are the same thing;
    judging by name would be us guessing what "default" means.

    The LAST of the matching set survives, because the agent lists the alias
    before the model it points at — observed in the real data, where
    `default` precedes `opus[1m]`. Keeping the first would leave exactly the
    entry that names nothing.

    Returns the surviving value for `current_value` too: if the artist is
    currently on the alias we just removed, the chip has to select the
    survivor, or it would show an empty label and the next click would look
    like a change when it isn't.
    """
    by_description: dict[str, object] = {}
    order: list = []
    for choice in choices:
        description = str(getattr(choice, "description", "") or "").strip()
        if not description:
            order.append(choice)
            continue
        if description in by_description:
            order[order.index(by_description[description])] = choice
        else:
            order.append(choice)
        by_description[description] = choice

    resolved = current_value
    if current_value and not any(
        str(getattr(c, "value", "")) == current_value for c in order
    ):
        dropped_description = next(
            (
                str(getattr(c, "description", "") or "").strip()
                for c in choices
                if str(getattr(c, "value", "")) == current_value
            ),
            "",
        )
        survivor = by_description.get(dropped_description)
        if survivor is not None:
            resolved = str(getattr(survivor, "value", ""))
    return order, resolved


def _is_bar_option(option) -> bool:
    identifier = (getattr(option, "id", "") or "").lower()
    name = (getattr(option, "name", "") or "").lower()
    return any(hint in identifier or hint in name for hint in _BAR_OPTION_HINTS)


def _command_input_hint(command: Any) -> str:
    """The `input.hint` an agent attached to a slash command, or "" if it
    declared none.

    Duck-typed against ACP's own shape (`docs/facts/acp-sdk.md` §8):
    `AvailableCommand.input` is `None` or an `AvailableCommandInput`
    (`RootModel[UnstructuredCommandInput]`), so the hint lives at
    `command.input.root.hint`, not `command.input.hint` directly. Falls back
    to reading `.hint` straight off `command.input` when there's no `.root`
    at all, so a simpler test double (or a future, non-pydantic client)
    doesn't have to fake the SDK's `RootModel` wrapping just to be readable.
    """
    spec = getattr(command, "input", None)
    if spec is None:
        return ""
    root = getattr(spec, "root", spec)
    return str(getattr(root, "hint", "") or "")


def _is_marketplace_command(command: Any) -> bool:
    """Whether this command came from the account's own personal skill/
    plugin marketplace rather than the agent itself (`docs/facts/acp-sdk.md`
    §8: `available_commands` mixes the two, and this is account-scoped, not
    project-scoped — a real artist's own marketplace would show up here on
    a real machine, not just this project's test one).

    The ONLY structural marker any agent actually gives for this is Codex's
    own `$` prefix on such a `name` (e.g. `"$may-hub:sync"`). Claude and
    Grok mix marketplace and built-in commands with no distinguishing
    feature at all — inventing a name-based guess for THOSE (matching
    known skill names, say) is exactly the kind of heuristic this project
    decided against, so they get no tag; only what's structurally provable
    does.
    """
    return (getattr(command, "name", "") or "").startswith("$")


#: Recognizes exactly `<a|b|c>` / `[a|b]` — brackets around pipe-separated
#: alternatives. Deliberately not a general parser: see `_parse_enum_hint`.
_ENUM_HINT_RE = re.compile(r"^([<\[])(.+)([>\]])$")


def _parse_enum_hint(hint: str) -> list[str] | None:
    """`hint` as a list of choices, or `None` if it isn't one.

    Conservative on purpose: the WHOLE hint (after stripping whitespace)
    must be one bracket pair around two or more `|`-separated alternatives,
    none of which contain a space. Anything else — a lone placeholder like
    `<model>` (no `|`, nothing to choose between), nested brackets or an
    embedded space like `mcp`'s `[reconnect|enable|disable [<server>|all]]`,
    a `key=value` template, or a free-text sentence like `<optional custom
    summarization instructions>` — returns `None` and the hint is shown as
    plain text instead. Guessing at a grammar the agent never committed to
    is worse than not helping at all.
    """
    match = _ENUM_HINT_RE.match(hint.strip())
    if match is None:
        return None
    opening, inner, closing = match.groups()
    if (opening, closing) not in (("<", ">"), ("[", "]")):
        return None
    if "|" not in inner:
        return None
    parts = inner.split("|")
    if any(not part or " " in part for part in parts):
        return None
    return parts


class _BootScrim(QtWidgets.QWidget):
    """The frosted pane laid over the input while an agent starts.

    The input is not merely inert during a boot — there is no agent to send
    anything to — and an inert control that looks live invites the artist to
    type a paragraph into nothing. So it is covered rather than greyed: the
    words underneath stay legible enough to be recognised, while nothing
    about the pane invites a click.

    The overlay is an opaque copy of the composer's own themed surface and
    swallows the mouse. An earlier blur-plus-transparent-tint treatment
    rendered almost black under H20.5/H21's native compositor.
    """

    def __init__(self, parent: QtWidgets.QWidget) -> None:
        super().__init__(parent)
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, False)
        self.setCursor(QtCore.Qt.ArrowCursor)
        self.hide()

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        painter.setPen(QtGui.QPen(theme.composer_border(), 1))
        painter.setBrush(theme.composer_background())
        # Same radius as the surface underneath, or the corners show a bright
        # crescent of un-covered input.
        area = QtCore.QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.drawRoundedRect(area, 18, 18)
        painter.end()

    def mousePressEvent(self, event) -> None:  # noqa: N802 - Qt
        event.accept()  # absorbed, deliberately: there is nothing to click yet


class Composer(QtWidgets.QWidget):
    """Bottom of the panel: growing field, "+", microphone, chips, counter, send/stop."""

    submitted = Signal(list)  # list[dict] — ready ACP content blocks
    cancelled = Signal()
    mode_selected = Signal(str)
    config_option_selected = Signal(str, str)  # config_id, value
    attachment_rejected = Signal(str)
    buddy_selected = Signal(str)
    #: The footer's own "Report a bug" link. Placement was the owner's own
    #: call, by screenshot: the thin strip below the input box, not the
    #: header, not a floating corner control — see `_position_bug_report_
    #: link`'s own docstring for how it avoids moving the input box or the
    #: conversation drawer moving it sideways.
    bug_report_link_clicked = Signal()

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
        # Parented at construction (not adopted afterward by the layout that
        # eventually holds it below) — a parentless QWidget is a top-level
        # window in Qt, and macOS realises a native one for it immediately,
        # same defect as `transcript.py`'s per-tool-call row.
        self._attachments_bar = QtWidgets.QWidget(self)
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
        input_palette.setColor(
            placeholder_role, theme.palette().color(QtGui.QPalette.Disabled, QtGui.QPalette.Text)
        )
        self._text_edit.setPalette(input_palette)
        self._text_edit.textChanged.connect(self._on_text_changed)
        self._text_edit.submit_requested.connect(self._submit)
        self._text_edit.navigate_requested.connect(self._on_popup_navigate)
        self._text_edit.escape_requested.connect(self._hide_popup)
        self._text_edit.accept_requested.connect(self._on_popup_accept)

        self._popup = _CommandPopup(self)
        #: Which command's argument the popup is currently hinting at, while
        #: `_slash_argument_command` has matched one — see `_on_popup_accept`.
        self._popup_command_name: str | None = None

        # --- left-hand buttons: attachments, voice
        self._attach_button = QtWidgets.QToolButton(self)
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
        self._config_bar = QtWidgets.QWidget(self)
        self._config_layout = QtWidgets.QHBoxLayout(self._config_bar)
        self._config_layout.setContentsMargins(0, 0, 0, 0)
        self._config_layout.setSpacing(3)
        self._config_bar.setVisible(False)

        # --- right-hand side: counter, send/stop
        self._usage_label = QtWidgets.QLabel(self)
        self._usage_label.setVisible(False)

        self._send_button = QtWidgets.QPushButton("↑", self)
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
        # Bottom margin grew from 14 to make permanent room for the bug-
        # report link below the input box — a FIXED part of this layout
        # from construction on, not something that appears/disappears and
        # shifts the box around later. `self._surface`'s own position is
        # governed by the TOP margin and its own height, both unaffected by
        # this, so the input box's position does not move because of it.
        main_layout.setContentsMargins(0, 42, 0, 30)
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

        # The boot strip belongs to the composer, not to the panel: it has to
        # line up with the input box, and only the composer knows where that
        # is (the box is centred and width-clamped in `resizeEvent`). Placed
        # as a free-floating child in the band above the box rather than in
        # the layout, so an agent starting never moves the input.
        self._boot_status = BootStatus(self)
        self._boot_scrim = _BootScrim(self)
        self._entrance = BuddyEntrance(self)
        self._entrance.finished.connect(self._on_entrance_finished)

        # "somewhere in the right corner, small text, a little button"
        # (owner, his original placement request) — a small, quiet text
        # control, not a toolbar button competing with Send/voice/mode for
        # attention; findable when wanted, invisible otherwise. Same
        # free-floating-child, positioned-in-`resizeEvent`
        # technique as `_buddy`/`_boot_status` just above, for the same
        # reason stated on both of those: living outside `main_layout`
        # means its own size is never what determines where the input box
        # sits, so it cannot be the thing that nudges the box around.
        self._bug_report_link = QtWidgets.QPushButton("Report a bug…", self)
        self._bug_report_link.setObjectName("bugReportLink")
        self._bug_report_link.setCursor(QtCore.Qt.PointingHandCursor)
        self._bug_report_link.setFlat(True)
        self._bug_report_link.clicked.connect(self.bug_report_link_clicked.emit)
        self._bug_report_link.adjustSize()

        surface_background = theme.to_hex(theme.composer_background())
        surface_border = theme.to_hex(theme.composer_border())
        self.setStyleSheet(
            "QFrame#composerSurface {"
            f" background: {surface_background};"
            f" border: 1px solid {surface_border};"
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
            # A footer link, not a toolbar button: no border, no fill, no
            # radius pill — just muted text that reads as "text" until
            # hovered, matching the owner's own description of it.
            "QPushButton#bugReportLink {"
            " border: none; background: transparent; padding: 0;"
            " color: palette(disabled, text); font-size: 11px;"
            " text-align: left;"
            "}"
            "QPushButton#bugReportLink:hover {"
            " color: palette(text); text-decoration: underline;"
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
        self._layout_boot_widgets(surface_x, surface_y)
        self._position_bug_report_link(surface_x, surface_y)

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

    def shutdown(self) -> None:
        """Forwarded to `VoiceButton` — see its own `shutdown`. Called from
        `AgentPanel.shutdown()`."""
        self._voice_button.shutdown()

    def set_modes(self, modes: list["SessionMode"], current_id: str | None) -> None:
        """Facade over `mode_chip.set_modes` — the panel feeds session modes
        here instead of reaching into the nested widget (architecture.md §10:
        widgets talk through public API, never through someone else's
        private or nested attributes)."""
        self.mode_chip.set_modes(modes, current_id)

    def set_config_options(self, options: list) -> None:
        """Draw one chip per agent-side setting that earns the composer bar.

        This is where the model picker lives. ACP has no separate "model"
        method: an agent publishes model, reasoning effort and fast mode as
        session config options, each with its own choices and current value.
        The chips shown here, their labels and order, are the agent's word —
        an agent with no options simply gets no chips.

        Each element is duck-typed: `id`, `name`, `current_value` and
        `choices` of `value`/`name`/`description`. That keeps this widget
        from importing `client.py`, exactly like the rest of `ui/`.

        Not every option earns a chip, and this is a DELIBERATE, standing
        product choice, not a gap: Codex alone sends approval, collaboration
        mode, model, reasoning effort and fast mode, and a row of five
        dropdowns under the input turns the thing an artist looks at most
        into a control panel. `_is_bar_option` keeps only what an artist
        actually reaches for mid-conversation (model, effort) on the bar.
        Everything else an agent offers as a config option — permission
        mode, fast mode, whatever else a future agent adds — is
        INTENTIONALLY not drawn anywhere by this widget; the artist reaches
        those through the agent's own slash commands instead, same as any
        other ACP client. This was previously mis-described as "nothing is
        dropped, everything stays reachable" with a computed-but-unused
        `_overflow_options` list backing that claim — neither was true, and
        both are gone.
        """
        # Chips are REUSED, never torn down and rebuilt. Measured inside a
        # live Houdini: realising a top-level window costs one native window
        # and destroying the widget gives back exactly none of them —
        # twenty widgets realised, twenty windows, still twenty after the
        # widgets were deleted. Every ChoiceButton owns a popup, and a popup
        # is a top-level window, so rebuilding this bar on every
        # `config_option_update` (a model change, an effort change) leaked
        # one window per chip, permanently, for as long as Houdini ran.
        wanted = [
            o for o in options
            if _is_bar_option(o) and len(getattr(o, "choices", ()) or ()) >= 2
        ]
        while len(self._config_chips) > len(wanted):
            # Only when the agent genuinely offers FEWER options than before,
            # which is rare — not on every update.
            discard(self._config_chips.pop(), self._config_layout)
        while len(self._config_chips) < len(wanted):
            chip = ChoiceButton(self._config_bar)
            self._config_layout.addWidget(chip)
            self._config_chips.append(chip)

        for chip, option in zip(self._config_chips, wanted):
            current_value = str(getattr(option, "current_value", "") or "")
            choices, current_value = _named_choices(
                list(getattr(option, "choices", ()) or []), current_value
            )
            option_description = getattr(option, "description", "") or getattr(option, "name", "") or ""
            chip.clear()
            chip.blockSignals(True)
            try:
                for choice in choices:
                    # A choice's OWN description — the agent's word on what
                    # this specific value actually is ("Opus 5 with 1M
                    # context · Best for everyday, complex tasks" for a
                    # model option's "Default (recommended)" choice, which
                    # otherwise names nothing) — beats the option's generic
                    # one. Falls back to it when a choice has none of its
                    # own (Claude's effort choices, for instance), rather
                    # than showing nothing at all.
                    tooltip = str(getattr(choice, "description", "") or "") or option_description
                    chip.addItem(
                        str(getattr(choice, "name", "") or getattr(choice, "value", "")),
                        str(getattr(choice, "value", "")),
                        tooltip,
                    )
                index = chip.findData(current_value)
                if index >= 0:
                    chip.setCurrentIndex(index)
            finally:
                chip.blockSignals(False)
            option_id = str(getattr(option, "id", "") or "")
            # Reconnected each time: a reused chip may now stand for a
            # different option than it did last round.
            try:
                chip.activated.disconnect()
            except (RuntimeError, TypeError):
                pass
            chip.activated.connect(
                lambda index, c=chip, oid=option_id: self._on_config_activated(c, oid, index)
            )

        self._config_bar.setVisible(bool(self._config_chips))

    def _layout_boot_widgets(self, surface_x: int, surface_y: int) -> None:
        width = self._surface.width()
        height = self._boot_status.sizeHint().height()
        self._boot_status.setGeometry(
            surface_x, max(0, surface_y - height - 4), width, height
        )
        self._boot_status.raise_()
        self._boot_scrim.setGeometry(
            surface_x, surface_y, width, self._surface.height()
        )
        self._boot_scrim.raise_()
        # Wrapped around the buddy's own rect, with the hole at its feet:
        # the creature comes out of the floor it already lives on, and the
        # animation ends exactly where the sprite sits.
        self._entrance.setGeometry(self._entrance.geometry_for(self._buddy.geometry()))
        self._entrance.raise_()

    def _position_bug_report_link(self, surface_x: int, surface_y: int) -> None:
        """The footer strip, below the input box.

        Free-floating (never added to `main_layout`), same as `_buddy`/the
        boot widgets above — its own size can never be the thing that
        determines where the input box sits, which is what "must not add
        height that pushes the input box up" (owner's own placement brief)
        actually requires: not that no pixel anywhere ever changed, but
        that the link's presence can't be the cause. `surface_x`/`surface_
        y` are the SAME numbers the input box and the buddy sprite are
        already positioned from, so this can never drift out of alignment
        with either of them, whether or not the conversation drawer is
        open — the drawer draws inside `TranscriptView`'s own gutter
        (`AgentPanel._body_layout`'s own note) and never touches the
        composer's geometry at all.

        Below `_LINK_MIN_SURFACE_WIDTH`, hidden rather than clipped or
        wrapped — a deliberate choice, not a fallthrough: at the panel's
        narrowest docked widths the surface itself is already down to its
        180px floor, where a link squeezed in below it would either
        overlap the surface or read as illegible clipped text. Settings
        keeps its own "Report a bug…" entry reachable at any width
        (`SettingsView`'s Data section), so hiding this one narrow does
        not remove the feature, only this particular shortcut to it.
        """
        width = self._surface.width()
        if width < _LINK_MIN_SURFACE_WIDTH:
            self._bug_report_link.hide()
            return
        self._bug_report_link.show()
        self._bug_report_link.adjustSize()
        link = self._bug_report_link
        link.move(
            surface_x + width - link.width(),
            surface_y + self._surface.height() + 6,
        )
        link.raise_()

    def boot_status(self) -> BootStatus:
        """The strip, for whoever drives the phases (the panel)."""
        return self._boot_status

    def set_booting(self, booting: bool, *, show_buddy: bool = True) -> None:
        """Cover the input, or uncover it.

        The buddy goes away for the duration and comes back with the agent —
        it sits in the same band as the strip, and more to the point it is
        the panel's one sign of life, which should not be perched over a
        dead input.
        """
        if booting:
            # Keep the card on the same subtle theme surface during boot.
            # Blurring this native child under H20.5/H21 darkened it almost
            # to black; the opaque scrim already communicates the disabled
            # state and prevents interaction without changing its tone.
            self._surface.setGraphicsEffect(None)
            self._boot_scrim.show()
            self._boot_scrim.raise_()
            self._buddy.hide()
        else:
            # `setGraphicsEffect(None)` deletes the previous effect — the
            # widget owns it, so there is nothing to free here.
            self._surface.setGraphicsEffect(None)
            self._boot_scrim.hide()
            if show_buddy:
                self._buddy.show()
                self._buddy.raise_()
        self._text_edit.setReadOnly(booting)

    # --- the boot strip, driven by the panel -----------------------------

    def begin_boot(self, agent_name: str) -> None:
        self._boot_status.begin(agent_name)
        self.set_booting(True)

    def set_boot_phase(self, phase: str, detail: str = "") -> None:
        self._boot_status.set_phase(phase, detail)

    def finish_boot(self) -> None:
        """Uncover the input only if it was this boot that covered it.

        The buddy does not simply reappear: it climbs back out of a hole
        (`BuddyEntrance`), and only becomes the real, ticking sprite when
        that has played. The input is live throughout — the animation is
        the ending of the boot, not a further wait.
        """
        was_booting = self._boot_status.is_booting()
        self._boot_status.finish()
        if not was_booting:
            return
        self.set_booting(False, show_buddy=False)
        # Geometry first: the entrance has to know where the sprite will
        # land before it can land on it, and during a boot the buddy has
        # been hidden, so nothing has re-laid it out.
        self._entrance.setGeometry(self._entrance.geometry_for(self._buddy.geometry()))
        self._entrance.play(self._buddy, self._buddy.geometry())

    def _on_entrance_finished(self) -> None:
        self._buddy.show()
        self._buddy.raise_()

    def cancel_boot(self) -> None:
        self._entrance.skip()
        self._boot_status.cancel()
        self.set_booting(False)

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

    def set_text(self, text: str) -> None:
        """Put something in the input box for the artist to send, or edit.

        Deliberately does NOT send it: offering a command is help, sending it
        for them is deciding. Refuses to overwrite anything already typed —
        losing a half-written prompt to a helpful suggestion would be worse
        than the suggestion is useful.
        """
        if self._text_edit.toPlainText().strip():
            return
        self._text_edit.setPlainText(text)
        # `QTextCursor.End`, not `cursor.End`: PySide6 moved these onto the
        # enum, and reaching through the instance raises there while working
        # on PySide2 — the panel has to run on both.
        cursor = self._text_edit.textCursor()
        cursor.movePosition(QtGui.QTextCursor.End)
        self._text_edit.setTextCursor(cursor)
        self._text_edit.setFocus()

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

    def showEvent(self, event: QtGui.QShowEvent) -> None:  # noqa: N802 - Qt override
        """Refresh the placeholder colour from the live theme.

        Same reasoning as `ChoiceButton._apply_theme`: re-read rather than
        cache, so a pane hidden then shown again under a different Houdini
        colour scheme doesn't keep a placeholder tone from the scheme that
        was active when the composer was first built.
        """
        super().showEvent(event)
        input_palette = self._text_edit.palette()
        placeholder_role = getattr(QtGui.QPalette, "PlaceholderText", QtGui.QPalette.Text)
        input_palette.setColor(
            placeholder_role, theme.palette().color(QtGui.QPalette.Disabled, QtGui.QPalette.Text)
        )
        self._text_edit.setPalette(input_palette)

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
        clear_layout(self._attachments_layout)
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
        # Parented at construction (to `self._attachments_bar`, the layout
        # this chip goes into) — this runs once per attachment, every time
        # the attachment list changes, not once at startup.
        chip = QtWidgets.QFrame(self._attachments_bar)
        chip.setObjectName("attachmentChip")
        chip.setFixedHeight(_ATTACHMENT_CHIP_HEIGHT)
        layout = QtWidgets.QHBoxLayout(chip)
        layout.setContentsMargins(4, 2, 2, 2)
        layout.setSpacing(6)

        thumbnail = _attachment_thumbnail(block)
        if thumbnail is not None:
            preview = QtWidgets.QLabel(chip)
            preview.setPixmap(thumbnail)
            preview.setFixedSize(thumbnail.size())
            layout.addWidget(preview)

        label = QtWidgets.QLabel(_attachment_label(block), chip)
        label.setToolTip(_attachment_label(block))
        layout.addWidget(label)

        remove = QtWidgets.QToolButton(chip)
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
        """The command-NAME fragment being typed — while there's no space
        after it yet, so this is still the name-autocomplete list's
        territory. `None` once a space appears (`_slash_argument_command`
        takes over from there) or this isn't a slash line at all."""
        text = self._text_edit.toPlainText()
        if not text.startswith("/") or "\n" in text:
            return None
        rest = text[1:]
        if " " in rest:
            return None
        return rest

    def _slash_argument_command(self) -> "AvailableCommand | None":
        """The command whose ARGUMENT is being typed — once a space follows
        a name that matches a real command exactly (case-insensitively).
        `None` if the name before the space isn't one of `self._all_
        commands` (nothing to hint — an unknown command is just text), or
        this isn't a slash line with a space in it yet."""
        text = self._text_edit.toPlainText()
        if not text.startswith("/") or "\n" in text:
            return None
        rest = text[1:]
        if " " not in rest:
            return None
        name = rest.split(" ", 1)[0]
        return next((c for c in self._all_commands if c.name.lower() == name.lower()), None)

    def _matching_commands(self, query: str) -> list:
        """Commands whose name matches `query` — prefix matches first (a
        command starting with what was typed is almost always the one
        wanted), then anywhere-in-the-name matches after them.

        Plain prefix-only matching quietly fails for a personal marketplace
        command with a name like "$may-hub:sync" (`docs/facts/acp-sdk.md`
        §8): nobody types a literal "$" first, so a prefix-only filter made
        that command permanently unfindable except by scrolling — a real
        problem once an agent's list runs past a hundred entries. This
        doesn't rank commands by GUESSING which are "important" by name;
        it's the same widening any search box gets, applied to the one
        field the artist is actually typing against.
        """
        needle = query.lower()
        if not needle:
            return list(self._all_commands)
        prefix = [c for c in self._all_commands if c.name.lower().startswith(needle)]
        contains = [
            c
            for c in self._all_commands
            if needle in c.name.lower() and not c.name.lower().startswith(needle)
        ]
        return prefix + contains

    def _update_slash_popup(self) -> None:
        query = self._slash_query()
        if query is not None:
            matches = self._matching_commands(query)
            if matches:
                self._popup.set_commands(matches)
                self._popup_command_name = None
                self._show_popup(interactive=True)
                return
            self._hide_popup()
            return

        command = self._slash_argument_command()
        if command is not None:
            hint = _command_input_hint(command)
            if hint:
                choices = _parse_enum_hint(hint)
                self._popup_command_name = command.name
                if choices is not None:
                    self._popup.set_choice_rows(choices)
                    self._show_popup(interactive=True)
                else:
                    self._popup.set_hint_only(hint)
                    # Nothing to navigate or pick — a free-text hint is
                    # read-only guidance, not a control. Keyboard focus
                    # stays exactly where typing already has it.
                    self._show_popup(interactive=False)
                return

        self._hide_popup()

    def _show_popup(self, *, interactive: bool) -> None:
        self._position_popup()
        self._popup.show()
        self._popup.raise_()
        self._text_edit.popup_active = interactive

    def _hide_popup(self) -> None:
        self._popup.hide()
        self._text_edit.popup_active = False
        self._popup_command_name = None

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
        selected = self._popup.current_name()
        if selected is not None:
            # `_popup_command_name` set means this was an argument-choice
            # popup — the selected row is a VALUE for that command, not a
            # command name of its own.
            text = (
                f"/{self._popup_command_name} {selected}"
                if self._popup_command_name is not None
                else f"/{selected} "
            )
            self._text_edit.setPlainText(text)
            cursor = self._text_edit.textCursor()
            cursor.movePosition(QtGui.QTextCursor.End)
            self._text_edit.setTextCursor(cursor)
        self._hide_popup()


__all__ = ["Composer", "build_attachment_block"]

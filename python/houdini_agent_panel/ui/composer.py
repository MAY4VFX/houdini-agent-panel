"""Composer — поле ввода панели: рост под текст, вложения, слеш-команды, голос.

Правило проекта живёт здесь буквально построчно: каждый контрол на панели
показывается только если `AgentInfo` (см. `docs/architecture.md` §6) реально
объявил соответствующую capability. Ничего не решаем и не изобретаем поверх
протокола — агент не прислал `supports_image`/`supports_embedded_context` —
кнопки «+» нет; нет `availableModes` — нет чипа режима; нет `audio` и не задан
whisper — нет микрофона.

`submitted` отдаёт `list[dict]` в формате контент-блоков ACP (см.
`docs/facts/acp-sdk.md` §4) — ключи ровно на проводе (`"mimeType"`, а не
`"mime_type"`), потому что `client.py` строит из них pydantic-модели через
`cls(**block)`, а поля этих моделей объявлены с алиасами camelCase.
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
_DEFAULT_PLACEHOLDER = "Что изменить в сцене?"
_RAIL_WIDTH = 736


class _GrowingTextEdit(QtWidgets.QPlainTextEdit):
    """Поле ввода: Enter отправляет, Shift+Enter — перенос строки.

    Пока открыт слеш-попап (`popup_active`), стрелки/Enter/Esc не редактируют
    текст, а листают и закрывают попап — сам виджет не знает о содержимом
    попапа, только сигнализирует композеру о намерении пользователя.
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

    def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:  # noqa: N802 - Qt-переопределение
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
                super().keyPressEvent(event)  # перенос строки
                return
            self.submit_requested.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class _CommandPopup(QtWidgets.QListWidget):
    """Список слеш-команд над полем ввода. Обычный дочерний виджет, не
    системный попап — так навигация целиком остаётся в руках `_GrowingTextEdit`
    (клавиатурный фокус не покидает поле ввода)."""

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
    """Компактное число для счётчика токенов: 950, 1.2K, 3M."""
    if n < 1000:
        return str(n)
    for threshold, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= threshold:
            text = f"{n / threshold:.1f}".rstrip("0").rstrip(".")
            return f"{text}{suffix}"
    return str(n)  # pragma: no cover - недостижимо при n >= 1000


def build_attachment_block(path: Path, info: "AgentInfo") -> dict | None:
    """Файл -> готовый ACP content-блок, под ту capability, что реально есть.

    Картинка при `supports_image` — блоком `image`. Иначе, при
    `supports_embedded_context` — встроенный ресурс (`resource`): текстовый
    файл — как текст, иначе — blob в base64. Ни одна из двух capability не
    подошла — `None`: приложить нечем, агент не поймёт этот блок.
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
    """Низ панели: growing-поле, «+», микрофон, чип режима, счётчик, отправка/стоп."""

    submitted = Signal(list)  # list[dict] — готовые контент-блоки ACP
    cancelled = Signal()
    mode_selected = Signal(str)
    model_selected = Signal(str)
    attachment_rejected = Signal(str)
    buddy_selected = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._info: "AgentInfo | None" = None
        self._busy = False
        self._blocked = False
        self._attachments: list[dict] = []
        self._all_commands: list["AvailableCommand"] = []

        self.setAcceptDrops(True)

        # --- вложения (строка чипов над полем, видна только если есть что показать)
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

        # --- поле ввода
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

        # --- левые кнопки: вложения, голос
        self._attach_button = QtWidgets.QToolButton()
        self._attach_button.setObjectName("composerTool")
        self._attach_button.setText("+")
        self._attach_button.setToolTip("Прикрепить файл")
        self._attach_button.setVisible(False)
        self._attach_button.clicked.connect(self._on_attach_clicked)

        self._voice_button = VoiceButton(self)
        self._voice_button.setObjectName("composerTool")
        self._voice_button.recorded_audio.connect(self._on_voice_audio)
        self._voice_button.transcribed_text.connect(self._on_voice_text)

        # --- чип режима: настоящий (chips.py) или временная заглушка
        self.mode_chip = ModeChip(self)
        self.mode_chip.mode_selected.connect(self.mode_selected.emit)

        # Модель — такой же data-driven control: по умолчанию скрыта и
        # появляется только если вызывающая сторона передала варианты.
        self.model_chip = ChoiceButton(self)
        self.model_chip.activated.connect(self._on_model_activated)
        self.model_chip.setVisible(False)

        # --- правая сторона: счётчик, отправка/стоп
        self._usage_label = QtWidgets.QLabel()
        self._usage_label.setVisible(False)

        self._send_button = QtWidgets.QPushButton("↑")
        self._send_button.setObjectName("composerSend")
        self._send_button.setFixedSize(32, 32)
        self._send_button.setToolTip("Отправить")
        self._send_button.clicked.connect(self._on_send_clicked)

        action_row = QtWidgets.QHBoxLayout()
        action_row.setContentsMargins(0, 0, 0, 0)
        action_row.setSpacing(3)
        action_row.addWidget(self.mode_chip)
        action_row.addWidget(self._attach_button)
        action_row.addStretch(1)
        action_row.addWidget(self.model_chip)
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

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self._surface.setFixedWidth(min(_RAIL_WIDTH, max(0, self.width() - 28)))
        # Layout геометрию применяет уже после resizeEvent, поэтому X считаем
        # из того же center rule напрямую, а не читаем устаревший mapTo().
        surface_x = (self.width() - self._surface.width()) // 2
        surface_y = self._surface.y()
        self._buddy.move(
            surface_x + self._surface.width() - self._buddy.width() - 20,
            surface_y - self._buddy.height() + 12,
        )
        self._buddy.raise_()

    # --- публичный контракт (docs/architecture.md §10) --------------------

    def set_capabilities(self, info: "AgentInfo | None", whisper: str) -> None:
        """Пересчитать видимость «+» и микрофона под свежий `AgentInfo`.

        `info=None` (агент отключился/ещё не подключён) — оба контрола прячутся,
        кроме случая, когда микрофону хватает одного whisper-эндпоинта: это
        решает сам `VoiceButton.configure`, а не эта функция.
        """
        self._info = info
        can_attach = info is not None and (info.supports_image or info.supports_embedded_context)
        self._attach_button.setVisible(can_attach)
        supports_audio = info is not None and info.supports_audio
        self._voice_button.configure(supports_audio=supports_audio, whisper_endpoint=whisper)

    def set_modes(self, modes: list["SessionMode"], current_id: str | None) -> None:
        """Фасад над `mode_chip.set_modes` — панель кормит режимы сессии сюда,
        не дотягиваясь до вложенного виджета напрямую (architecture.md §10:
        общение между виджетами только через публичный API, не через чужие
        приватные/вложенные атрибуты)."""
        self.mode_chip.set_modes(modes, current_id)

    def set_models(self, models: list[tuple[str, str]], current_id: str | None) -> None:
        """Показать выбор модели только для списка, пришедшего от агента."""
        self.model_chip.blockSignals(True)
        try:
            self.model_chip.clear()
            for model_id, label in models:
                self.model_chip.addItem(label, model_id)
            index = self.model_chip.findData(current_id)
            if index >= 0:
                self.model_chip.setCurrentIndex(index)
        finally:
            self.model_chip.blockSignals(False)
        self.model_chip.setVisible(bool(models))

    def set_buddy(self, key: str) -> None:
        self._buddy.set_buddy(key)

    def trigger_buddy(self) -> None:
        self._buddy.start_action()

    def popover_anchor_rect(self, target: QtWidgets.QWidget) -> QtCore.QRect:
        """Composer surface in coordinates of an external overlay host."""
        top_left = self._surface.mapTo(target, QtCore.QPoint(0, 0))
        return QtCore.QRect(top_left, self._surface.size())

    def enable_preview_microphone(self) -> None:
        """Показать affordance в standalone preview без выдуманной capability."""
        self._voice_button.setVisible(True)
        self._voice_button.setToolTip("Микрофон (в preview без аудиобэкенда)")

    def _on_model_activated(self, index: int) -> None:
        model_id = self.model_chip.itemData(index)
        if model_id:
            self.model_selected.emit(str(model_id))

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._send_button.setText("■" if busy else "↑")
        self._send_button.setToolTip("Остановить" if busy else "Отправить")

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
        """Блокирует ТОЛЬКО поле ввода и кнопку отправки/вложений/голоса —
        лента, прокрутка, закрытие панели и вся остальная Houdini не в курсе,
        что композер сейчас нельзя использовать (design.md)."""
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
        """Не из architecture.md §10, но нужна вызывающему коду (панель проверяет
        это в тестах и, вероятно, в логике самой панели) — простой геттер к
        состоянию, которое `block_input`/`unblock_input` уже держат."""
        return self._blocked

    # --- вложения: «+», drag&drop -----------------------------------------

    def add_attachment(self, path: Path) -> bool:
        """Добавить файл как вложение к следующей отправке.

        `False` — capability текущего агента не позволяет приложить именно
        этот файл (не картинка при отсутствии `embeddedContext`, либо агент
        ещё не подключён).
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
                # `setParent(None)` — сразу: иначе чип ещё числится ребёнком
                # композера до следующего цикла событий.
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
        for url in event.mimeData().urls():
            if url.isLocalFile() and self.add_attachment(Path(url.toLocalFile())):
                added_any = True
        if added_any:
            event.acceptProposedAction()
        else:
            event.ignore()

    # --- голос -------------------------------------------------------------

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

    # --- отправка ------------------------------------------------------------

    def _gather_blocks(self) -> list[dict]:
        blocks: list[dict] = []
        text = self._text_edit.toPlainText().strip()
        if text:
            blocks.append({"type": "text", "text": text})
        blocks.extend(self._attachments)
        return blocks

    def _submit(self) -> None:
        if self._blocked or self._busy:
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

    # --- рост поля ввода -----------------------------------------------------

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

    # --- слеш-команды -----------------------------------------------------

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

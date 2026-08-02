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
from typing import TYPE_CHECKING

from .chips import ModeChip
from .qt import QtCore, QtGui, QtWidgets, Signal
from .voice import VoiceButton

if TYPE_CHECKING:
    from ..client import AgentInfo
    from ..sessions import AvailableCommand, SessionMode, Usage

_MIN_LINES = 1
_MAX_LINES = 6
_MAX_POPUP_HEIGHT = 160


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
        self.setFocusPolicy(QtCore.Qt.NoFocus)
        self.hide()

    def set_commands(self, commands: list[Any]) -> None:
        self.clear()
        for cmd in commands:
            label = f"/{cmd.name}"
            if cmd.description:
                label += f" — {cmd.description}"
            item = QtWidgets.QListWidgetItem(label)
            item.setData(QtCore.Qt.UserRole, cmd.name)
            self.addItem(item)
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
        return {"type": "image", "data": base64.b64encode(data).decode("ascii"), "mimeType": mime_type}
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


def _attachment_label(block: dict) -> str:
    kind = block.get("type")
    if kind == "image":
        return "🖼 изображение"
    if kind == "audio":
        return "🎙 аудио"
    if kind == "resource":
        uri = (block.get("resource") or {}).get("uri", "")
        name = uri.rsplit("/", 1)[-1] if uri else "файл"
        return f"📎 {name}"
    return "📎 вложение"


class Composer(QtWidgets.QWidget):
    """Низ панели: growing-поле, «+», микрофон, чип режима, счётчик, отправка/стоп."""

    submitted = Signal(list)  # list[dict] — готовые контент-блоки ACP
    cancelled = Signal()
    mode_selected = Signal(str)

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
        self._attachments_layout = QtWidgets.QHBoxLayout(self._attachments_bar)
        self._attachments_layout.setContentsMargins(0, 0, 0, 0)
        self._attachments_bar.setVisible(False)

        # --- поле ввода
        self._text_edit = _GrowingTextEdit(self)
        self._text_edit.textChanged.connect(self._on_text_changed)
        self._text_edit.submit_requested.connect(self._submit)
        self._text_edit.navigate_requested.connect(self._on_popup_navigate)
        self._text_edit.escape_requested.connect(self._hide_popup)
        self._text_edit.accept_requested.connect(self._on_popup_accept)

        self._popup = _CommandPopup(self)

        # --- левые кнопки: вложения, голос
        self._attach_button = QtWidgets.QToolButton()
        self._attach_button.setText("+")
        self._attach_button.setToolTip("Прикрепить файл")
        self._attach_button.setVisible(False)
        self._attach_button.clicked.connect(self._on_attach_clicked)

        self._voice_button = VoiceButton(self)
        self._voice_button.recorded_audio.connect(self._on_voice_audio)
        self._voice_button.transcribed_text.connect(self._on_voice_text)

        # --- чип режима: настоящий (chips.py) или временная заглушка
        self.mode_chip = ModeChip(self)
        self.mode_chip.mode_selected.connect(self.mode_selected.emit)

        # --- правая сторона: счётчик, отправка/стоп
        self._usage_label = QtWidgets.QLabel()
        self._usage_label.setVisible(False)

        self._send_button = QtWidgets.QPushButton("➤")
        self._send_button.setToolTip("Отправить")
        self._send_button.clicked.connect(self._on_send_clicked)

        input_row = QtWidgets.QHBoxLayout()
        input_row.addWidget(self._attach_button)
        input_row.addWidget(self._voice_button)
        input_row.addWidget(self.mode_chip)
        input_row.addWidget(self._text_edit, 1)
        input_row.addWidget(self._usage_label)
        input_row.addWidget(self._send_button)

        main_layout = QtWidgets.QVBoxLayout(self)
        main_layout.setContentsMargins(4, 4, 4, 4)
        main_layout.addWidget(self._attachments_bar)
        main_layout.addLayout(input_row)

        self._adjust_text_height()

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

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._send_button.setText("■" if busy else "➤")
        self._send_button.setToolTip("Остановить" if busy else "Отправить")

    def set_commands(self, commands: list["AvailableCommand"]) -> None:
        self._all_commands = list(commands)
        if self._popup.isVisible():
            self._update_slash_popup()

    def set_usage(self, usage: "Usage | None") -> None:
        if usage is None:
            self._usage_label.setVisible(False)
            return
        self._usage_label.setText(_format_tokens(getattr(usage, "total_tokens", 0)))
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
        self._text_edit.setPlaceholderText("")
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
        block = build_attachment_block(Path(path), self._info)
        if block is None:
            return False
        self._attachments.append(block)
        self._refresh_attachments_bar()
        return True

    def _on_attach_clicked(self) -> None:
        if self._info is None:
            return
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Прикрепить файлы")
        for raw_path in paths:
            self.add_attachment(Path(raw_path))

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
            chip = QtWidgets.QWidget()
            chip_layout = QtWidgets.QHBoxLayout(chip)
            chip_layout.setContentsMargins(4, 0, 4, 0)
            chip_layout.addWidget(QtWidgets.QLabel(_attachment_label(block)))
            remove = QtWidgets.QToolButton()
            remove.setText("✕")
            remove.setAutoRaise(True)
            remove.clicked.connect(lambda checked=False, i=index: self._remove_attachment(i))
            chip_layout.addWidget(remove)
            self._attachments_layout.addWidget(chip)
        self._attachments_bar.setVisible(bool(self._attachments))

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
        """Растёт по числу строк текста, а не `document().size()`/`blockCount()`:
        оба без реального layout-прохода (которого нет без экрана — в headless-
        тестах в том числе) не отражают фактическое число абзацев надёжно."""
        line_height = QtGui.QFontMetrics(self._text_edit.font()).lineSpacing()
        lines = max(1, self._text_edit.toPlainText().count("\n") + 1)
        padding = 12
        min_height = line_height * _MIN_LINES + padding
        max_height = line_height * _MAX_LINES + padding
        new_height = max(min_height, min(line_height * lines + padding, max_height))
        self._text_edit.setFixedHeight(int(new_height))

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
        edit_geo = self._text_edit.geometry()
        row_height = self._popup.sizeHintForRow(0) if self._popup.count() else 20
        height = min(row_height * max(self._popup.count(), 1) + 4, _MAX_POPUP_HEIGHT)
        self._popup.setGeometry(edit_geo.x(), edit_geo.y() - height, edit_geo.width(), height)

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

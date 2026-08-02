"""Компактный popover запроса разрешения над composer.

Кнопки строятся строго из `view.options`, в порядке, присланном агентом:
своих кнопок не добавляем (в том числе кнопку «отмена» — если агенту нужна
такая опция, он пришлёт её сам), чужие не переименовываем. `kind` каждой
опции влияет только на акцент (не текст, не порядок): `reject_*` получают
цвет ошибки из `theme` (по форме, не по хардкод-цвету — см. `theme.status_color`),
`*_always` — жирное начертание. Решение хранит TranscriptModel, а сам
интерактивный popover исчезает сразу после ответа.
"""

from __future__ import annotations

from ..transcript_model import PermissionView
from . import theme
from .qt import QtGui, QtWidgets, Signal


class PermissionRow(QtWidgets.QWidget):
    answered = Signal(str, str)  # request_key, option_id

    def __init__(self, view: PermissionView, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("permissionPopover")
        self.setAttribute(theme.QtCore.Qt.WA_StyledBackground, True)
        self.setMinimumWidth(280)
        self.setMaximumWidth(400)
        self.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)
        self._view = view
        self._answered: str | None = view.answered
        self._buttons: dict[str, QtWidgets.QPushButton] = {}

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)

        self._title_label = QtWidgets.QLabel(view.tool_title, self)
        self._title_label.setWordWrap(True)
        self._title_label.setTextInteractionFlags(theme.QtCore.Qt.TextSelectableByMouse)
        layout.addWidget(self._title_label)

        buttons_row = QtWidgets.QHBoxLayout()
        buttons_row.setSpacing(theme.SPACING_TIGHT)
        buttons_row.addStretch(1)
        for option_id, name, kind in view.options:
            button = QtWidgets.QPushButton(name, self)  # текст — ровно то, что прислал агент
            button.setFlat(True)
            button.setMinimumHeight(26)
            font = button.font()
            if kind.endswith("_always"):
                font.setBold(True)
            button.setFont(font)
            if kind.startswith("reject_"):
                palette = button.palette()
                palette.setColor(QtGui.QPalette.ButtonText, theme.status_color("pending"))
                button.setPalette(palette)
            button.setProperty("permissionPrimary", kind == "allow_once")
            button.clicked.connect(lambda _checked=False, oid=option_id: self._on_clicked(oid))
            self._buttons[option_id] = button
            buttons_row.addWidget(button)
        layout.addLayout(buttons_row)

        # Видна только после ответа — история решения человека.
        self._status_label = QtWidgets.QLabel(self)
        self._status_label.setVisible(False)
        layout.addWidget(self._status_label)

        if self._answered is not None:
            self._apply_answered(self._answered)

        self.setStyleSheet(
            "QWidget#permissionPopover {"
            " background: palette(base);"
            " border: 1px solid palette(mid);"
            " border-radius: 12px;"
            "}"
            "QWidget#permissionPopover QPushButton {"
            " border: 1px solid palette(mid);"
            " border-radius: 6px;"
            " padding: 3px 8px;"
            " background: palette(alternate-base);"
            "}"
            "QWidget#permissionPopover QPushButton:hover {"
            " background: palette(button);"
            "}"
            "QWidget#permissionPopover QPushButton[permissionPrimary=\"true\"] {"
            " border-color: transparent;"
            " color: #24180c;"
            " background: #e5a047;"
            "}"
        )
        shadow = QtWidgets.QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 7)
        shadow.setColor(QtGui.QColor(0, 0, 0, 115))
        self.setGraphicsEffect(shadow)

    def request_key(self) -> str:
        return self._view.request_key

    def apply_view(self, view: PermissionView) -> None:
        """Обновить строку по свежему `PermissionView` (например `answered` пришёл извне).

        Используется `ui/transcript.py`, чтобы патчить существующую строку на месте,
        а не пересоздавать виджет — так порядок и состояние кнопок не дёргаются.
        """
        self._view = view
        if view.answered is not None and self._answered is None:
            self._apply_answered(view.answered)

    def _on_clicked(self, option_id: str) -> None:
        if self._answered is not None:
            return  # кнопки задизейблены после ответа, но защищаемся и здесь
        self._apply_answered(option_id)
        self.answered.emit(self._view.request_key, option_id)

    def _apply_answered(self, option_id: str) -> None:
        self._answered = option_id
        for button in self._buttons.values():
            button.setEnabled(False)
        chosen = self._buttons.get(option_id)
        if chosen is not None:
            font = chosen.font()
            font.setUnderline(True)
            chosen.setFont(font)
            self._status_label.setText(f"Выбрано: {chosen.text()}")
        else:
            # option_id не из своего же списка options — не должно случаться в
            # штатной работе, но не падаем: просто показываем сырое значение.
            self._status_label.setText(f"Выбрано: {option_id}")
        self._status_label.setVisible(True)


__all__ = ["PermissionRow"]

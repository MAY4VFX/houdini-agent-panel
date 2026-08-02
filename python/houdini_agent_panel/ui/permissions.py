"""`PermissionRow` — строка запроса разрешения в ленте (docs/architecture.md §10).

Кнопки строятся строго из `view.options`, в порядке, присланном агентом:
своих кнопок не добавляем (в том числе кнопку «отмена» — если агенту нужна
такая опция, он пришлёт её сам), чужие не переименовываем. `kind` каждой
опции влияет только на акцент (не текст, не порядок): `reject_*` получают
цвет ошибки из `theme` (по форме, не по хардкод-цвету — см. `theme.status_color`),
`*_always` — жирное начертание. Ответив, строка не исчезает и не превращается
в кнопки заново — она остаётся в ленте историей решения человека.
"""

from __future__ import annotations

from ..transcript_model import PermissionView
from . import theme
from .qt import QtGui, QtWidgets, Signal


class PermissionRow(QtWidgets.QWidget):
    answered = Signal(str, str)  # request_key, option_id

    def __init__(self, view: PermissionView, parent=None) -> None:
        super().__init__(parent)
        self._view = view
        self._answered: str | None = view.answered
        self._buttons: dict[str, QtWidgets.QPushButton] = {}

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(theme.MARGIN, theme.SPACING_TIGHT, theme.MARGIN, theme.SPACING_TIGHT)
        layout.setSpacing(theme.SPACING_TIGHT)

        self._title_label = QtWidgets.QLabel(view.tool_title, self)
        self._title_label.setWordWrap(True)
        self._title_label.setTextInteractionFlags(theme.QtCore.Qt.TextSelectableByMouse)
        layout.addWidget(self._title_label)

        buttons_row = QtWidgets.QHBoxLayout()
        buttons_row.setSpacing(theme.SPACING_TIGHT)
        for option_id, name, kind in view.options:
            button = QtWidgets.QPushButton(name, self)  # текст — ровно то, что прислал агент
            button.setFlat(True)
            font = button.font()
            if kind.endswith("_always"):
                font.setBold(True)
            button.setFont(font)
            if kind.startswith("reject_"):
                palette = button.palette()
                palette.setColor(QtGui.QPalette.ButtonText, theme.status_color("pending"))
                button.setPalette(palette)
            button.clicked.connect(lambda _checked=False, oid=option_id: self._on_clicked(oid))
            self._buttons[option_id] = button
            buttons_row.addWidget(button)
        buttons_row.addStretch(1)
        layout.addLayout(buttons_row)

        # Видна только после ответа — история решения человека.
        self._status_label = QtWidgets.QLabel(self)
        self._status_label.setVisible(False)
        layout.addWidget(self._status_label)

        if self._answered is not None:
            self._apply_answered(self._answered)

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

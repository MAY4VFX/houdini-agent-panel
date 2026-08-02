"""Экран входа — рисуется целиком из `authMethods`, присланных агентом.

Ни одного своего поля: список кнопок — ровно `AuthMethod` из
`AgentInfo.auth_methods` (см. docs/architecture.md §6). Кнопка выхода
показывается только если агент объявил `supports_logout` — своих полей
логина/пароля/чего угодно ещё панель не изобретает (design.md: «агент не
умеет — контрол не рисуется»).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .qt import QtWidgets, Signal

if TYPE_CHECKING:
    from ..client import AuthMethod


def _clear_layout(layout: "QtWidgets.QLayout") -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()


class AuthView(QtWidgets.QWidget):
    method_chosen = Signal(str)
    logout_requested = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        title = QtWidgets.QLabel("Войти")
        title.setStyleSheet("font-weight: bold; font-size: 14px;")

        self._methods_layout = QtWidgets.QVBoxLayout()
        self._empty_label = QtWidgets.QLabel("Агент не прислал способов входа.")
        self._empty_label.setVisible(False)

        self._logout_button = QtWidgets.QPushButton("Выйти")
        self._logout_button.setVisible(False)
        self._logout_button.clicked.connect(self.logout_requested.emit)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(self._empty_label)
        layout.addLayout(self._methods_layout)
        layout.addStretch(1)
        layout.addWidget(self._logout_button)

    def set_methods(self, methods: list["AuthMethod"], *, can_logout: bool) -> None:
        """Перерисовать список способов входа. Пустой список — не ошибка:
        показываем это человеку текстом, а не пустым экраном без объяснений."""
        _clear_layout(self._methods_layout)
        methods = list(methods)
        self._empty_label.setVisible(not methods)

        for method in methods:
            button = QtWidgets.QPushButton(method.name)
            if method.description:
                button.setToolTip(method.description)
            button.clicked.connect(lambda checked=False, mid=method.id: self.method_chosen.emit(mid))
            self._methods_layout.addWidget(button)

        self._logout_button.setVisible(can_logout)


__all__ = ["AuthView"]

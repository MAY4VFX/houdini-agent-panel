"""The sign-in screen — drawn entirely from the `authMethods` the agent sent.

Not one field of our own: the button list is exactly the `AuthMethod` entries
from `AgentInfo.auth_methods` (see docs/architecture.md §6). The sign-out
button appears only if the agent declared `supports_logout` — the panel
invents no login/password/anything-else fields of its own (design.md: "the
agent doesn't support it, the control doesn't get drawn").
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

        title = QtWidgets.QLabel("Sign in")
        title.setStyleSheet("font-weight: bold; font-size: 14px;")

        self._methods_layout = QtWidgets.QVBoxLayout()
        self._empty_label = QtWidgets.QLabel("The agent offered no sign-in methods.")
        self._empty_label.setVisible(False)

        self._logout_button = QtWidgets.QPushButton("Sign out")
        self._logout_button.setVisible(False)
        self._logout_button.clicked.connect(self.logout_requested.emit)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(self._empty_label)
        layout.addLayout(self._methods_layout)
        layout.addStretch(1)
        layout.addWidget(self._logout_button)

    def set_methods(self, methods: list["AuthMethod"], *, can_logout: bool) -> None:
        """Redraw the list of sign-in methods. An empty list isn't an error:
        we say so in words rather than show a blank screen with no explanation."""
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

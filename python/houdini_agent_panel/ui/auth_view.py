"""The sign-in screen — drawn entirely from the `authMethods` the agent sent.

Not one field of our own: the button list is exactly the `AuthMethod` entries
from `AgentInfo.auth_methods` (see docs/architecture.md §6). The sign-out
button appears only if the agent declared `supports_logout` — the panel
invents no login/password/anything-else fields of its own (design.md: "the
agent doesn't support it, the control doesn't get drawn").
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .qt import QtCore, QtWidgets, Signal

if TYPE_CHECKING:
    from ..client import AuthMethod


#: Same centred column width as the feed, the composer and settings.
_RAIL_WIDTH = 736


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

        #: Failures are shown HERE, not in the feed. A sign-in that fails
        #: while the artist is looking at the sign-in screen used to report
        #: itself into a transcript they could not see, so the screen simply
        #: sat there — which is exactly what a refused login looks like when
        #: nobody tells you it was refused.
        self._error_label = QtWidgets.QLabel()
        self._error_label.setWordWrap(True)
        self._error_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self._error_label.setVisible(False)
        self._buttons: dict[str, QtWidgets.QPushButton] = {}

        self.setStyleSheet(
            'QPushButton[signInFailed="true"] {'
            " color: palette(disabled, text);"
            " text-decoration: line-through;"
            "}"
        )

        # Centred column of a fixed width, like the feed and the composer.
        # Buttons stretched edge to edge across a docked panel looked like a
        # form, not a choice of four.
        rail = QtWidgets.QWidget(self)
        rail.setMaximumWidth(_RAIL_WIDTH)
        rail_layout = QtWidgets.QVBoxLayout(rail)
        rail_layout.setContentsMargins(0, 0, 0, 0)
        rail_layout.addWidget(title)
        rail_layout.addWidget(self._empty_label)
        rail_layout.addWidget(self._error_label)
        rail_layout.addLayout(self._methods_layout)
        rail_layout.addStretch(1)
        rail_layout.addWidget(self._logout_button)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.addWidget(rail, 0, QtCore.Qt.AlignHCenter)

    def set_methods(self, methods: list["AuthMethod"], *, can_logout: bool) -> None:
        """Redraw the list of sign-in methods. An empty list isn't an error:
        we say so in words rather than show a blank screen with no explanation."""
        _clear_layout(self._methods_layout)
        methods = list(methods)
        self._empty_label.setVisible(not methods)
        self.clear_error()

        self._buttons = {}
        for method in methods:
            button = QtWidgets.QPushButton(method.name)
            if method.description:
                button.setToolTip(method.description)
            button.clicked.connect(lambda checked=False, mid=method.id: self.method_chosen.emit(mid))
            self._methods_layout.addWidget(button)
            self._buttons[method.id] = button

        self._logout_button.setVisible(can_logout)

    def show_error(self, message: str, method_id: str = "") -> None:
        """Report a failed sign-in on the screen the artist is looking at.

        The method that failed is marked, but never removed. Which methods
        exist is the agent's word — Gemini CLI advertises `oauth-personal`
        and then refuses it for individual accounts — and hiding one on our
        own initiative would mean the day Google fixes it, the panel keeps
        the working door shut. Marking says "this one just failed" without
        pretending to know it will fail forever.
        """
        self._error_label.setText(message)
        self._error_label.setVisible(bool(message))
        for identifier, button in self._buttons.items():
            failed = bool(method_id) and identifier == method_id
            button.setProperty("signInFailed", failed)
            button.setToolTip(message if failed else button.toolTip())
            button.style().unpolish(button)
            button.style().polish(button)

    def clear_error(self) -> None:
        self._error_label.clear()
        self._error_label.setVisible(False)
        for button in self._buttons.values():
            button.setProperty("signInFailed", False)
            button.style().unpolish(button)
            button.style().polish(button)


__all__ = ["AuthView"]

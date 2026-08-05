"""The sign-in screen — drawn entirely from the `authMethods` the agent sent.

Not one field of our own: the button list is exactly the `AuthMethod` entries
from `AgentInfo.auth_methods` (see docs/architecture.md §6). The sign-out
button appears only if the agent declared `supports_logout` — the panel
invents no login/password/anything-else fields of its own (design.md: "the
agent doesn't support it, the control doesn't get drawn").
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import theme
from .qt import QtCore, QtWidgets, Signal

if TYPE_CHECKING:
    from ..client import AuthMethod


#: Same centred column width as the feed, the composer and settings.
_RAIL_WIDTH = 736
#: Floor for the centred rail — see `Composer._MIN_RAIL_WIDTH`.
_MIN_RAIL_WIDTH = 180


def _clear_layout(layout: "QtWidgets.QLayout") -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.hide()  # before orphaning: a parentless widget is a window
            widget.setParent(None)
            widget.deleteLater()


class AuthView(QtWidgets.QWidget):
    method_chosen = Signal(str)
    logout_requested = Signal()
    #: The artist gave up waiting on a pending `authenticate()` call and
    #: wants the method list back — see `set_pending`'s docstring for why
    #: this can't actually cancel anything on the protocol side.
    cancel_pending = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        title = QtWidgets.QLabel("Sign in")
        title.setStyleSheet("font-weight: bold; font-size: 14px;")

        self._methods_layout = QtWidgets.QVBoxLayout()
        #: Shown instead of an empty method list. Plain "no sign-in
        #: methods" used to be the only thing this ever said — technically
        #: true, useless in practice for an agent like Claude Agent that
        #: DOES have a real way in, just not one the panel can drive
        #: (docs/facts/acp-sdk.md §9/§11). `set_methods`'s `no_methods_help`
        #: replaces it with that agent's own instructions when the caller
        #: has them (`AgentPanel._no_methods_advice`); word-wrapped and
        #: selectable so a command in it can be copied, same as the error
        #: label below.
        self._empty_label = QtWidgets.QLabel("The agent offered no sign-in methods.")
        self._empty_label.setWordWrap(True)
        self._empty_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
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

        # Shown while `authenticate()` is in flight for a method that opens
        # a browser or waits on another CLI — these don't return until the
        # human finishes elsewhere (docs/facts/acp-sdk.md §12: Codex
        # `chat-gpt` and Kimi `login` both stay open indefinitely, and
        # returning without raising IS the success signal). Before this
        # existed, the screen went quiet the instant a method was picked —
        # a Codex login that was genuinely working and a Kimi one stuck for
        # some other reason looked identical: both silence. The method
        # buttons are disabled rather than hidden — the list is still the
        # true answer to "what are my choices", it's just not the moment to
        # press one again — and Cancel gives the artist the list back
        # without pretending the underlying call was actually stopped.
        # Flat siblings of `_error_label` in `rail_layout` below, not one
        # extra wrapper widget with its own nested layout: a `QWidget` that
        # starts out hidden and only grows real (wrapped, multi-line)
        # content once shown doesn't reliably tell its OWN parent layout to
        # re-measure it under every Qt backend — the same word-wrapped
        # `QLabel` sitting directly in `rail_layout`, exactly like `_error_
        # label` two lines below, has no such extra hop and measures
        # correctly the same way `_error_label` already does.
        self._pending_label = QtWidgets.QLabel()
        self._pending_label.setWordWrap(True)
        self._pending_label.setVisible(False)
        #: The agent's own raw stderr while pending, one line at a time —
        #: e.g. gemini's `oauth-personal`, which never emits anything else
        #: (docs/facts/acp-sdk.md §13: "Failed to authenticate with
        #: authorization code:invalid_grant" / "...Retrying..." on stderr,
        #: nothing on the ACP channel itself). Replaced on each new line
        #: rather than accumulated — this is "what's happening right now",
        #: not a log the artist has to scroll.
        self._pending_detail_label = QtWidgets.QLabel()
        self._pending_detail_label.setWordWrap(True)
        self._pending_detail_label.setVisible(False)
        self._pending_detail_label.setStyleSheet("color: palette(disabled, text);")
        self._cancel_pending_button = QtWidgets.QPushButton("Cancel")
        self._cancel_pending_button.setVisible(False)
        self._cancel_pending_button.clicked.connect(self._on_cancel_pending)
        self._cancel_pending_row = QtWidgets.QHBoxLayout()
        self._cancel_pending_row.setContentsMargins(0, 0, 0, 0)
        self._cancel_pending_row.addStretch(1)
        self._cancel_pending_row.addWidget(self._cancel_pending_button)

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
        rail_layout.addWidget(self._pending_label)
        rail_layout.addWidget(self._pending_detail_label)
        rail_layout.addLayout(self._cancel_pending_row)
        rail_layout.addLayout(self._methods_layout)
        # Sign out belongs with the choices, not pinned to the floor. The
        # stretch used to sit here, which pushed it to the bottom of however
        # tall the panel happened to be — on a docked panel the screen read
        # as a title with two buttons at the top and one stray button an inch
        # above the taskbar, related to nothing. A gap says "this one is
        # different" without exiling it.
        rail_layout.addSpacing(theme.SPACING * 4)
        rail_layout.addWidget(self._logout_button)

        # The whole group is centred vertically as well as horizontally: a
        # short list of buttons hugging the top of an empty screen looks like
        # the page failed to finish loading.
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.addStretch(1)
        layout.addWidget(rail, 0, QtCore.Qt.AlignHCenter)
        layout.addStretch(1)
        self._rail = rail

    def minimumSizeHint(self) -> QtCore.QSize:  # noqa: N802 - Qt override
        """Don't let the rail's fixed width become this view's minimum —
        same reason as `Composer.minimumSizeHint`/`SettingsView.
        minimumSizeHint`: a `setFixedWidth` child propagates its width
        upward as a minimum, which would pin the whole panel wide."""
        hint = super().minimumSizeHint()
        return QtCore.QSize(min(hint.width(), _MIN_RAIL_WIDTH), hint.height())

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        """`rail` only had `setMaximumWidth` — its ACTUAL width was whatever
        its narrowest content (a button, "Sign in") wanted, often far
        narrower than 736px. That was invisible for a one-line error, but a
        longer message (the pending-state text, docs/facts/acp-sdk.md §12)
        needs several wrapped lines at that width — and a widget sized via
        `addWidget(rail, 0, AlignHCenter)` computes its height from a
        sizeHint fixed at THAT narrow width, which under-reserves height for
        a WIDER rail's word-wrap and clips the last line. Same fix
        `SettingsView.resizeEvent`/`Composer.resizeEvent` already use for
        exactly this "centred rail, up to 736px" pattern: give it an
        explicit, real width, so the wrapped labels measure themselves
        against the width they will actually have — not a guess.
        """
        super().resizeEvent(event)
        width = max(_MIN_RAIL_WIDTH, min(_RAIL_WIDTH, self.width() - 32))
        self._rail.setFixedWidth(width)

    def set_methods(
        self, methods: list["AuthMethod"], *, can_logout: bool, no_methods_help: str = ""
    ) -> None:
        """Redraw the list of sign-in methods. An empty list isn't an error:
        we say so in words rather than show a blank screen with no
        explanation — `no_methods_help`, when the caller has it, replaces
        the generic "no sign-in methods" line with that agent's own real
        instructions (`AgentPanel._no_methods_advice`)."""
        _clear_layout(self._methods_layout)
        methods = list(methods)
        self._empty_label.setText(
            no_methods_help if (not methods and no_methods_help) else "The agent offered no sign-in methods."
        )
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
        # A fresh method list means whatever was in flight before is moot —
        # e.g. `auth_required` firing again after a failed attempt, or the
        # artist switching to a different agent's sign-in screen entirely.
        self.clear_pending()

    def show_error(self, message: str, method_id: str = "") -> None:
        """Report a failed sign-in on the screen the artist is looking at.

        The method that failed is marked, but never removed. Which methods
        exist is the agent's word — Gemini CLI advertises `oauth-personal`
        and then refuses it for individual accounts — and hiding one on our
        own initiative would mean the day Google fixes it, the panel keeps
        the working door shut. Marking says "this one just failed" without
        pretending to know it will fail forever.
        """
        self.clear_pending()
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

    def set_pending(self, message: str) -> None:
        """`authenticate()` is now in flight for whichever method the artist
        just picked. `message` is composed by the caller (`ui/panel.py`
        knows what each method id actually does — browser vs. an agent's
        own CLI, docs/facts/acp-sdk.md §12) — this view only draws it, same
        division of labour as everywhere else in this file.
        """
        self.clear_error()
        self._pending_label.setTextFormat(QtCore.Qt.PlainText)
        self._pending_label.setOpenExternalLinks(False)
        self._pending_label.setText(message)
        self._pending_label.setVisible(True)
        self._cancel_pending_button.setVisible(True)
        for button in self._buttons.values():
            button.setEnabled(False)
        self._logout_button.setEnabled(False)

    def set_pending_detail(self, text: str) -> None:
        """The agent's own raw stderr line while pending — see the label's
        own docstring for why gemini specifically needs this."""
        self._pending_detail_label.setText(text)
        self._pending_detail_label.setVisible(bool(text))

    def set_terminal_login_link(self, url: str, code: str) -> None:
        """A verification URL a spawned terminal-auth process printed
        (Kimi, docs/facts/acp-sdk.md §14) — the one agent measured where a
        real, clickable link is possible at all (`AgentPanel._start_
        terminal_login` is what spawns and parses it; this only draws the
        result). Replaces the plain pending message with the link itself;
        Cancel above still works, and now also stops that process.
        """
        self.clear_error()
        text = f'<a href="{url}">{url}</a>'
        if code:
            text += f"<br>Code: {code}"
        self._pending_label.setTextFormat(QtCore.Qt.RichText)
        self._pending_label.setOpenExternalLinks(True)
        self._pending_label.setText(text)
        self._pending_label.setVisible(True)
        self._cancel_pending_button.setVisible(True)
        for button in self._buttons.values():
            button.setEnabled(False)
        self._logout_button.setEnabled(False)

    def clear_pending(self) -> None:
        self._pending_label.setVisible(False)
        self._pending_label.setTextFormat(QtCore.Qt.PlainText)
        self._pending_label.setOpenExternalLinks(False)
        self._pending_detail_label.clear()
        self._pending_detail_label.setVisible(False)
        self._cancel_pending_button.setVisible(False)
        for button in self._buttons.values():
            button.setEnabled(True)
        self._logout_button.setEnabled(True)

    def _on_cancel_pending(self) -> None:
        self.clear_pending()
        self.cancel_pending.emit()


__all__ = ["AuthView"]

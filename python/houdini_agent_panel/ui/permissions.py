"""Compact permission-request popover above the composer.

Buttons are built strictly from `view.options`, in the order the agent sent
them: we add none of our own (including a "cancel" — if the agent wants that
option it will send it) and rename none of theirs. Each option's `kind` only
affects emphasis, never the text or the order: `reject_*` take the error
colour from `theme` (by role, not a hardcoded colour — see
`theme.status_color`), `*_always` get bold weight. The decision itself is
kept by TranscriptModel; the interactive popover disappears the moment it is
answered.
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
        # Widened from the original 280–400: a real four-option agent
        # ("Allow once" / "Allow always" / "Reject once" / "Reject always")
        # didn't fit in 400px, and the popover clipped instead of wrapping.
        self.setMinimumWidth(320)
        self.setMaximumWidth(480)
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

        # A grid, not a single row: the title and the buttons are already
        # stacked vertically (a long title just wraps, it never squeezes the
        # buttons), but a single `QHBoxLayout` row of options has no fallback
        # once four real options don't fit — each button just gets squeezed
        # instead of wrapping. Two columns always fit, at any popover width.
        buttons_grid = QtWidgets.QGridLayout()
        buttons_grid.setHorizontalSpacing(theme.SPACING_TIGHT)
        buttons_grid.setVerticalSpacing(theme.SPACING_TIGHT)
        columns = 2 if len(view.options) > 2 else max(len(view.options), 1)
        for index, (option_id, name, kind) in enumerate(view.options):
            button = QtWidgets.QPushButton(name, self)  # text is exactly what the agent sent
            button.setFlat(True)
            button.setMinimumHeight(26)
            button.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
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
            row, col = divmod(index, columns)
            buttons_grid.addWidget(button, row, col)
        layout.addLayout(buttons_grid)

        # Only visible after an answer — the record of what the human chose.
        self._status_label = QtWidgets.QLabel(self)
        self._status_label.setVisible(False)
        layout.addWidget(self._status_label)

        if self._answered is not None:
            self._apply_answered(self._answered)

        # The primary button (`allow_once`) is painted straight onto the
        # theme's accent — which can be a warm amber or a bright Plumtree
        # pink — so its text has to be chosen for contrast against THAT
        # colour rather than assumed, unlike everything else here that just
        # reads a `palette(...)` role.
        primary_bg = theme.to_hex(theme.accent_color())
        primary_text = theme.to_hex(theme.contrasting_text_color(theme.accent_color()))
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
            f" color: {primary_text};"
            f" background: {primary_bg};"
            "}"
        )
        shadow = QtWidgets.QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 7)
        # Deliberately NOT `theme.color(QtGui.QPalette.Shadow)`: checked
        # against a hand-built `QPalette` (no `hou` here to ask), `Shadow`
        # stayed black even with every other role swapped to a light scheme —
        # but that's a bare `QPalette`, not Houdini's own light-scheme output,
        # and there's no way to confirm the same holds there without a real
        # light-themed Houdini GUI session. A shadow's whole job is to read as
        # depth/occlusion, which is dark by convention in light AND dark UI
        # (this codebase's own `.hcs` files don't even define a scheme name
        # for it) — a fixed near-black with alpha is the safe choice here,
        # not a gap in the "no hardcoded colour" rule.
        shadow.setColor(QtGui.QColor(0, 0, 0, 115))  # theme-exception: see comment above
        self.setGraphicsEffect(shadow)

    def request_key(self) -> str:
        return self._view.request_key

    def apply_view(self, view: PermissionView) -> None:
        """Refresh the row from a fresh `PermissionView` (e.g. `answered` arrived elsewhere).

        NOT used by `ui/panel.py` today — `_sync_permission_popover` always
        destroys and rebuilds the popover instead of patching it in place
        (`ui/transcript.py` doesn't touch `PermissionRow` at all; it only
        filters `entry.kind == "permission"` out of the drawn feed). This
        method exists for a caller that DOES want to update one in place —
        currently just the tests — and does the right thing if one shows
        up; it just isn't wired to anything in production right now.
        """
        self._view = view
        if view.answered is not None and self._answered is None:
            self._apply_answered(view.answered)

    def _on_clicked(self, option_id: str) -> None:
        if self._answered is not None:
            return  # buttons are disabled after an answer, but guard here too
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
            self._status_label.setText(f"Chosen: {chosen.text()}")
        else:
            # An option_id that isn't in our own options list — shouldn't
            # happen in normal operation, but we don't crash: just show the
            # raw value.
            self._status_label.setText(f"Chosen: {option_id}")
        self._status_label.setVisible(True)


__all__ = ["PermissionRow"]

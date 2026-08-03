"""Shared visual decisions for the feed widgets.

Not part of the public contract in docs/architecture.md §10 — a helper module
`chips.py`, `transcript.py` and `permissions.py` lean on so their spacing,
fonts and colours don't drift apart. The panel lives inside Houdini as a guest
in someone else's window, so colour always comes from the current
QApplication's palette (`QtWidgets.QApplication.palette()`) rather than
hardcoded hex — Houdini's theme can be dark, light or entirely custom (see
facts/houdini.md §5). `hou` is never imported: this module has to work in unit
tests outside Houdini too.
"""

from __future__ import annotations

from .qt import QtCore, QtGui, QtWidgets

# --- geometry ----------------------------------------------------------

SPACING = 6
SPACING_TIGHT = 3
MARGIN = 8
RADIUS = 4
ICON_SIZE = 16

#: The ten ACP tool-call kinds (facts/acp-sdk.md §4, ToolKind).
TOOL_KINDS: tuple[str, ...] = (
    "read",
    "edit",
    "delete",
    "move",
    "search",
    "execute",
    "think",
    "fetch",
    "switch_mode",
    "other",
)

_KIND_GLYPH: dict[str, str] = {
    "read": "R",
    "edit": "E",
    "delete": "D",
    "move": "M",
    "search": "S",
    "execute": "X",
    "think": "T",
    "fetch": "F",
    "switch_mode": "⇄",
    "other": "•",
}

_STATUS_GLYPH: dict[str, str] = {
    "pending": "○",
    "in_progress": "◐",
    "completed": "✓",
    "failed": "✕",
}

_STATUS_LABEL: dict[str, str] = {
    "pending": "pending",
    "in_progress": "running",
    "completed": "done",
    "failed": "failed",
}


def palette() -> QtGui.QPalette:
    """The host's (Houdini's) palette, not our own — the only source of colour."""
    return QtWidgets.QApplication.palette()


def monospace_font() -> QtGui.QFont:
    """System monospace font — for code/diffs inside an expanded tool call."""
    return QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)


def kind_glyph(kind: str) -> str:
    """A one-letter or symbol badge for a tool call's kind."""
    return _KIND_GLYPH.get(kind, _KIND_GLYPH["other"])


def kind_icon(kind: str, *, size: int = ICON_SIZE) -> QtGui.QIcon:
    """Tool-call icon for a `kind`.

    Houdini's own icons (`hicon:/SVGIcons.index?...`) aren't reachable
    outside Houdini and their exact names aren't confirmed in facts/ — rather
    than guess, we draw a small text badge in the current palette's colours.
    Behaves identically in unit tests and inside Houdini, and needs no `hou`.
    """
    pixmap = QtGui.QPixmap(size, size)
    pixmap.fill(QtCore.Qt.transparent)

    painter = QtGui.QPainter(pixmap)
    try:
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        pal = palette()
        painter.setBrush(pal.color(QtGui.QPalette.Mid))
        painter.setPen(QtCore.Qt.NoPen)
        painter.drawRoundedRect(0, 0, size, size, RADIUS, RADIUS)

        font = painter.font()
        font.setPointSize(max(6, size // 2))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(pal.color(QtGui.QPalette.Text))
        painter.drawText(pixmap.rect(), QtCore.Qt.AlignCenter, kind_glyph(kind))
    finally:
        painter.end()

    return QtGui.QIcon(pixmap)


def status_glyph(status: str) -> str:
    """Status symbol — shape, not colour: the only signal that survives any theme."""
    return _STATUS_GLYPH.get(status, _STATUS_GLYPH["pending"])


def status_label(status: str) -> str:
    """Human-readable status caption for the collapsible tool-call row."""
    return _STATUS_LABEL.get(status, status)


def status_color(status: str) -> QtGui.QColor:
    """Status colour — `QPalette` roles only, nothing of our own.

    `QPalette` has no semantic "error" role: painting `failed` red would mean
    hardcoding a colour around the theme's palette, which this module's rules
    forbid. So `completed`/`failed` differ by the shape of the glyph
    (`status_glyph`), not by colour — while `in_progress`/`pending` take a
    visible accent (`Highlight`) and a muted tone (`Disabled`/`Text`) from
    the palette itself.
    """
    pal = palette()
    if status == "in_progress":
        return pal.color(QtGui.QPalette.Active, QtGui.QPalette.Highlight)
    if status == "pending":
        return pal.color(QtGui.QPalette.Disabled, QtGui.QPalette.Text)
    return pal.color(QtGui.QPalette.Active, QtGui.QPalette.Text)


__all__ = [
    "ICON_SIZE",
    "MARGIN",
    "RADIUS",
    "SPACING",
    "SPACING_TIGHT",
    "TOOL_KINDS",
    "kind_glyph",
    "kind_icon",
    "monospace_font",
    "palette",
    "status_color",
    "status_glyph",
    "status_label",
]

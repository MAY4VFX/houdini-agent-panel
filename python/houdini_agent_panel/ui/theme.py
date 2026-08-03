"""Общие визуальные решения для виджетов ленты.

Не публичный контракт из docs/architecture.md §10 — вспомогательный модуль,
на который опираются `chips.py`, `transcript.py`, `permissions.py`, чтобы не
разъезжаться в отступах, шрифтах и цветах. Панель живёт внутри Houdini как
гость в чужом окне, поэтому цвет всегда берём из палитры текущего QApplication
(`QtWidgets.QApplication.palette()`), а не хардкодим hex — тема Houdini может
быть тёмной, светлой или полностью пользовательской (см. facts/houdini.md §5).

Houdini 22's "Edit Theme" presets (e.g. "Ponycorn Adventure") repaint the
whole application's own widgets, but `QApplication.palette()` alone does not
follow that — `color()` below tries Houdini's own live theme first
(`hou.qt.getColor`) and falls back to the app palette for everything else:
Houdini 20.5 (no such presets), outside Houdini entirely (unit tests,
`dev_preview`), and any `QPalette` role with no solid Houdini scheme analog.
`hou` is only ever imported lazily, inside a try/except, from `color()` —
this module still has to work with no `hou` on the path at all.
"""

from __future__ import annotations

from .qt import QtCore, QtGui, QtWidgets

# --- геометрия ---------------------------------------------------------

SPACING = 6
SPACING_TIGHT = 3
MARGIN = 8
RADIUS = 4
ICON_SIZE = 16

#: Десять видов вызова инструмента протокола ACP (facts/acp-sdk.md §4, ToolKind).
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
    "pending": "ожидание",
    "in_progress": "выполняется",
    "completed": "готово",
    "failed": "ошибка",
}


def palette() -> QtGui.QPalette:
    """Палитра хоста (Houdini), а не своя — единственный источник цвета."""
    return QtWidgets.QApplication.palette()


#: `QPalette` role -> Houdini scheme color name, for roles with a solid,
#: checked analog. Verified by hand against Houdini 22.0.368's own
#: `houdini/config/UIDark.hcs` (and cross-checked against `UILight.hcs`) —
#: guessing a name here and getting `hou.OperationFailed` at paint time is
#: worse than just falling back, so a role stays out of this table rather
#: than get a shaky guess.
_HOU_COLOR_NAMES: dict[QtGui.QPalette.ColorRole, str] = {
    QtGui.QPalette.Window: "BackColor",
    QtGui.QPalette.Base: "BackColor",
    QtGui.QPalette.AlternateBase: "MenuBG",
    QtGui.QPalette.Text: "TextColor",
    QtGui.QPalette.WindowText: "TextColor",
    QtGui.QPalette.ButtonText: "TextColor",
    QtGui.QPalette.Highlight: "MenuSelectedBG",
    QtGui.QPalette.Mid: "SplitBarBackground",
}

#: The one `(group, role)` pair that needs a different scheme name than its
#: `Active` counterpart — everything else falls back to the app palette for
#: any non-`Active` group, which is what every call site needs today.
_HOU_DISABLED_TEXT = "DisabledTextColor"


def _hou_scheme_color(name: str) -> QtGui.QColor | None:
    """`hou.qt.getColor(name)`, or `None` for any reason at all.

    `hou` may not be importable (outside Houdini entirely), `hou.qt` may not
    exist (Houdini 20.5 hython confirmed: `hou.isUIAvailable()` is `False`
    and `hou.qt` isn't even there), or the name may not exist in whatever
    scheme is active. All three, and anything else, mean "fall back to the
    app palette" — this never raises.
    """
    try:
        import hou
    except ImportError:
        return None
    try:
        if not hou.isUIAvailable():
            return None
        return hou.qt.getColor(name)
    except Exception:  # noqa: BLE001 - a theme lookup has no right to break paint code
        return None


def color(
    role: QtGui.QPalette.ColorRole,
    group: QtGui.QPalette.ColorGroup = QtGui.QPalette.Active,
) -> QtGui.QColor:
    """One color: Houdini's own live theme first, the app palette otherwise.

    This is the entry point every call site that used to read
    `self.palette()`/`QApplication.palette()` for a single role goes
    through now — a Houdini 22 "Edit Theme" preset changes what this
    returns without the panel doing anything theme-specific itself. Houdini
    20.5 and anything outside Houdini fall straight through to
    `palette().color(group, role)`, byte-for-byte what every call site
    already did before this existed.
    """
    if group == QtGui.QPalette.Disabled and role == QtGui.QPalette.Text:
        name = _HOU_DISABLED_TEXT
    else:
        name = _HOU_COLOR_NAMES.get(role)
    if name is not None:
        hou_color = _hou_scheme_color(name)
        if hou_color is not None:
            return QtGui.QColor(hou_color)
    return palette().color(group, role)


def monospace_font() -> QtGui.QFont:
    """Системный моноширинный шрифт — для кода/diff в развёрнутом вызове инструмента."""
    return QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)


def kind_glyph(kind: str) -> str:
    """Однобуквенный/символьный бейдж для вида вызова инструмента."""
    return _KIND_GLYPH.get(kind, _KIND_GLYPH["other"])


def kind_icon(kind: str, *, size: int = ICON_SIZE) -> QtGui.QIcon:
    """Иконка вызова инструмента по `kind`.

    Штатные иконки Houdini (`hicon:/SVGIcons.index?...`) недоступны вне
    Houdini и их точные имена не подтверждены в facts/ — вместо угадывания
    рисуем маленький текстовый бейдж цветами текущей палитры (`color()`, so
    it tracks Houdini's own live theme where that's available). Работает
    одинаково в юнит-тестах и внутри Houdini — `hou` не обязателен.
    """
    pixmap = QtGui.QPixmap(size, size)
    pixmap.fill(QtCore.Qt.transparent)

    painter = QtGui.QPainter(pixmap)
    try:
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setBrush(color(QtGui.QPalette.Mid))
        painter.setPen(QtCore.Qt.NoPen)
        painter.drawRoundedRect(0, 0, size, size, RADIUS, RADIUS)

        font = painter.font()
        font.setPointSize(max(6, size // 2))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(color(QtGui.QPalette.Text))
        painter.drawText(pixmap.rect(), QtCore.Qt.AlignCenter, kind_glyph(kind))
    finally:
        painter.end()

    return QtGui.QIcon(pixmap)


def status_glyph(status: str) -> str:
    """Символ статуса — форма, не цвет: единственный надёжный сигнал вне зависимости от темы."""
    return _STATUS_GLYPH.get(status, _STATUS_GLYPH["pending"])


def status_label(status: str) -> str:
    """Человекочитаемая подпись статуса для сворачиваемой строки вызова инструмента."""
    return _STATUS_LABEL.get(status, status)


def status_color(status: str) -> QtGui.QColor:
    """Цвет статуса — только роли `QPalette`, ничего своего.

    У `QPalette` нет семантической роли «ошибка»: подкрашивать `failed` в
    красный значило бы хардкодить цвет в обход палитры темы, что запрещено
    правилами модуля. Поэтому `completed`/`failed` различаются формой значка
    (`status_glyph`), а не цветом — `in_progress`/`pending` получают
    заметный акцент (`Highlight`) и приглушение (`Disabled`/`Text`)
    из самой палитры.
    """
    if status == "in_progress":
        return color(QtGui.QPalette.Highlight)
    if status == "pending":
        return color(QtGui.QPalette.Text, QtGui.QPalette.Disabled)
    return color(QtGui.QPalette.Text)


__all__ = [
    "ICON_SIZE",
    "MARGIN",
    "RADIUS",
    "SPACING",
    "SPACING_TIGHT",
    "TOOL_KINDS",
    "color",
    "kind_glyph",
    "kind_icon",
    "monospace_font",
    "palette",
    "status_color",
    "status_glyph",
    "status_label",
]

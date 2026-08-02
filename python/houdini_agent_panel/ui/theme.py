"""Общие визуальные решения для виджетов ленты.

Не публичный контракт из docs/architecture.md §10 — вспомогательный модуль,
на который опираются `chips.py`, `transcript.py`, `permissions.py`, чтобы не
разъезжаться в отступах, шрифтах и цветах. Панель живёт внутри Houdini как
гость в чужом окне, поэтому цвет всегда берём из палитры текущего QApplication
(`QtWidgets.QApplication.palette()`), а не хардкодим hex — тема Houdini может
быть тёмной, светлой или полностью пользовательской (см. facts/houdini.md §5).
`hou` не импортируем: модуль обязан работать и в юнит-тестах вне Houdini.
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
    рисуем маленький текстовый бейдж цветами текущей палитры. Работает
    одинаково в юнит-тестах и внутри Houdini, не требует `hou`.
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

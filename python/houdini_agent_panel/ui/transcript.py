"""`TranscriptView` — лента панели (docs/architecture.md §10, §8).

Отрисовывает `TranscriptModel.entries()` построчно, без рамок у сообщений.
Два требования критичны для ощущения от панели (design.md, «Середина»):

- `refresh(entry_id)` патчит ОДНУ запись на месте, не пересоздавая остальные
  виджеты — полная перерисовка (`refresh(None)`) только при смене сессии.
  Перерисовка всей ленты на каждый чанк стрима видна глазом и тормозит.
- Автопрокрутка вниз работает, только если человек и так был внизу: отлистал
  вверх читать — оставляем его там, а не утаскиваем к последнему чанку.
"""

from __future__ import annotations

from ..transcript_model import Entry, TranscriptModel
from . import theme
from .permissions import PermissionRow
from .qt import QtCore, QtGui, QtWidgets, Signal

#: Сколько пикселей до низа ещё считается «внизу» — маленький запас на
#: округления layout'а, чтобы автопрокрутка не отваливалась от одного пикселя.
_BOTTOM_EPSILON = 4


class TranscriptView(QtWidgets.QScrollArea):
    permission_answered = Signal(str, str)  # request_key, option_id ("" = отменено)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        # Длинный вывод инструмента не должен растягивать панель по горизонтали —
        # весь текст внутри строк переносится по словам, горизонтальный скролл не нужен.
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)

        self._content = QtWidgets.QWidget(self)
        self._layout = QtWidgets.QVBoxLayout(self._content)
        self._layout.setContentsMargins(theme.MARGIN, theme.MARGIN, theme.MARGIN, theme.MARGIN)
        self._layout.setSpacing(theme.SPACING)
        # Стретч в конце — записи прижимаются к верху, а не растягиваются по
        # всей высоте вьюпорта, пока лента короткая.
        self._layout.addStretch(1)
        self.setWidget(self._content)

        self._model: TranscriptModel | None = None
        self._rows: dict[str, QtWidgets.QWidget] = {}

    # --- публичный API -------------------------------------------------

    def set_model(self, model: TranscriptModel) -> None:
        self._model = model
        self.refresh(None)

    def refresh(self, entry_id: str | None = None) -> None:
        if self._model is None:
            return
        was_at_bottom = self._is_at_bottom()
        if entry_id is None:
            self._rebuild_all()
        else:
            self._refresh_one(entry_id)
        if was_at_bottom:
            self._scroll_to_bottom()

    # --- перестройка ------------------------------------------------------

    def _rebuild_all(self) -> None:
        for row in list(self._rows.values()):
            self._layout.removeWidget(row)
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()

        for entry in self._model.entries():
            row = self._make_row(entry)
            self._rows[entry.id] = row
            self._layout.insertWidget(self._layout.count() - 1, row)

    def _refresh_one(self, entry_id: str) -> None:
        entries = self._model.entries()
        entry = next((e for e in entries if e.id == entry_id), None)

        if entry is None:
            # Протокол сегодня записи не удаляет, но не падаем, если это
            # когда-нибудь изменится — просто снимаем строку со сцены.
            row = self._rows.pop(entry_id, None)
            if row is not None:
                self._layout.removeWidget(row)
                row.setParent(None)
                row.deleteLater()
            return

        row = self._rows.get(entry_id)
        if row is not None:
            self._update_row(row, entry)
            return

        # Новая запись — вставляем на её позицию среди уже отрисованных.
        # Записи из TranscriptModel всегда добавляются в конец и не
        # переставляются, так что позиция среди уже отрисованных строк
        # совпадает с индексом записи в полном списке модели.
        index = entries.index(entry)
        row = self._make_row(entry)
        self._rows[entry.id] = row
        self._layout.insertWidget(index, row)

    # --- сборка строк по kind ----------------------------------------------

    def _make_row(self, entry: Entry) -> QtWidgets.QWidget:
        if entry.kind == "tool":
            return _ToolCallRow(entry)
        if entry.kind == "plan":
            return _PlanRow(entry)
        if entry.kind == "permission":
            row = PermissionRow(entry.permission)
            row.answered.connect(self.permission_answered)
            return row
        return _MessageRow(entry)

    def _update_row(self, row: QtWidgets.QWidget, entry: Entry) -> None:
        if isinstance(row, PermissionRow):
            row.apply_view(entry.permission)
            return
        row.update_from(entry)

    # --- автопрокрутка -----------------------------------------------------

    def _is_at_bottom(self) -> bool:
        bar = self.verticalScrollBar()
        return bar.value() >= bar.maximum() - _BOTTOM_EPSILON

    def _scroll_to_bottom(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())


class _MessageRow(QtWidgets.QWidget):
    """Сообщение (user/agent/thought) или ошибка — без рамок, текст выделяется мышью."""

    def __init__(self, entry: Entry, parent=None) -> None:
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._label = QtWidgets.QLabel(self)
        self._label.setWordWrap(True)
        self._label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        layout.addWidget(self._label)

        self._apply_kind_style(entry.kind)
        self._set_text(entry.text)

    def update_from(self, entry: Entry) -> None:
        self._set_text(entry.text)

    def _set_text(self, text: str) -> None:
        self._label.setText(text)

    def _apply_kind_style(self, kind: str) -> None:
        font = self._label.font()
        palette = self._label.palette()
        if kind == "thought":
            # Мысль агента — приглушённая и курсивом, чтобы не спорить взглядом
            # с обычным текстом ответа.
            font.setItalic(True)
            palette.setColor(QtGui.QPalette.WindowText, theme.status_color("pending"))
        elif kind == "error":
            font.setBold(True)
        self._label.setFont(font)
        self._label.setPalette(palette)


class _ToolCallRow(QtWidgets.QWidget):
    """Сворачиваемая строка вызова инструмента: иконка по `kind`, живой статус."""

    def __init__(self, entry: Entry, parent=None) -> None:
        super().__init__(parent)
        self._entry_id = entry.id
        self._expanded = False

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACING_TIGHT)

        self._toggle = QtWidgets.QToolButton(self)
        self._toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self._toggle.setArrowType(QtCore.Qt.RightArrow)
        self._toggle.setAutoRaise(True)
        self._toggle.setCheckable(True)
        self._toggle.clicked.connect(self._on_toggled)
        layout.addWidget(self._toggle)

        self._details = QtWidgets.QLabel(self)
        self._details.setWordWrap(True)
        self._details.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        self._details.setFont(theme.monospace_font())
        self._details.setVisible(False)
        layout.addWidget(self._details)

        self.update_from(entry)

    def update_from(self, entry: Entry) -> None:
        tool = entry.tool
        self._tool = tool
        self._toggle.setIcon(theme.kind_icon(tool.kind))
        status_text = f"{theme.status_glyph(tool.status)} {tool.title} — {theme.status_label(tool.status)}"
        self._toggle.setText(status_text)
        palette = self._toggle.palette()
        palette.setColor(QtGui.QPalette.ButtonText, theme.status_color(tool.status))
        self._toggle.setPalette(palette)
        if self._expanded:
            self._render_details()

    def _on_toggled(self, checked: bool) -> None:
        self._expanded = checked
        self._toggle.setArrowType(QtCore.Qt.DownArrow if checked else QtCore.Qt.RightArrow)
        if checked:
            self._render_details()
        self._details.setVisible(checked)

    def _render_details(self) -> None:
        self._details.setText(_format_tool_content(self._tool.content, self._tool.locations))


def _format_tool_content(content: list[dict], locations: list[dict]) -> str:
    parts: list[str] = []
    for item in content:
        item_type = item.get("type")
        if item_type == "diff":
            path = item.get("path", "")
            old_text = item.get("old_text")
            new_text = item.get("new_text", "")
            if old_text:
                parts.append(f"--- {path}\n{old_text}\n+++ {path}\n{new_text}")
            else:
                parts.append(f"+++ {path}\n{new_text}")
        elif item_type == "terminal":
            parts.append(f"[терминал {item.get('terminal_id', '?')}]")
        elif item_type == "content":
            block = item.get("content") or {}
            text = block.get("text")
            parts.append(text if text is not None else str(block))
        else:
            parts.append(str(item))

    if locations:
        paths = ", ".join(loc.get("path", "?") for loc in locations)
        parts.append(f"[файлы: {paths}]")

    return "\n\n".join(parts) if parts else "(без содержимого)"


class _PlanRow(QtWidgets.QWidget):
    """План агента — блок со списком шагов и их статусами."""

    def __init__(self, entry: Entry, parent=None) -> None:
        super().__init__(parent)
        self._layout = QtWidgets.QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(theme.SPACING_TIGHT)

        self._title = QtWidgets.QLabel("План", self)
        font = self._title.font()
        font.setBold(True)
        self._title.setFont(font)
        self._layout.addWidget(self._title)

        self._step_labels: list[QtWidgets.QLabel] = []
        self.update_from(entry)

    def update_from(self, entry: Entry) -> None:
        steps = entry.plan
        # Переиспользуем уже созданные QLabel там, где можем — обычно план
        # правится по количеству шагов не сильно, но на первом рендере или
        # при изменении длины просто досоздаём/убираем недостающее.
        while len(self._step_labels) < len(steps):
            label = QtWidgets.QLabel(self)
            label.setWordWrap(True)
            label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
            self._step_labels.append(label)
            self._layout.addWidget(label)
        while len(self._step_labels) > len(steps):
            label = self._step_labels.pop()
            self._layout.removeWidget(label)
            label.setParent(None)
            label.deleteLater()

        for label, step in zip(self._step_labels, steps):
            glyph = {"pending": "○", "in_progress": "◐", "completed": "✓"}.get(step.status, "○")
            label.setText(f"{glyph} {step.content}")


__all__ = ["TranscriptView"]

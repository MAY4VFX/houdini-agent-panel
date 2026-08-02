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

import re

from ..transcript_model import Entry, TranscriptModel
from . import theme
from .permissions import PermissionRow
from .qt import QtCore, QtGui, QtWidgets, Signal

#: Сколько пикселей до низа ещё считается «внизу» — маленький запас на
#: округления layout'а, чтобы автопрокрутка не отваливалась от одного пикселя.
_BOTTOM_EPSILON = 4

#: `QTextEdit.setMarkdown` есть с Qt 5.14 — в PySide2 5.15.15 (H20.5) и
#: PySide6 6.8.3 (H22) он точно есть (facts/houdini.md §3), но проверяем
#: динамически, а не полагаемся на версию: деградация на обычный текст лучше
#: падения, если метод вдруг отсутствует в чьей-то сборке.
_HAS_MARKDOWN = hasattr(QtWidgets.QTextEdit, "setMarkdown")

#: Тройные бэктики — с необязательным языком на той же строке. Незакрытый
#: fence (агент ещё не дострил ```` ``` ```` на конце стрима) матчится до
#: конца строки — так частично пришедший код рендерится кодом, а не сырыми
#: бэктиками посреди текста.
_CODE_FENCE_RE = re.compile(r"```[a-zA-Z0-9_+-]*\r?\n(.*?)(?:```|\Z)", re.DOTALL)


def _split_markdown_segments(text: str) -> list[tuple[str, str]]:
    """Разбить текст на чередующиеся куски ``("text", ...)`` / ``("code", ...)``.

    Блок кода рендерится ОТДЕЛЬНЫМ виджетом с собственной горизонтальной
    прокруткой и без переноса строк (иначе ломается отступ VEX/питона) —
    поэтому он не может быть просто частью общего markdown-документа прозы,
    у которого перенос по словам должен работать как обычно.
    """
    segments: list[tuple[str, str]] = []
    pos = 0
    for match in _CODE_FENCE_RE.finditer(text):
        before = text[pos : match.start()]
        if before.strip():
            segments.append(("text", before))
        segments.append(("code", match.group(1)))
        pos = match.end()
    tail = text[pos:]
    if tail.strip() or not segments:
        segments.append(("text", tail))
    return segments


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
    """Сообщение (user/agent/thought) или ошибка — без рамок, текст выделяется мышью.

    Автор реплики должен читаться с одного взгляда, без вчитывания (design.md
    просит именно «без рамок», поэтому различаем цветом/отступом, не боксом):
    реплика человека — приглушённым цветом палитры и с отступом слева, ответ
    агента — обычным цветом на всю ширину, размышление — курсивом и тоже
    приглушённое, но без отступа (это не вопрос человека, а мысль агента).

    Текст рендерится через `QTextDocument.setMarkdown` — агент присылает
    markdown (бэктики, **жирный**, списки, ```code```) постоянно, и это
    единственный путь показать его отформатированным, не скармливая
    недоверенный текст агента в `setHtml` напрямую. Блоки кода вырезаются из
    markdown и рендерятся отдельными моноширинными виджетами со своей
    горизонтальной прокруткой — прозу это не касается, она просто переносится
    по словам.
    """

    def __init__(self, entry: Entry, parent=None) -> None:
        super().__init__(parent)
        self._layout = QtWidgets.QVBoxLayout(self)
        self._layout.setSpacing(theme.SPACING_TIGHT)
        self._apply_kind_margins(entry.kind)
        self._segments: list[QtWidgets.QWidget] = []
        self.update_from(entry)

    def update_from(self, entry: Entry) -> None:
        segments = _split_markdown_segments(entry.text)

        # Стриминг чаще всего просто дописывает текст в последний кусок, не
        # меняя число/тип кусков — тогда достаточно обновить содержимое на
        # месте, не пересоздавая виджеты (та же логика, что и у остальной
        # ленты: патчим, а не строим заново).
        same_shape = len(segments) == len(self._segments) and all(
            isinstance(widget, _CodeBlock) == (kind == "code")
            for widget, (kind, _content) in zip(self._segments, segments)
        )
        if same_shape:
            for widget, (kind, content) in zip(self._segments, segments):
                if kind == "code":
                    widget.set_code(content)
                else:
                    widget.set_text(content)
            return

        for widget in self._segments:
            self._layout.removeWidget(widget)
            widget.setParent(None)
            widget.deleteLater()
        self._segments = []

        for kind, content in segments:
            if kind == "code":
                widget = _CodeBlock(content, self)
            else:
                widget = _ProseBlock(self)
                self._apply_kind_style(widget, entry.kind)
                widget.set_text(content)
            self._segments.append(widget)
            self._layout.addWidget(widget)

    def _apply_kind_margins(self, kind: str) -> None:
        # Отступ — визуальный маркер «это ввёл человек», без рамок и боксов.
        indent = theme.SPACING * 4 if kind == "user" else 0
        self._layout.setContentsMargins(indent, 0, 0, 0)

    def _apply_kind_style(self, widget: "_ProseBlock", kind: str) -> None:
        font = widget.font()
        palette = widget.palette()
        if kind in ("user", "thought"):
            # И реплика человека, и мысль агента — приглушённые: первая явно
            # видна по отступу, вторая — курсивом ниже.
            palette.setColor(QtGui.QPalette.Text, theme.status_color("pending"))
        if kind == "thought":
            font.setItalic(True)
        elif kind == "error":
            font.setBold(True)
        widget.setFont(font)
        widget.setPalette(palette)


class _ProseBlock(QtWidgets.QTextBrowser):
    """Кусок markdown-прозы сообщения — без рамки, с переносом по словам.

    Высота подгоняется под содержимое: внутренний вертикальный скролл не
    нужен, лента и так скроллится целиком (`TranscriptView`), а свой скролл
    внутри строки сообщения был бы лишним уровнем прокрутки.
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setReadOnly(True)
        # Ссылки — во внешний браузер: панель не файловый менеджер и не веб-вьюер.
        self.setOpenExternalLinks(True)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        # Без собственной заливки — фон ленты просвечивает, никакого бокса.
        self.setAutoFillBackground(False)
        self.viewport().setAutoFillBackground(False)
        self.document().documentLayout().documentSizeChanged.connect(self._sync_height)

    def set_text(self, text: str) -> None:
        if _HAS_MARKDOWN:
            self.setMarkdown(text)
        else:  # pragma: no cover — на всех целевых Qt (5.14+, см. facts/houdini.md §3) есть
            self.setPlainText(text)
        self._sync_height()

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:
        super().resizeEvent(event)
        self.document().setTextWidth(self.viewport().width())
        self._sync_height()

    def _sync_height(self, *_args: object) -> None:
        self.setFixedHeight(max(int(self.document().size().height()) + 4, 1))


class _CodeBlock(QtWidgets.QPlainTextEdit):
    """Блок кода из ```fence``` — моноширинный, своя горизонтальная прокрутка.

    `NoWrap` намеренно: перенос сломал бы отступы VEX/питона. Длинная строка
    скроллится ВНУТРИ этого виджета (`sizeHint()` у `QPlainTextEdit` не растёт
    от длины документа — см. `_sync_height`, ширину задаёт только layout), а
    не раздвигает панель по горизонтали — снаружи `TranscriptView` держит
    `ScrollBarAlwaysOff` именно ради этого.
    """

    def __init__(self, code: str, parent=None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(theme.monospace_font())
        self.setFrameShape(QtWidgets.QFrame.NoFrame)
        self.setLineWrapMode(QtWidgets.QPlainTextEdit.NoWrap)
        self.setVerticalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAsNeeded)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)
        palette = self.palette()
        # Едва заметная подложка — из палитры (роль именно для такого
        # «альтернативного» блока), не рамка и не хардкод-цвет: просто
        # отличает код от прозы вокруг него.
        palette.setColor(QtGui.QPalette.Base, theme.palette().color(QtGui.QPalette.AlternateBase))
        self.setPalette(palette)
        self.set_code(code)

    def set_code(self, code: str) -> None:
        self.setPlainText(code.rstrip("\n"))
        self._sync_height()

    def _sync_height(self) -> None:
        lines = max(self.document().blockCount(), 1)
        self.setFixedHeight(lines * self.fontMetrics().lineSpacing() + 8)


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

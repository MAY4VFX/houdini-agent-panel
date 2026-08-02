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
from .qt import QtCore, QtGui, QtWidgets
from .thinking import ThinkingIndicator

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
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        # Длинный вывод инструмента не должен растягивать панель по горизонтали —
        # весь текст внутри строк переносится по словам, горизонтальный скролл не нужен.
        self.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)

        self._content = QtWidgets.QWidget(self)
        self._layout = QtWidgets.QVBoxLayout(self._content)
        self._layout.setContentsMargins(14, 39, 14, 8)
        self._layout.setSpacing(14)
        # Activity rows остаются в хронологии: user → Worked for… → answer.
        self._layout.addStretch(1)
        self.setWidget(self._content)

        self._model: TranscriptModel | None = None
        self._rows: dict[str, QtWidgets.QWidget] = {}

    # --- публичный API -------------------------------------------------

    def set_model(self, model: TranscriptModel) -> None:
        self._model = model
        self.refresh(None)

    def reset_thinking_after_tool(self) -> None:
        for row in reversed(tuple(self._rows.values())):
            if isinstance(row, _ActivityRow) and row.indicator.is_active():
                row.indicator.reset_after_tool()
                return

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
            if entry.kind == "permission":
                continue
            row = self._make_row(entry)
            self._rows[entry.id] = row
            self._layout.insertWidget(len(self._rows) - 1, row)

    def _refresh_one(self, entry_id: str) -> None:
        entries = self._model.entries()
        entry = next((e for e in entries if e.id == entry_id), None)

        if entry is not None and entry.kind == "permission":
            row = self._rows.pop(entry_id, None)
            if row is not None:
                self._layout.removeWidget(row)
                row.setParent(None)
                row.deleteLater()
            return

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
        index = sum(
            1
            for candidate in entries[: entries.index(entry)]
            if candidate.kind != "permission"
        )
        row = self._make_row(entry)
        self._rows[entry.id] = row
        self._layout.insertWidget(index, row)

    # --- сборка строк по kind ----------------------------------------------

    def _make_row(self, entry: Entry) -> QtWidgets.QWidget:
        if entry.kind == "activity":
            return _ActivityRow(entry)
        if entry.kind == "tool":
            return _ToolCallRow(entry)
        if entry.kind == "plan":
            return _PlanRow(entry)
        return _MessageRow(entry)

    def _update_row(self, row: QtWidgets.QWidget, entry: Entry) -> None:
        row.update_from(entry)

    # --- автопрокрутка -----------------------------------------------------

    def _is_at_bottom(self) -> bool:
        bar = self.verticalScrollBar()
        return bar.value() >= bar.maximum() - _BOTTOM_EPSILON

    def _scroll_to_bottom(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        # Both supplied references use a 706–736 px reading rail.  Wider
        # Houdini panes add quiet gutters instead of stretching prose forever.
        gutter = max(14, (self.viewport().width() - 736) // 2)
        margins = self._layout.contentsMargins()
        self._layout.setContentsMargins(gutter, margins.top(), gutter, margins.bottom())


class _ActivityRow(QtWidgets.QWidget):
    """Spinner while active; compact Worked-for divider after completion."""

    def __init__(self, entry: Entry, parent=None) -> None:
        super().__init__(parent)
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(0)
        self.indicator = ThinkingIndicator(self)
        self.indicator.setMinimumHeight(30)
        layout.addWidget(self.indicator)
        rule = QtWidgets.QFrame(self)
        rule.setFrameShape(QtWidgets.QFrame.HLine)
        rule.setObjectName("activityRule")
        layout.addWidget(rule)
        self.setStyleSheet("QFrame#activityRule { color: palette(mid); }")
        self.update_from(entry)

    def update_from(self, entry: Entry) -> None:
        activity = entry.activity
        if activity is None:
            self.indicator.clear_activity()
            return
        if activity.finished_at is None:
            if not self.indicator.is_active():
                self.indicator.start(activity.started_at)
            return
        elapsed_ms = max(0, int((activity.finished_at - activity.started_at) * 1000))
        self.indicator.finish(elapsed_ms)


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
        self._kind = entry.kind
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
            alignment = QtCore.Qt.AlignRight if entry.kind == "user" else QtCore.Qt.Alignment()
            self._layout.addWidget(widget, 0, alignment)

    def _apply_kind_margins(self, kind: str) -> None:
        # Отступ — визуальный маркер «это ввёл человек», без рамок и боксов.
        indent = theme.SPACING * 4 if kind == "user" else 0
        bottom = 32 if kind == "user" else 0
        self._layout.setContentsMargins(indent, 0, 0, bottom)

    def _apply_kind_style(self, widget: "_ProseBlock", kind: str) -> None:
        font = widget.font()
        palette = widget.palette()
        if kind == "thought":
            # Мысль вторична; пользовательский bubble, напротив, держит
            # нормальный контраст как в референсах Claude/Codex.
            palette.setColor(QtGui.QPalette.Text, theme.status_color("pending"))
        elif kind == "user":
            user_text = palette.color(QtGui.QPalette.Text)
            user_text.setAlpha(230)
            palette.setColor(QtGui.QPalette.Text, user_text)
        if kind == "thought":
            font.setItalic(True)
        elif kind == "error":
            font.setBold(True)
        if kind == "user":
            widget.setMaximumWidth(540)
            widget.document().setDocumentMargin(8)
            widget.setStyleSheet(
                "QTextBrowser {"
                " border: none;"
                " border-radius: 12px;"
                " background: palette(alternate-base);"
                " padding: 2px 4px;"
                "}"
            )
        widget.setFont(font)
        widget.setPalette(palette)

    def resizeEvent(self, event: QtGui.QResizeEvent) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if self._kind == "user":
            maximum = max(220, int(self.width() * 0.74))
            for widget in self._segments:
                widget.setMaximumWidth(maximum)


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


class _ToolTrigger(QtWidgets.QAbstractButton):
    """Flat, fully painted disclosure row — no native Qt arrow/button chrome."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setMinimumHeight(34)

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(5, 0, 5, 0)
        layout.setSpacing(7)
        self._kind = QtWidgets.QLabel(self)
        self._title = QtWidgets.QLabel(self)
        self._status = QtWidgets.QLabel(self)
        self._chevron = QtWidgets.QLabel("›", self)
        layout.addWidget(self._kind)
        layout.addWidget(self._title)
        layout.addStretch(1)
        layout.addWidget(self._status)
        layout.addWidget(self._chevron)

    def set_view(self, *, kind: str, title: str, status: str) -> None:
        self._kind.setText(theme.kind_glyph(kind))
        self._title.setText(title)
        self._status.setText(f"{theme.status_glyph(status)}  {theme.status_label(status)}")
        color = theme.status_color(status)
        palette = self._status.palette()
        palette.setColor(QtGui.QPalette.WindowText, color)
        self._status.setPalette(palette)
        self.setAccessibleName(self.text())
        self.update()

    def text(self) -> str:
        return f"{self._title.text()} — {self._status.text()}"

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802
        del event
        painter = QtGui.QPainter(self)
        if self.underMouse():
            hover = self.palette().color(QtGui.QPalette.AlternateBase)
            painter.fillRect(self.rect(), hover)
        divider = self.palette().color(QtGui.QPalette.Mid)
        divider.setAlpha(155)
        painter.setPen(divider)
        painter.drawLine(0, self.height() - 1, self.width(), self.height() - 1)

    def nextCheckState(self) -> None:  # noqa: N802
        super().nextCheckState()
        self._chevron.setText("⌄" if self.isChecked() else "›")


class _ToolCallRow(QtWidgets.QWidget):
    """Сворачиваемая строка вызова инструмента: иконка по `kind`, живой статус."""

    def __init__(self, entry: Entry, parent=None) -> None:
        super().__init__(parent)
        self.setMaximumWidth(560)
        self._entry_id = entry.id
        self._expanded = False

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.SPACING_TIGHT)

        self._toggle = _ToolTrigger(self)
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
        self._toggle.set_view(kind=tool.kind, title=tool.title, status=tool.status)
        if self._expanded:
            self._render_details()

    def _on_toggled(self, checked: bool) -> None:
        self._expanded = checked
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

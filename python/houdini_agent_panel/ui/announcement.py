"""Плашки над лентой (`NoticeStrip`) и над полем ввода (`BlockingNotice`).

Оба класса нарочно ничего не знают о сети, о том, что запись пришла из фида
или из проверки версий, и не решают про `settings.seen_announcements` — это
дело вызывающего кода (см. docs/design.md «Оповещения»). Здесь только
отрисовка того, что уже разобрано в `Announcement`/`Update`, и сигналы наружу
о нажатых кнопках.

`NoticeStrip` — тихая строка: закрывается по кнопке и не блокирует ничего.
`BlockingNotice` — попап НАД полем ввода: сам виджет ничего не блокирует
(`Composer.block_input`/`unblock_input` — забота вызывающего), он только
показывает сообщение и отдаёт нажатия кнопок.
"""

from __future__ import annotations

from ..announcements import Announcement
from ..updates import Update
from .qt import QtWidgets, Signal


class NoticeStrip(QtWidgets.QWidget):
    """Строка над лентой: обычное оповещение или уведомление об обновлении.

    `action_clicked(id, url)` — для оповещения `id` это `Announcement.id`,
    `url` — ссылка нажатой кнопки. Для обновления `id` — `Update.target`
    (agent_id или имя пакета), `url` всегда пустой: там нет ссылки, кнопка
    «Обновить» — сигнал вызывающему запустить установку самому.
    """

    action_clicked = Signal(str, str)
    dismissed = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._id = ""
        self._buttons_row: QtWidgets.QWidget | None = None

        self._label = QtWidgets.QLabel()
        self._label.setWordWrap(True)

        close_button = QtWidgets.QToolButton()
        close_button.setText("✕")
        close_button.setAutoRaise(True)
        close_button.setToolTip("Закрыть")
        close_button.clicked.connect(self._on_close)
        self._close_button = close_button

        self._layout = QtWidgets.QHBoxLayout(self)
        self._layout.setContentsMargins(6, 4, 6, 4)
        self._layout.addWidget(self._label, 1)
        self._layout.addWidget(close_button)

        self.setVisible(False)

    # --- публичное -----------------------------------------------------

    def show_notice(self, ann: Announcement) -> None:
        self._id = ann.id
        self._label.setText(ann.title)
        self._set_buttons([(b.label, b.url) for b in ann.buttons])
        self.setVisible(True)

    def show_update(self, update: Update) -> None:
        self._id = update.target
        self._label.setText(f"Есть обновление: {update.label} ({update.current} → {update.latest})")
        self._set_buttons([("Обновить", "")])
        self.setVisible(True)

    # --- внутреннее ------------------------------------------------------

    def _set_buttons(self, buttons: list[tuple[str, str]]) -> None:
        if self._buttons_row is not None:
            self._layout.removeWidget(self._buttons_row)
            # `setParent(None)` отвязывает СРАЗУ (иначе виджет ещё числится
            # ребёнком до следующего прохода цикла событий, и повторный показ
            # оповещения на мгновение видел бы кнопки старого и нового разом).
            self._buttons_row.setParent(None)
            self._buttons_row.deleteLater()
            self._buttons_row = None
        if not buttons:
            return
        row = QtWidgets.QWidget()
        row_layout = QtWidgets.QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        for label, url in buttons:
            btn = QtWidgets.QPushButton(label)
            btn.clicked.connect(lambda checked=False, u=url: self.action_clicked.emit(self._id, u))
            row_layout.addWidget(btn)
        # Кнопки — сразу после текста, до крестика закрытия.
        self._layout.insertWidget(1, row)
        self._buttons_row = row

    def _on_close(self) -> None:
        ann_id = self._id
        self.setVisible(False)
        self.dismissed.emit(ann_id)


class BlockingNotice(QtWidgets.QWidget):
    """Попап над полем ввода — рисуется строго из присланных кнопок.

    Сам виджет не трогает ввод: то, что появление `BlockingNotice` обязано
    заблокировать поле композера, а нажатие кнопки — разблокировать, решает
    код, который его показывает (владеет и `BlockingNotice`, и `Composer`).
    Здесь — только рендер сообщения и сигнал о нажатой кнопке.
    """

    action_clicked = Signal(str, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._id = ""

        self._title = QtWidgets.QLabel()
        self._title.setWordWrap(True)
        self._title.setStyleSheet("font-weight: bold;")

        self._body = QtWidgets.QLabel()
        self._body.setWordWrap(True)

        self._buttons_row = QtWidgets.QWidget()
        self._buttons_layout = QtWidgets.QHBoxLayout(self._buttons_row)
        self._buttons_layout.setContentsMargins(0, 0, 0, 0)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(self._title)
        layout.addWidget(self._body)
        layout.addWidget(self._buttons_row)

        self.setVisible(False)

    def show_notice(self, ann: Announcement) -> None:
        self._id = ann.id
        self._title.setText(ann.title)
        self._body.setText(ann.body)
        self._body.setVisible(bool(ann.body))

        while self._buttons_layout.count():
            item = self._buttons_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()

        for button in ann.buttons:
            btn = QtWidgets.QPushButton(button.label)
            btn.clicked.connect(lambda checked=False, url=button.url: self.action_clicked.emit(self._id, url))
            self._buttons_layout.addWidget(btn)
        self._buttons_layout.addStretch(1)

        self.setVisible(True)

    def hide_notice(self) -> None:
        """Не в контракте architecture.md, но нужна вызывающему коду, чтобы
        убрать попап после того, как нажатая кнопка разблокировала ввод —
        без этого метода закрыть `BlockingNotice` снаружи нечем."""
        self.setVisible(False)


__all__ = ["NoticeStrip", "BlockingNotice"]

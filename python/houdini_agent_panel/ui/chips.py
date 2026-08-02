"""Верхняя строка панели и чип режима сессии (docs/architecture.md §10).

`HeaderBar` — чип агента, чип рабочей папки `$HIP`, выбор сессии, «+» и
шестерёнка настроек (docs/design.md, раздел UI, «Верх»). `ModeChip` физически
живёт в `Composer` (см. контракт), но определён здесь вместе с остальными
чипами — двух похожих виджетов в разных файлах быть не должно.

Оба виджета только показывают то, что им передали снаружи (`set_*`), и шлют
сигналы на действия человека — никакой своей логики про сессии/режимы/агентов
здесь нет, это дело `panel.py` и `client.py`.
"""

from __future__ import annotations

from ..sessions import SessionMode, SessionState
from . import theme
from .qt import QtCore, QtGui, QtWidgets, Signal


class HeaderBar(QtWidgets.QWidget):
    """Верх панели: агент · $HIP · сессия · «+» · настройки."""

    agent_clicked = Signal()
    session_selected = Signal(str)
    new_session_clicked = Signal()
    settings_clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(theme.MARGIN, theme.SPACING_TIGHT, theme.MARGIN, theme.SPACING_TIGHT)
        layout.setSpacing(theme.SPACING)

        # Чип агента: иконка+имя, клик — переход на экран выбора агента.
        self._agent_button = QtWidgets.QToolButton(self)
        self._agent_button.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self._agent_button.setAutoRaise(True)
        self._agent_button.clicked.connect(self.agent_clicked)
        layout.addWidget(self._agent_button)

        # Чип рабочей папки — строка без выбора (design.md: выбор папки отложен в v1).
        self._cwd_label = QtWidgets.QLabel(self)
        self._cwd_label.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        layout.addWidget(self._cwd_label)

        layout.addStretch(1)

        self._session_combo = QtWidgets.QComboBox(self)
        self._session_combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)
        # activated (а не currentIndexChanged) — стреляет только по действию
        # человека, не по нашему же programmatic setCurrentIndex в set_sessions.
        self._session_combo.activated.connect(self._on_session_activated)
        layout.addWidget(self._session_combo)

        self._new_session_button = QtWidgets.QToolButton(self)
        self._new_session_button.setText("+")
        self._new_session_button.setAutoRaise(True)
        self._new_session_button.setToolTip("Новый разговор")
        self._new_session_button.clicked.connect(self.new_session_clicked)
        layout.addWidget(self._new_session_button)

        self._settings_button = QtWidgets.QToolButton(self)
        self._settings_button.setText("⚙")
        self._settings_button.setAutoRaise(True)
        self._settings_button.setToolTip("Настройки")
        self._settings_button.clicked.connect(self.settings_clicked)
        layout.addWidget(self._settings_button)

    # --- наполнение снаружи ------------------------------------------------

    def set_agent(self, name: str, icon: QtGui.QIcon | None) -> None:
        self._agent_button.setText(name)
        self._agent_button.setIcon(icon if icon is not None else QtGui.QIcon())

    def set_cwd(self, path: str) -> None:
        self._cwd_label.setText(path)
        self._cwd_label.setToolTip(path)

    def set_sessions(self, states: list[SessionState], current: str | None) -> None:
        """Перестроить список сессий, сохранив текущую выбранной, без лишнего сигнала."""
        self._session_combo.blockSignals(True)
        try:
            self._session_combo.clear()
            for state in states:
                self._session_combo.addItem(state.title, state.session_id)
            if current is not None:
                index = self._session_combo.findData(current)
                if index >= 0:
                    self._session_combo.setCurrentIndex(index)
        finally:
            self._session_combo.blockSignals(False)

    def _on_session_activated(self, index: int) -> None:
        session_id = self._session_combo.itemData(index)
        if session_id:
            self.session_selected.emit(session_id)


class ModeChip(QtWidgets.QWidget):
    """Чип режима сессии — живёт в `Composer`.

    Правило «агент не умеет — контрола нет»: пустой список режимов скрывает
    виджет целиком, а не рисует пустую выпадашку.
    """

    mode_selected = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._combo = QtWidgets.QComboBox(self)
        self._combo.setSizeAdjustPolicy(QtWidgets.QComboBox.AdjustToContents)
        self._combo.activated.connect(self._on_activated)
        layout.addWidget(self._combo)

        self.setVisible(False)

    def set_modes(self, modes: list[SessionMode], current_id: str | None) -> None:
        if not modes:
            self._combo.blockSignals(True)
            self._combo.clear()
            self._combo.blockSignals(False)
            self.setVisible(False)
            return

        self._combo.blockSignals(True)
        try:
            self._combo.clear()
            for mode in modes:
                self._combo.addItem(mode.name, mode.id)
            if current_id is not None:
                index = self._combo.findData(current_id)
                if index >= 0:
                    self._combo.setCurrentIndex(index)
        finally:
            self._combo.blockSignals(False)
        self.setVisible(True)

    def _on_activated(self, index: int) -> None:
        mode_id = self._combo.itemData(index)
        if mode_id:
            self.mode_selected.emit(mode_id)


__all__ = ["HeaderBar", "ModeChip"]

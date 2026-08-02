"""Экран настроек — ровно набор из design.md, ничего сверх него.

Агент по умолчанию, автостарт агента при открытии панели, проверять
обновления, показывать оповещения, телеметрия, папка данных с кнопкой
«Открыть», эндпоинт локального whisper, «Скопировать диагностику».

Читает и пишет `settings.json` напрямую (`settings.load`/`settings.save`) —
тот же однонаправленный слой, что и у `ui/agents.py`: экран настроек по праву
знает про `settings.py`, а не наоборот.
"""

from __future__ import annotations

from .. import paths
from .. import settings as settings_module
from .qt import QtWidgets, Signal


class SettingsView(QtWidgets.QWidget):
    changed = Signal()
    closed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._loading = False

        close_button = QtWidgets.QToolButton()
        close_button.setText("←")
        close_button.setToolTip("Назад")
        close_button.clicked.connect(self.closed.emit)

        header = QtWidgets.QHBoxLayout()
        header.addWidget(close_button)
        header.addWidget(QtWidgets.QLabel("Настройки"))
        header.addStretch(1)

        self._default_agent_combo = QtWidgets.QComboBox()
        self._default_agent_combo.currentIndexChanged.connect(self._on_field_changed)

        self._autostart_checkbox = QtWidgets.QCheckBox("Автостарт агента при открытии панели")
        self._autostart_checkbox.toggled.connect(self._on_field_changed)

        self._check_updates_checkbox = QtWidgets.QCheckBox("Проверять обновления")
        self._check_updates_checkbox.toggled.connect(self._on_field_changed)

        self._show_announcements_checkbox = QtWidgets.QCheckBox("Показывать оповещения")
        self._show_announcements_checkbox.toggled.connect(self._on_field_changed)

        self._telemetry_checkbox = QtWidgets.QCheckBox("Телеметрия (анонимная, по умолчанию выключена)")
        self._telemetry_checkbox.toggled.connect(self._on_field_changed)

        self._whisper_edit = QtWidgets.QLineEdit()
        self._whisper_edit.setPlaceholderText("http://127.0.0.1:9000 (локальный whisper)")
        self._whisper_edit.textChanged.connect(self._on_field_changed)

        self._data_dir_label = QtWidgets.QLabel()
        self._data_dir_label.setWordWrap(True)
        open_data_dir_button = QtWidgets.QPushButton("Открыть")
        open_data_dir_button.clicked.connect(self._on_open_data_dir)

        data_dir_row = QtWidgets.QHBoxLayout()
        data_dir_row.addWidget(self._data_dir_label, 1)
        data_dir_row.addWidget(open_data_dir_button)

        copy_diagnostics_button = QtWidgets.QPushButton("Скопировать диагностику")
        copy_diagnostics_button.clicked.connect(self._on_copy_diagnostics)

        form = QtWidgets.QFormLayout()
        form.addRow("Агент по умолчанию", self._default_agent_combo)
        form.addRow(self._autostart_checkbox)
        form.addRow(self._check_updates_checkbox)
        form.addRow(self._show_announcements_checkbox)
        form.addRow(self._telemetry_checkbox)
        form.addRow("Папка данных", data_dir_row)
        form.addRow("Whisper-эндпоинт", self._whisper_edit)
        form.addRow(copy_diagnostics_button)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(header)
        layout.addLayout(form)
        layout.addStretch(1)

        self.reload()

    # --- публичное -----------------------------------------------------

    def reload(self) -> None:
        """Перечитать `settings.json` с диска и обновить контролы без
        повторной записи (иначе внешнее изменение файла зациклило бы себя
        через `_on_field_changed`)."""
        self._loading = True
        try:
            current = settings_module.load()
            self._populate_default_agent_options(current)
            self._autostart_checkbox.setChecked(current.autostart_agent)
            self._check_updates_checkbox.setChecked(current.check_updates)
            self._show_announcements_checkbox.setChecked(current.show_announcements)
            self._telemetry_checkbox.setChecked(current.telemetry)
            self._whisper_edit.setText(current.whisper_endpoint)
            self._data_dir_label.setText(str(paths.data_dir()))
        finally:
            self._loading = False

    # --- внутреннее ------------------------------------------------------

    def _populate_default_agent_options(self, current: "settings_module.Settings") -> None:
        self._default_agent_combo.blockSignals(True)
        self._default_agent_combo.clear()
        self._default_agent_combo.addItem("—", None)
        agent_ids = sorted(set(current.installed_agents) | {a.id for a in current.custom_agents})
        for agent_id in agent_ids:
            self._default_agent_combo.addItem(agent_id, agent_id)
        index = self._default_agent_combo.findData(current.default_agent)
        self._default_agent_combo.setCurrentIndex(index if index >= 0 else 0)
        self._default_agent_combo.blockSignals(False)

    def _on_field_changed(self, *_args: object) -> None:
        if self._loading:
            return
        current = settings_module.load()
        current.default_agent = self._default_agent_combo.currentData()
        current.autostart_agent = self._autostart_checkbox.isChecked()
        current.check_updates = self._check_updates_checkbox.isChecked()
        current.show_announcements = self._show_announcements_checkbox.isChecked()
        current.telemetry = self._telemetry_checkbox.isChecked()
        current.whisper_endpoint = self._whisper_edit.text().strip()
        settings_module.save(current)
        self.changed.emit()

    def _on_open_data_dir(self) -> None:
        paths.open_in_file_manager(paths.data_dir())

    def _on_copy_diagnostics(self) -> None:
        current = settings_module.load()
        text = settings_module.diagnostics(current)
        clipboard = QtWidgets.QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)


__all__ = ["SettingsView"]

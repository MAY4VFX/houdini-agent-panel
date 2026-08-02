"""Settings screen — the design.md set of fields, plus the agents section.

The "Agents" block (the registry six plus custom agents: install / update /
remove) lives at the top of this screen instead of on its own page. Switching
between already-installed agents now happens from the header chip's dropdown
(`ui/chips.py`), so a whole separate screen dedicated to "which agent to run"
stopped earning its keep — see `AgentPanel._open_agent_management`, which is
just "open settings, scroll to top".

Reads and writes `settings.json` directly (`settings.load`/`settings.save`) —
the same one-way layering as `ui/agents.py`: the settings screen is allowed
to know about `settings.py`, not the other way round.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import paths
from .. import settings as settings_module
from .agents import AgentsView
from .chips import ChoiceButton
from .qt import QtWidgets, Signal

if TYPE_CHECKING:
    from ..network import Fetcher


class SettingsView(QtWidgets.QWidget):
    changed = Signal()
    closed = Signal()

    def __init__(self, parent=None, *, fetch: "Fetcher | None" = None) -> None:
        super().__init__(parent)
        self._loading = False

        close_button = QtWidgets.QToolButton()
        close_button.setText("←")
        close_button.setToolTip("Back")
        close_button.clicked.connect(self.closed.emit)

        header = QtWidgets.QHBoxLayout()
        header.addWidget(close_button)
        header.addWidget(QtWidgets.QLabel("Settings"))
        header.addStretch(1)

        self._agents_view = AgentsView(fetch=fetch)
        # An installed/custom agent list change is exactly the kind of
        # settings change that should make the default-agent combo and the
        # header chip's menu refresh, so it rides the same `changed` signal
        # rather than getting a parallel one panel.py has to wire up too.
        self._agents_view.installed_changed.connect(self._on_agents_changed)

        self._default_agent_combo = ChoiceButton()
        self._default_agent_combo.currentIndexChanged.connect(self._on_field_changed)

        self._autostart_checkbox = QtWidgets.QCheckBox("Autostart agent when the panel opens")
        self._autostart_checkbox.toggled.connect(self._on_field_changed)

        self._check_updates_checkbox = QtWidgets.QCheckBox("Check for updates")
        self._check_updates_checkbox.toggled.connect(self._on_field_changed)

        self._show_announcements_checkbox = QtWidgets.QCheckBox("Show announcements")
        self._show_announcements_checkbox.toggled.connect(self._on_field_changed)

        self._telemetry_checkbox = QtWidgets.QCheckBox(
            "Telemetry (anonymous, off by default)"
        )
        self._telemetry_checkbox.toggled.connect(self._on_field_changed)

        self._whisper_edit = QtWidgets.QLineEdit()
        self._whisper_edit.setPlaceholderText("http://127.0.0.1:9000 (local whisper)")
        self._whisper_edit.textChanged.connect(self._on_field_changed)

        self._data_dir_label = QtWidgets.QLabel()
        self._data_dir_label.setWordWrap(True)
        open_data_dir_button = QtWidgets.QPushButton("Open")
        open_data_dir_button.clicked.connect(self._on_open_data_dir)

        data_dir_row = QtWidgets.QHBoxLayout()
        data_dir_row.addWidget(self._data_dir_label, 1)
        data_dir_row.addWidget(open_data_dir_button)

        copy_diagnostics_button = QtWidgets.QPushButton("Copy diagnostics")
        copy_diagnostics_button.clicked.connect(self._on_copy_diagnostics)

        form = QtWidgets.QFormLayout()
        form.addRow("Default agent", self._default_agent_combo)
        form.addRow(self._autostart_checkbox)
        form.addRow(self._check_updates_checkbox)
        form.addRow(self._show_announcements_checkbox)
        form.addRow(self._telemetry_checkbox)
        form.addRow("Data folder", data_dir_row)
        form.addRow("Whisper endpoint", self._whisper_edit)
        form.addRow(copy_diagnostics_button)

        content = QtWidgets.QWidget()
        content_layout = QtWidgets.QVBoxLayout(content)
        # Agents first, at the top: it's the section that used to be its own
        # screen, and "open settings on the agents block" means nothing more
        # than opening settings scrolled to the top (see `focus_agents`).
        content_layout.addWidget(QtWidgets.QLabel("<b>Agents</b>"))
        content_layout.addWidget(self._agents_view)
        content_layout.addLayout(form)
        content_layout.addStretch(1)

        self._scroll = QtWidgets.QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self._scroll.setWidget(content)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(header)
        layout.addWidget(self._scroll, 1)

        self.reload()

    # --- public -----------------------------------------------------

    def set_agents(self, entries, *, updates=None) -> None:
        """Forwarded to the embedded `AgentsView` — callers (the panel's
        registry refresh) don't need to know the agents block moved in
        here."""
        self._agents_view.set_agents(entries, updates=updates)

    def focus_agents(self) -> None:
        """Scroll to the agents section. It's first, so this is just "top"."""
        self._scroll.verticalScrollBar().setValue(0)

    def reload(self) -> None:
        """Re-read `settings.json` from disk and refresh the controls without
        writing it back (otherwise an external file change would loop back
        through `_on_field_changed`)."""
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

    # --- internal ------------------------------------------------------

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

    def _on_agents_changed(self) -> None:
        # An install/update/remove writes settings.json directly (like
        # AgentsView._on_add_custom always has) — reload rather than merge,
        # same reasoning as `AgentPanel._on_agent_chosen`.
        self.reload()
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

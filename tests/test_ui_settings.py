"""Тесты экрана настроек: набор строго из design.md, чтение/запись settings.json."""

from __future__ import annotations

from houdini_agent_panel import paths
from houdini_agent_panel import settings as settings_module
from houdini_agent_panel.ui.settings_view import SettingsView


def test_reload_reflects_defaults(qapp):
    view = SettingsView()
    assert view._autostart_checkbox.isChecked() is True
    assert view._check_updates_checkbox.isChecked() is True
    assert view._show_announcements_checkbox.isChecked() is True
    assert view._telemetry_checkbox.isChecked() is False
    assert view._whisper_edit.text() == ""
    assert view._data_dir_label.text() == str(paths.data_dir())


def test_toggling_checkbox_persists_and_emits_changed(qapp):
    view = SettingsView()
    received = []
    view.changed.connect(lambda: received.append(True))

    view._telemetry_checkbox.setChecked(True)

    assert received == [True]
    assert settings_module.load().telemetry is True


def test_whisper_endpoint_persists(qapp):
    view = SettingsView()
    view._whisper_edit.setText("http://127.0.0.1:9000")
    assert settings_module.load().whisper_endpoint == "http://127.0.0.1:9000"


def test_default_agent_combo_lists_installed_and_custom(qapp):
    current = settings_module.load()
    current.installed_agents["claude-acp"] = settings_module.InstalledAgent(
        agent_id="claude-acp", version="1.0", kind="npx", installed_at="now"
    )
    current.custom_agents.append(
        settings_module.CustomAgent(id="custom:x", name="X", command="/bin/x")
    )
    settings_module.save(current)

    view = SettingsView()
    options = {view._default_agent_combo.itemData(i) for i in range(view._default_agent_combo.count())}
    assert "claude-acp" in options
    assert "custom:x" in options


def test_settings_has_no_native_combobox(qapp):
    from PySide6 import QtWidgets

    view = SettingsView()
    assert view.findChildren(QtWidgets.QComboBox) == []


def test_default_agent_selection_persists(qapp):
    current = settings_module.load()
    current.installed_agents["claude-acp"] = settings_module.InstalledAgent(
        agent_id="claude-acp", version="1.0", kind="npx", installed_at="now"
    )
    settings_module.save(current)

    view = SettingsView()
    index = view._default_agent_combo.findData("claude-acp")
    view._default_agent_combo.setCurrentIndex(index)

    assert settings_module.load().default_agent == "claude-acp"


def test_open_data_dir_button_calls_paths_helper(qapp, monkeypatch):
    calls = []
    monkeypatch.setattr(paths, "open_in_file_manager", lambda path: calls.append(path))
    view = SettingsView()
    view._on_open_data_dir()
    assert calls == [paths.data_dir()]


def test_copy_diagnostics_sets_clipboard(qapp, monkeypatch):
    monkeypatch.setattr(settings_module, "diagnostics", lambda settings: "DIAG-TEXT")
    view = SettingsView()
    view._on_copy_diagnostics()
    from PySide6 import QtWidgets

    assert QtWidgets.QApplication.clipboard().text() == "DIAG-TEXT"


def test_reload_does_not_resave_settings(qapp, monkeypatch):
    """Перечитывание не должно порождать запись — иначе внешний touch файла
    зациклился бы через `_on_field_changed`."""
    view = SettingsView()
    calls = []
    monkeypatch.setattr(settings_module, "save", lambda s, path=None: calls.append(s))
    view.reload()
    assert calls == []

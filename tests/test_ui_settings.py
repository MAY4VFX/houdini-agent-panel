"""Тесты экрана настроек: набор строго из design.md, чтение/запись settings.json,
плюс встроенный блок «Агенты» (см. ui/agents.py и ui/panel.py)."""

from __future__ import annotations

from houdini_agent_panel import paths
from houdini_agent_panel import settings as settings_module
from houdini_agent_panel.registry import AgentEntry, BinaryDistribution
from houdini_agent_panel.ui.agents import AgentsView
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


# --- embedded agents section --------------------------------------------


def test_agents_section_is_embedded_at_top(qapp):
    """The old standalone «Агенты» screen is gone: its content lives inside
    settings now, as an `AgentsView` instance, first at the top."""
    view = SettingsView()
    assert isinstance(view._agents_view, AgentsView)
    content_layout = view._scroll.widget().layout()
    index = next(
        i for i in range(content_layout.count())
        if content_layout.itemAt(i).widget() is view._agents_view
    )
    assert index == 1  # right after the "Agents" section label


def test_focus_agents_scrolls_to_top(qapp):
    view = SettingsView()
    view._scroll.verticalScrollBar().setValue(50)
    view.focus_agents()
    assert view._scroll.verticalScrollBar().value() == 0


def test_set_agents_forwards_to_embedded_view(qapp, monkeypatch):
    monkeypatch.setattr("houdini_agent_panel.registry.platform_key", lambda: "fake-platform")
    view = SettingsView()
    entry = AgentEntry(
        id="agent-a",
        name="Agent A",
        version="1.0.0",
        binaries={"fake-platform": BinaryDistribution(archive="https://x/a.zip", cmd="./a", sha256="0" * 64)},
    )
    view.set_agents([entry])
    assert view._agents_view._entries == [entry]


def test_installing_agent_refreshes_default_agent_combo_without_recreating_panel(qapp, monkeypatch):
    """«После установки агента из настроек он должен без перезапуска панели
    появиться в меню чипа» — here we check the SettingsView-side half of
    that: the default-agent combo (and `changed`, which the panel listens to
    for the chip menu) update from the same `installed_changed` signal."""
    monkeypatch.setattr("houdini_agent_panel.registry.platform_key", lambda: "fake-platform")
    view = SettingsView()

    changed = []
    view.changed.connect(lambda: changed.append(True))

    current = settings_module.load()
    current.installed_agents["agent-a"] = settings_module.InstalledAgent(
        agent_id="agent-a", version="1.0.0", kind="binary", installed_at="now"
    )
    settings_module.save(current)
    view._agents_view.installed_changed.emit()

    options = {view._default_agent_combo.itemData(i) for i in range(view._default_agent_combo.count())}
    assert "agent-a" in options
    assert changed == [True]

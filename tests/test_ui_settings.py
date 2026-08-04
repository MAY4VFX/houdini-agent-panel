"""Settings screen tests: the field set straight from design.md, reading and
writing settings.json, plus the embedded "Agents" block (see ui/agents.py and
ui/panel.py)."""

from __future__ import annotations

from houdini_agent_panel import paths
from houdini_agent_panel import settings as settings_module
from houdini_agent_panel.registry import AgentEntry, BinaryDistribution
from houdini_agent_panel.ui.agents import AgentsView
from houdini_agent_panel.ui.qt import QtCore
from houdini_agent_panel.ui.settings_view import SettingsView


def test_reload_reflects_defaults(qapp):
    view = SettingsView()
    assert view._autostart_checkbox.isChecked() is True
    assert view._check_updates_checkbox.isChecked() is True
    assert view._show_announcements_checkbox.isChecked() is True
    assert view._telemetry_checkbox.isChecked() is False
    assert view._whisper_edit.text() == ""
    assert view._proxy_edit.text() == ""
    assert view._no_proxy_edit.text() == ""
    assert view._ca_bundle_edit.text() == ""
    assert view._restart_banner.isVisible() is False
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


# --- Network section (issue #26) ------------------------------------------


def test_network_section_starts_collapsed(qapp):
    """Same rank as Privacy/Data — a studio with no proxy never needs it
    open by default."""
    view = SettingsView()
    assert view._network_section._toggle.isChecked() is False


def test_network_caption_states_the_socks_limit(qapp):
    """The owner asked what proxy TYPE is supported. Answer lives in the
    same caption as the other honest facts, not a new label — a real
    warning from the owner's own studio notes backs this: "НЕ ставить
    ALL_PROXY=socks5 — ломает urllib OAuth."."""
    view = SettingsView()
    text = view._network_caption.text()
    assert "HTTP" in text
    assert "SOCKS" in text and "not supported" in text


def test_network_fields_persist(qapp):
    view = SettingsView()
    view._proxy_edit.setText("http://proxy.studio.local:8080")
    view._no_proxy_edit.setText("render01.internal")
    view._ca_bundle_edit.setText("/etc/ssl/studio-ca.pem")

    saved = settings_module.load()
    assert saved.proxy_url == "http://proxy.studio.local:8080"
    assert saved.no_proxy == "render01.internal"
    assert saved.ca_bundle == "/etc/ssl/studio-ca.pem"


def test_network_field_change_emits_both_changed_and_proxy_changed(qapp):
    view = SettingsView()
    changed = []
    proxy_changed = []
    view.changed.connect(lambda: changed.append(True))
    view.proxy_changed.connect(lambda: proxy_changed.append(True))

    view._proxy_edit.setText("http://proxy.studio.local:8080")

    assert changed == [True]
    assert proxy_changed == [True]


def test_ordinary_checkbox_change_does_not_emit_proxy_changed(qapp):
    """`proxy_changed` is scoped to the three Network fields — "not on
    every checkbox" (issue #26)."""
    view = SettingsView()
    proxy_changed = []
    view.proxy_changed.connect(lambda: proxy_changed.append(True))

    view._telemetry_checkbox.setChecked(True)
    view._whisper_edit.setText("http://127.0.0.1:9000")

    assert proxy_changed == []


def _expand_network_section(view: SettingsView) -> None:
    """Network is collapsed by default (`test_network_section_starts_
    collapsed`) — `isVisible()` only reflects reality once both the window
    is realised AND the section's own body is expanded (same requirement
    `test_settings_grid_alignment.py::_build` has for every section)."""
    view.show()
    view._network_section._toggle.setChecked(True)
    view._network_section._on_toggled(True)


def test_editing_a_network_field_shows_the_restart_banner(qapp):
    view = SettingsView()
    _expand_network_section(view)
    qapp.processEvents()
    assert view._restart_banner.isVisible() is False

    view._no_proxy_edit.setText("render01.internal")

    assert view._restart_banner.isVisible() is True


def test_restart_button_hides_banner_and_requests_a_restart(qapp):
    view = SettingsView()
    _expand_network_section(view)
    qapp.processEvents()
    view._proxy_edit.setText("http://proxy.studio.local:8080")
    assert view._restart_banner.isVisible() is True

    requested = []
    view.restart_agent_requested.connect(lambda: requested.append(True))
    view._restart_button.click()

    assert requested == [True]
    assert view._restart_banner.isVisible() is False


def test_reload_hides_the_restart_banner(qapp):
    """A reload is a fresh read from disk, not an edit THIS screen made —
    the restart invitation belongs only to the latter."""
    view = SettingsView()
    _expand_network_section(view)
    qapp.processEvents()
    view._proxy_edit.setText("http://proxy.studio.local:8080")
    assert view._restart_banner.isVisible() is True

    view.reload()

    assert view._restart_banner.isVisible() is False


def test_browse_ca_bundle_fills_the_field(qapp, monkeypatch):
    from houdini_agent_panel.ui.qt import QtWidgets

    monkeypatch.setattr(
        QtWidgets.QFileDialog, "getOpenFileName", lambda *a, **k: ("/etc/ssl/studio-ca.pem", "")
    )
    view = SettingsView()

    view._browse_ca_bundle_button.click()

    assert view._ca_bundle_edit.text() == "/etc/ssl/studio-ca.pem"
    assert settings_module.load().ca_bundle == "/etc/ssl/studio-ca.pem"


def test_browse_ca_bundle_cancelled_changes_nothing(qapp, monkeypatch):
    from houdini_agent_panel.ui.qt import QtWidgets

    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileName", lambda *a, **k: ("", ""))
    view = SettingsView()

    view._browse_ca_bundle_button.click()

    assert view._ca_bundle_edit.text() == ""
    assert view._restart_banner.isVisible() is False


def test_collapsing_network_with_a_pending_restart_dismisses_it(qapp):
    """Collapsing the section while a restart is pending is an explicit
    dismiss, not a silent loss — re-expanding must land on a defined
    state (still dismissed), not "however it happens to render" (`_on_
    network_section_toggled`)."""
    view = SettingsView()
    view.show()
    view._network_section._toggle.setChecked(True)
    view._network_section._on_toggled(True)
    qapp.processEvents()

    view._proxy_edit.setText("http://proxy.studio.local:8080")
    assert view._restart_pending is True
    assert view._restart_banner.isVisible() is True

    # A real click — fires BOTH `.clicked` (`_Section._on_toggled`, the
    # body's own visibility) and `.toggled` (`_on_network_section_toggled`,
    # the dismiss) exactly like the artist's mouse would.
    view._network_section._toggle.click()
    qapp.processEvents()

    assert view._restart_pending is False

    view._network_section._toggle.click()  # back open
    qapp.processEvents()

    assert view._restart_banner.isVisible() is False, (
        "a dismissed restart must not silently come back on re-expand"
    )


def test_collapsing_network_with_no_pending_restart_does_nothing_odd(qapp):
    """Collapsing an untouched (or already-dismissed) Network section is
    just... collapsing it — no flag to flip, nothing to dismiss."""
    view = SettingsView()
    view.show()
    view._network_section._toggle.setChecked(True)
    view._network_section._on_toggled(True)
    qapp.processEvents()

    view._network_section._toggle.click()
    qapp.processEvents()

    assert view._restart_pending is False
    assert view._restart_banner.isVisible() is False


def test_settings_has_no_native_combobox(qapp):
    from PySide6 import QtWidgets

    view = SettingsView()
    assert view.findChildren(QtWidgets.QComboBox) == []


def test_no_default_agent_control_on_the_settings_screen(qapp):
    """Removed per the owner's call, seen live: "непонятно, какая модель
    дефолта выбрана, в меню этого не нужно" — a second control for a fact
    the header chip's own menu already decides. `settings.default_agent`
    itself is untouched (`test_saving_other_fields_does_not_touch_default_
    agent` below) — only this screen's control is gone. "Behaviour" now
    has exactly one row: the autostart checkbox, occupying row 0."""
    view = SettingsView()
    assert not hasattr(view, "_default_agent_combo")
    assert view._behaviour_section.widget_at(0, 0) is view._autostart_checkbox


def test_saving_other_fields_does_not_touch_default_agent(qapp):
    """`default_agent` is set exactly one way now: picking an agent from
    the header chip (`AgentPanel._on_agent_chosen`). Editing anything on
    this screen must leave it exactly as it was on disk."""
    current = settings_module.load()
    current.default_agent = "claude-acp"
    settings_module.save(current)

    view = SettingsView()
    view._whisper_edit.setText("http://127.0.0.1:9000")
    view._autostart_checkbox.setChecked(False)

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
    """A reload must not cause a write — otherwise an external touch of the
    file would loop back through `_on_field_changed`."""
    view = SettingsView()
    calls = []
    monkeypatch.setattr(settings_module, "save", lambda s, path=None: calls.append(s))
    view.reload()
    assert calls == []


# --- embedded agents section --------------------------------------------


def test_agents_section_is_embedded_at_top(qapp):
    """The old standalone "Agents" screen is gone: its content lives inside
    settings now, as an `AgentsView` instance, inside the first (topmost)
    collapsible section."""
    view = SettingsView()
    assert isinstance(view._agents_view, AgentsView)
    rail_layout = view._rail.layout()
    first_section = rail_layout.itemAt(0).widget()
    assert view._agents_view in first_section.findChildren(AgentsView)


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


def test_installing_agent_refreshes_the_panel_without_recreating_it(qapp, monkeypatch):
    """"Install an agent from settings and it shows up in the chip menu with
    no panel restart" — here we check the SettingsView-side half of that:
    `changed` (which the panel listens to, to rebuild the header chip's
    menu) fires from the same `installed_changed` signal `AgentsView`
    emits after an install."""
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

    assert changed == [True]


def test_back_button_lines_up_with_the_settings_rail(qapp):
    """Full width put the back arrow at the panel's own edge, where an open
    conversation drawer covered it — and it is the only way out of settings."""
    view = SettingsView()
    view.resize(1000, 700)
    view.show()
    qapp.processEvents()

    header_left = view._header_rail.mapTo(view, QtCore.QPoint(0, 0)).x()
    rail_left = view._rail.mapTo(view, QtCore.QPoint(0, 0)).x()

    assert view._header_rail.width() == view._rail.width()
    assert abs(header_left - rail_left) <= 1


def test_settings_screen_does_not_pin_the_panel_wide(qapp):
    view = SettingsView()
    assert view.minimumSizeHint().width() <= 200

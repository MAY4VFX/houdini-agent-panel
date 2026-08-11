"""Settings screen tests: the field set straight from design.md, reading and
writing settings.json, plus the embedded "Agents" block (see ui/agents.py and
ui/panel.py)."""

from __future__ import annotations

from houdini_agent_panel import paths
from houdini_agent_panel import settings as settings_module
from houdini_agent_panel import updates as updates_module
from houdini_agent_panel.registry import AgentEntry, BinaryDistribution
from houdini_agent_panel.ui.agents import AgentsView
from houdini_agent_panel.ui import settings_view as settings_view_mod
from houdini_agent_panel.ui.qt import QtCore, QtWidgets
from houdini_agent_panel.ui.settings_view import SettingsView, _ROW_LABELS


def test_reload_reflects_defaults(qapp):
    view = SettingsView()
    assert view._autostart_checkbox.isChecked() is True
    assert view._check_updates_checkbox.isChecked() is True
    assert view._show_announcements_checkbox.isChecked() is True
    assert view._telemetry_checkbox.isChecked() is False
    assert view._claude_host_mcp_checkbox.isChecked() is True
    assert view._claude_host_skills_checkbox.isChecked() is True
    assert view._whisper_edit.text() == ""
    assert view._proxy_edit.text() == ""
    assert view._no_proxy_edit.text() == ""
    assert view._ca_bundle_edit.text() == ""
    assert view._restart_banner.isVisible() is False
    assert view._data_dir_label.text() == str(paths.data_dir())


def test_grid_measuring_labels_never_render_over_the_page(qapp):
    """Regression, reported live with a screenshot: `_GridMetrics.measure`
    builds five throwaway `QLabel`s (`_ROW_LABELS`) purely to measure the
    widest one's `sizeHint()`. They must be parented (a parentless QWidget
    is a real native top-level window on macOS — the original defect this
    codebase already fixed once, in `chips.py`/`transcript.py`), but
    parenting them turned them into ordinary VISIBLE children of the view
    under construction, with no layout to place them — all five stacked at
    (0, 0), each painted over the last, until `deleteLater()`'s deferred
    cleanup finally ran. The owner's screenshot showed exactly that:
    illegible overlapping label text laid over the back button and title.
    `SettingsView()` here is a fresh, un-eventlooped construction — the
    worst case for that race, with no `processEvents()` call given a
    chance to run the deferred deletion first — so a stray visible probe
    would still be a live, visible child of the view at this exact point,
    same as it was for the artist's own first paint.
    """
    view = SettingsView()
    # `isVisible()` follows the WHOLE ancestor chain — a child reports
    # `False` whenever its top level was never shown, regardless of its
    # own state. Without this, the assertion below would pass whether or
    # not the bug was present, since `view` itself starts unshown.
    view.show()
    # `_ROW_LABELS` text is not unique to the probes — "Whisper endpoint"
    # is also the real, legitimately-visible row label in the Voice
    # section. What's unique to a probe is being a DIRECT child of `view`
    # itself: every real row label lives several layers down (section ->
    # its body -> the row), never straight off the top-level view, since
    # `_GridMetrics.measure(self)` is called with the view itself as the
    # probes' parent.
    probes = [
        child
        for child in view.findChildren(QtWidgets.QLabel)
        if child.parentWidget() is view and child.text() in _ROW_LABELS
    ]
    assert probes, "the probes this regression test targets weren't found at all — has measure() changed?"
    assert [p for p in probes if p.isVisible()] == []


def test_toggling_checkbox_persists_and_emits_changed(qapp):
    view = SettingsView()
    received = []
    view.changed.connect(lambda: received.append(True))

    view._telemetry_checkbox.setChecked(True)

    assert received == [True]
    assert settings_module.load().telemetry is True


def test_claude_host_visibility_checkboxes_persist_and_survive_reload(qapp):
    view = SettingsView()

    view._claude_host_mcp_checkbox.setChecked(False)
    view._claude_host_skills_checkbox.setChecked(False)

    current = settings_module.load()
    assert current.claude_show_host_mcp_servers is False
    assert current.claude_show_host_skills is False

    reloaded = SettingsView()
    assert reloaded._claude_host_mcp_checkbox.isChecked() is False
    assert reloaded._claude_host_skills_checkbox.isChecked() is False


def test_whisper_endpoint_persists(qapp):
    view = SettingsView()
    view._whisper_edit.setText("http://127.0.0.1:9000")
    assert settings_module.load().whisper_endpoint == "http://127.0.0.1:9000"


def test_whisper_api_key_field_defaults_to_empty(qapp):
    view = SettingsView()
    assert view._whisper_api_key_edit.text() == ""


def test_whisper_api_key_field_is_masked(qapp):
    """A secret typed here must not be readable off the screen — same
    expectation the project already states in words for the proxy URL's
    own password ("A password typed into the proxy URL is written to
    settings.json as plain text"), but here there's an actual masked field
    to hold it to."""
    view = SettingsView()
    assert view._whisper_api_key_edit.echoMode() == QtWidgets.QLineEdit.Password


def test_whisper_api_key_persists(qapp):
    view = SettingsView()
    view._whisper_api_key_edit.setText("sk-whisper-secret")
    assert settings_module.load().whisper_api_key == "sk-whisper-secret"


def test_whisper_api_key_reload_reflects_disk(qapp):
    current = settings_module.load()
    current.whisper_api_key = "sk-whisper-secret"
    settings_module.save(current)

    view = SettingsView()
    view.reload()

    assert view._whisper_api_key_edit.text() == "sk-whisper-secret"


# --- Network section (issue #26) ------------------------------------------


def test_network_section_starts_collapsed(qapp):
    """Same rank as Privacy/Data — a studio with no proxy never needs it
    open by default."""
    view = SettingsView()
    assert view._network_section._toggle.isChecked() is False


def test_network_caption_states_the_socks_limit(qapp):
    """The owner asked what proxy TYPE is supported. Answer lives in the
    same caption as the other honest facts, not a new label — a real
    warning from the owner's own studio notes backs this: "DO NOT set
    ALL_PROXY=socks5 — breaks urllib OAuth."."""
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
    """Removed per the owner's call, seen live: "not clear which one is
    the default agent — don't need that in the menu" — a second control
    for a fact the header chip's own menu already decides.
    `settings.default_agent` itself is untouched
    (`test_saving_other_fields_does_not_touch_default_
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


def test_header_rail_lines_up_with_the_settings_rail(qapp):
    """`_header_rail` (now just the centred "Settings" title — the back
    button it used to hold is gone, Settings is an overlay you close via
    the "…" toggle/Escape/an agent switch instead of navigating out of)
    must still share the content rail's own width and left edge below it.
    Full width put the title at the panel's own edge, where an open
    conversation drawer covered it; the underlying alignment mismatch this
    guards is the same one `test_nothing_starts_left_of_the_back_button`
    (test_settings_grid_alignment.py) catches in the scrollbar case."""
    view = SettingsView()
    view.resize(1000, 700)
    view.show()
    qapp.processEvents()

    header_left = view._header_rail.mapTo(view, QtCore.QPoint(0, 0)).x()
    rail_left = view._rail.mapTo(view, QtCore.QPoint(0, 0)).x()

    assert view._header_rail.width() == view._rail.width()
    assert abs(header_left - rail_left) <= 1


def test_overlay_background_does_not_replace_child_control_palette(qapp):
    """Qt5 cascaded the old parent background QSS into every child role."""
    from houdini_agent_panel.ui.qt import QtGui

    palette = QtGui.QPalette()
    palette.setColor(QtGui.QPalette.Window, QtGui.QColor("#3a3a3a"))
    palette.setColor(QtGui.QPalette.Text, QtGui.QColor("#cccccc"))
    palette.setColor(QtGui.QPalette.Base, QtGui.QColor("#000000"))
    palette.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor("#989898"))
    qapp.setPalette(palette)

    view = SettingsView()

    assert view.palette().color(QtGui.QPalette.Window) == QtGui.QColor("#454545")
    assert view._whisper_edit.palette().color(QtGui.QPalette.Base) == QtGui.QColor("#000000")


def test_settings_screen_does_not_pin_the_panel_wide(qapp):
    view = SettingsView()
    assert view.minimumSizeHint().width() <= 200


# --- version + "Check now" -------------------------------------------------
#
# The owner's own report: the panel sat on 0.8.5 with 0.8.8 already on PyPI,
# autoupdate never caught it, and there was no way to even see the running
# version from inside Houdini without an ssh session onto the deps tree.


def _wait_until(condition, *, timeout_ms: int = 5000) -> None:
    from PySide6 import QtTest

    app = QtWidgets.QApplication.instance()
    elapsed = 0
    step = 20
    while not condition() and elapsed < timeout_ms:
        app.processEvents()
        QtTest.QTest.qWait(step)
        elapsed += step
    assert condition(), "condition did not become true in time"


def test_version_row_shows_the_running_version_with_no_action_needed(qapp, monkeypatch):
    """Item 1: always visible, before a single click — this is the whole
    point, the owner could not tell the version without ssh."""
    monkeypatch.setattr(updates_module, "_current_panel_version", lambda: "0.8.5")
    monkeypatch.setattr(updates_module, "_current_fx_version", lambda: "2.10.0")

    view = SettingsView()

    assert view._panel_version_label.text() == "houdini-agent-panel 0.8.5"
    assert view._fx_version_label.text() == "fxhoudinimcp 2.10.0"
    assert view._panel_update_button.isVisible() is False


def test_version_row_survives_an_unreadable_fx_version(qapp, monkeypatch):
    """`_current_fx_version()` returns `None` when fxhoudinimcp isn't
    importable (outside a real Houdini plugin process) — the row must say
    so plainly, not show a blank or crash."""
    monkeypatch.setattr(updates_module, "_current_panel_version", lambda: "0.8.5")
    monkeypatch.setattr(updates_module, "_current_fx_version", lambda: None)

    view = SettingsView()

    assert view._fx_version_label.text() == "fxhoudinimcp — not detected"


def test_check_now_sets_the_checking_state_synchronously(qapp, fetcher, monkeypatch):
    """The click handler sets "checking…" and disables the button on the
    calling thread, before the worker thread has had any chance to run —
    silence between the click and a result is exactly what this whole
    feature exists to end."""
    monkeypatch.setattr(updates_module, "_current_panel_version", lambda: "0.8.5")
    monkeypatch.setattr(updates_module, "_current_fx_version", lambda: "2.10.0")
    view = SettingsView(fetch=fetcher)

    view._check_updates_now_button.click()

    assert "checking…" in view._panel_version_label.text()
    assert "checking…" in view._fx_version_label.text()
    assert view._check_updates_now_button.isEnabled() is False
    _wait_until(lambda: view._check_now_worker is None)


def test_check_now_ignores_the_check_for_updates_checkbox(qapp, fetcher, monkeypatch):
    """Item 2: "Check now" must work regardless of the auto-check toggle —
    a manual check the artist just asked for is not gated on a setting
    they may not even know exists."""
    monkeypatch.setattr(updates_module, "_current_panel_version", lambda: "0.8.5")
    monkeypatch.setattr(updates_module, "_current_fx_version", lambda: "2.10.0")
    fetcher.add_json(updates_module.PYPI_URL.format(name="houdini-agent-panel"), {"info": {"version": "0.8.5"}})
    fetcher.add_json(updates_module.PYPI_URL.format(name="fxhoudinimcp"), {"info": {"version": "2.10.0"}})
    view = SettingsView(fetch=fetcher)
    view._check_updates_checkbox.setChecked(False)
    assert settings_module.load().check_updates is False

    view._check_updates_now_button.click()
    _wait_until(lambda: view._check_now_worker is None)

    assert fetcher.calls  # the network was actually reached, unlike updates.check() with the toggle off


def test_check_now_reports_up_to_date(qapp, fetcher, monkeypatch):
    monkeypatch.setattr(updates_module, "_current_panel_version", lambda: "0.8.5")
    monkeypatch.setattr(updates_module, "_current_fx_version", lambda: "2.10.0")
    fetcher.add_json(updates_module.PYPI_URL.format(name="houdini-agent-panel"), {"info": {"version": "0.8.5"}})
    fetcher.add_json(updates_module.PYPI_URL.format(name="fxhoudinimcp"), {"info": {"version": "2.10.0"}})
    view = SettingsView(fetch=fetcher)

    view._check_updates_now_button.click()
    _wait_until(lambda: view._check_now_worker is None)

    assert view._panel_version_label.text() == "houdini-agent-panel 0.8.5 — up to date"
    assert view._fx_version_label.text() == "fxhoudinimcp 2.10.0 — up to date"
    assert view._panel_update_button.isVisible() is False
    assert view._fx_update_button.isVisible() is False
    assert view._check_updates_now_button.isEnabled() is True


def test_check_now_finds_a_panel_update_and_the_update_button_wires_through(qapp, fetcher, monkeypatch):
    """Item 4: an update found by "Check now" has a real path to install
    it — the same `SelfUpdateWorker` mechanism the notice strip's own
    "Update" button already drives (`AgentPanel._start_update`), reached
    here through `panel_update_requested`."""
    monkeypatch.setattr(updates_module, "_current_panel_version", lambda: "0.8.5")
    monkeypatch.setattr(updates_module, "_current_fx_version", lambda: "2.10.0")
    fetcher.add_json(updates_module.PYPI_URL.format(name="houdini-agent-panel"), {"info": {"version": "0.8.9"}})
    fetcher.add_json(updates_module.PYPI_URL.format(name="fxhoudinimcp"), {"info": {"version": "2.10.0"}})
    view = SettingsView(fetch=fetcher)
    view.show()  # isVisible() below follows the whole ancestor chain — see test_grid_measuring_labels_never_render_over_the_page
    requested = []
    view.panel_update_requested.connect(requested.append)

    view._check_updates_now_button.click()
    _wait_until(lambda: view._check_now_worker is None)

    assert view._panel_version_label.text() == "houdini-agent-panel 0.8.5 — update available: 0.8.9"
    assert view._panel_update_button.isVisible() is True
    assert view._fx_update_button.isVisible() is False

    view._panel_update_button.click()

    assert len(requested) == 1
    update = requested[0]
    assert update.kind == "panel"
    assert update.target == "houdini-agent-panel"
    assert update.latest == "0.8.9"
    assert update.current == "0.8.5"


def test_check_now_finds_an_fx_update(qapp, fetcher, monkeypatch):
    monkeypatch.setattr(updates_module, "_current_panel_version", lambda: "0.8.5")
    monkeypatch.setattr(updates_module, "_current_fx_version", lambda: "2.10.0")
    fetcher.add_json(updates_module.PYPI_URL.format(name="houdini-agent-panel"), {"info": {"version": "0.8.5"}})
    fetcher.add_json(updates_module.PYPI_URL.format(name="fxhoudinimcp"), {"info": {"version": "2.11.0"}})
    view = SettingsView(fetch=fetcher)
    view.show()
    requested = []
    view.panel_update_requested.connect(requested.append)

    view._check_updates_now_button.click()
    _wait_until(lambda: view._check_now_worker is None)

    assert view._fx_version_label.text() == "fxhoudinimcp 2.10.0 — update available: 2.11.0"
    assert view._fx_update_button.isVisible() is True

    view._fx_update_button.click()

    assert len(requested) == 1
    assert requested[0].kind == "fx"
    assert requested[0].target == "fxhoudinimcp"


def test_check_now_reports_a_network_failure_plainly(qapp, monkeypatch):
    """Item 3: a check that could not reach PyPI must say so, not sit
    silent or claim "up to date" — the exact silence that cost the owner
    three versions of not knowing."""
    monkeypatch.setattr(updates_module, "_current_panel_version", lambda: "0.8.5")
    monkeypatch.setattr(updates_module, "_current_fx_version", lambda: "2.10.0")
    # No fetcher fixture registered for either URL — FakeFetcher raises NetworkError.
    from tests.conftest import FakeFetcher

    view = SettingsView(fetch=FakeFetcher())

    view._check_updates_now_button.click()
    _wait_until(lambda: view._check_now_worker is None)

    assert "check failed" in view._panel_version_label.text().lower()
    assert "check failed" in view._fx_version_label.text().lower()
    assert view._check_updates_now_button.isEnabled() is True


def test_a_second_click_while_checking_is_a_noop(qapp, fetcher, monkeypatch):
    monkeypatch.setattr(updates_module, "_current_panel_version", lambda: "0.8.5")
    monkeypatch.setattr(updates_module, "_current_fx_version", lambda: "2.10.0")
    fetcher.add_json(updates_module.PYPI_URL.format(name="houdini-agent-panel"), {"info": {"version": "0.8.5"}})
    fetcher.add_json(updates_module.PYPI_URL.format(name="fxhoudinimcp"), {"info": {"version": "2.10.0"}})
    view = SettingsView(fetch=fetcher)

    view._check_updates_now_button.click()
    first_worker = view._check_now_worker
    view._check_updates_now_button.click()  # while the button is already disabled

    assert view._check_now_worker is first_worker
    _wait_until(lambda: view._check_now_worker is None)


# --- Voice section: currently hidden unconditionally, on every platform ---
#
# `recording_available()` (`ui/voice.py`) is computed once, at construction —
# the same check `VoiceButton` uses for the composer's mic button, so the two
# can never disagree. It's currently pinned off everywhere by
# `ui/voice.py::_VOICE_INPUT_AVAILABLE` (macOS is the only platform actually
# measured; Linux/Windows were never tried, so showing voice input there was
# a guess, not a verification — the owner's own call). design.md's own rule
# ("the agent doesn't support it — the control doesn't get drawn") applies
# to an unverified-platform reason exactly as it does to an agent one.
#
# `SettingsView` only calls `recording_available()` — it has no idea WHY the
# answer is what it is, so these tests drive that function directly rather
# than reaching into `ui/voice.py`'s own flag.


def test_voice_section_is_hidden_by_default_right_now(qapp):
    """No monkeypatching — this is the real, current, unconditional
    default: `recording_available()` says no on every platform."""
    view = SettingsView()
    view.show()  # isVisible() follows the ancestor chain — see the probe test above.

    assert view._voice_section.isVisible() is False


def test_voice_section_hidden_when_recording_is_unavailable(qapp, monkeypatch):
    monkeypatch.setattr(
        settings_view_mod, "recording_available", lambda: (False, "no entitlement")
    )
    view = SettingsView()
    view.show()

    assert view._voice_section.isVisible() is False


def test_nothing_stands_in_for_the_hidden_voice_section(qapp):
    """Hidden means gone, not replaced by a note about being hidden.

    A line saying "this is temporarily off" is still something to read
    and wonder about, for a feature the panel does not offer at all right
    now — the owner asked for it removed after seeing one. Settings shows
    no trace of voice until `_VOICE_INPUT_AVAILABLE` says otherwise.
    """
    view = SettingsView()
    view.show()

    for label in view.findChildren(QtWidgets.QLabel):
        if label.isVisible():
            assert "voice" not in label.text().lower(), label.text()
            assert "whisper" not in label.text().lower(), label.text()


def test_voice_section_shown_when_recording_is_available(qapp, monkeypatch):
    """Not the current default (see the "hidden by default" test above) —
    this only proves `SettingsView` still honours a `True` answer, i.e. the
    section isn't hardcoded hidden, just gated on `recording_available()`."""
    monkeypatch.setattr(settings_view_mod, "recording_available", lambda: (True, ""))
    view = SettingsView()
    view.show()

    assert view._voice_section.isVisible() is True

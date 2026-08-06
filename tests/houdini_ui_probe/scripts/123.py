"""Capture the panel's real in-Houdini palette and layout, then exit.

This is loaded by Houdini through ``HOUDINI_PATH``.  It is deliberately a
real GUI probe rather than a hython test: hython does not install Houdini's
application style/palette, which is the subject under test here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

# The launcher must provide a throwaway directory.  Refuse to import the
# package if it did not: accidentally using the owner's settings may autostart
# a real agent on their account.
if not os.environ.get("HAP_DATA_DIR", "").startswith("/tmp/hap-ui-probe."):
    raise RuntimeError("HAP_DATA_DIR must be a /tmp/hap-ui-probe.* directory")

from hutil.PySide import QtCore, QtGui, QtWidgets

from houdini_agent_panel.sessions import SessionMode
from houdini_agent_panel.transcript_model import TranscriptModel
from houdini_agent_panel.ui.chips import HeaderBar
from houdini_agent_panel.ui.composer import Composer
from houdini_agent_panel.ui.settings_view import SettingsView
from houdini_agent_panel.ui.transcript import TranscriptView
from houdini_agent_panel.registry import AgentEntry, NpxDistribution
from houdini_agent_panel import paths


def _rgb(color):
    return color.name(QtGui.QColor.HexRgb)


def _point(widget, root):
    point = widget.mapTo(root, QtCore.QPoint(0, 0))
    return [point.x(), point.y(), widget.width(), widget.height()]


def _luma(color):
    return 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()


def _surface_ratio(color, window, text):
    """Position of a surface on the host's Window -> Text contrast axis."""
    span = _luma(text) - _luma(window)
    return (_luma(color) - _luma(window)) / span if abs(span) > 1 else 0.0


def _capture():
    app = QtWidgets.QApplication.instance()
    palette = app.palette()

    root = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(root)
    header = HeaderBar()
    header.set_agent("OpenCode", None)
    header.set_agent_menu(
        [("codex-acp", "Codex"), ("opencode", "OpenCode")], "opencode"
    )
    header.set_cwd("/Users/may/BS/ship")
    settings = SettingsView()
    entries = []
    for index, name in enumerate(
        ("Claude Agent", "Codex", "Grok Build", "OpenCode", "Gemini CLI", "Kimi CLI")
    ):
        agent_id = "probe-" + str(index)
        manifest = paths.agent_dir(agent_id) / "manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps({"agent_id": agent_id, "version": "1.2.3", "kind": "npx"}),
            encoding="utf-8",
        )
        entries.append(
            AgentEntry(
                id=agent_id,
                name=name,
                version="1.2.3",
                npx=NpxDistribution(package="probe-agent", args=[]),
            )
        )
    settings.set_agents(entries)
    composer = Composer()
    layout.addWidget(header)
    layout.addWidget(settings, 1)
    layout.addWidget(composer)
    root.resize(820, 640)
    root.show()
    app.processEvents()
    app.processEvents()

    output_dir = Path(os.environ["HAP_DATA_DIR"])
    image = root.grab().toImage()
    image.save(str(output_dir / "probe.png"))
    composer.begin_boot("OpenCode")
    app.processEvents()
    boot_image = root.grab().toImage()
    boot_image.save(str(output_dir / "probe-boot.png"))

    # The exact live-panel state from the H20.5/H21 report: system notes in
    # the transcript plus agent/mode/model labels in the two control rails.
    state_root = QtWidgets.QWidget()
    state_layout = QtWidgets.QVBoxLayout(state_root)
    state_header = HeaderBar()
    state_header.set_cwd("/Users/may")
    transcript = TranscriptView()
    transcript_model = TranscriptModel()
    note = transcript_model.append_error(
        "Codex: no system Node found, fetching the portable one — "
        "first launch may take a minute…"
    )
    transcript_model.append_error("Codex 1.1.9 · /Users/may")
    transcript.set_model(transcript_model)
    state_composer = Composer()
    state_layout.addWidget(state_header)
    state_layout.addWidget(transcript, 1)
    state_layout.addWidget(state_composer)
    state_root.resize(968, 700)
    state_root.show()
    app.processEvents()
    app.processEvents()
    # ACP data arrives after the pane is already visible. Houdini 21 used to
    # retain the empty controls' 36–38px hints here, although a preview that
    # populated everything before show looked correct.
    state_header.set_agent("Codex", None)
    state_header.set_agent_menu(
        [("codex-acp", "Codex"), ("opencode", "OpenCode")], "codex-acp"
    )
    state_composer.set_modes(
        [SessionMode("agent", "Agent"), SessionMode("plan", "Plan")], "agent"
    )
    state_composer.set_config_options(
        [
            SimpleNamespace(
                id="model",
                name="Model",
                current_value="gpt-5.6",
                category="model_config",
                choices=(
                    SimpleNamespace(value="gpt-5.6", name="GPT-5.6-Sol", description=""),
                    SimpleNamespace(value="gpt-5.5", name="GPT-5.5", description=""),
                ),
            ),
            SimpleNamespace(
                id="effort",
                name="Effort",
                current_value="medium",
                category="thought_level",
                choices=(
                    SimpleNamespace(value="medium", name="Medium", description=""),
                    SimpleNamespace(value="high", name="High", description=""),
                ),
            ),
        ]
    )
    app.processEvents()
    app.processEvents()
    state_image = state_root.grab().toImage()
    state_image.save(str(output_dir / "probe-panel-state.png"))

    installed_panel = None
    installed_panel_image = None
    installed_note_prose = None
    if os.environ.get("HAP_USE_INSTALLED_PANEL") == "1":
        import hou

        from houdini_agent_panel import settings as settings_mod
        from houdini_agent_panel.ui import panel as panel_mod

        settings_mod.save(
            settings_mod.Settings(
                default_agent="codex-acp",
                autostart_agent=False,
                telemetry_consent_asked=True,
            )
        )
        panel_mod._RefreshWorker.start = lambda self: None
        panel_mod._OrphanSweepWorker.start = lambda self: None
        # Exercise the real host hierarchy. A standalone AgentPanel looked
        # correct while Houdini's Python Pane Tab compressed the exact same
        # controls, so the old probe could green-light the user's bug.
        desktop = hou.ui.curDesktop()
        anchor = next(iter(desktop.paneTabs()))
        pane_tab = anchor.pane().createTab(hou.paneTabType.PythonPanel)
        pane_tab.setActiveInterface(hou.pypanel.interfaceByName("hap::agent"))
        app.processEvents()
        app.processEvents()
        installed_panel = pane_tab.activeInterfaceRootWidget()
        if installed_panel is None:
            raise RuntimeError(
                "hap::agent did not create a root widget: "
                + pane_tab.activeInterfaceScriptErrors()
            )
        installed_panel._show_page(installed_panel.PAGE_TRANSCRIPT)
        installed_panel._header.set_agent("Codex", None)
        installed_panel._note("Codex 1.1.9 · /Users/may")
        installed_panel._composer.set_modes(
            [
                SessionMode("read-only", "Read Only"),
                SessionMode("agent", "Agent"),
                SessionMode("agent-full-access", "Agent Full Access"),
            ],
            "agent",
        )
        installed_panel._composer.set_config_options(
            [
                SimpleNamespace(
                    id="model",
                    name="Model",
                    current_value="gpt-5.6-sol",
                    category="model_config",
                    choices=(
                        SimpleNamespace(
                            value="gpt-5.6-sol", name="GPT-5.6-Sol", description=""
                        ),
                        SimpleNamespace(value="gpt-5.5", name="GPT-5.5", description=""),
                    ),
                ),
                SimpleNamespace(
                    id="reasoning_effort",
                    name="Reasoning effort",
                    current_value="medium",
                    category="thought_level",
                    choices=(
                        SimpleNamespace(value="medium", name="Medium", description=""),
                        SimpleNamespace(value="high", name="High", description=""),
                    ),
                ),
            ]
        )
        app.processEvents()
        app.processEvents()
        installed_panel_image = installed_panel.grab().toImage()
        installed_panel_image.save(str(output_dir / "probe-installed-panel.png"))
        installed_model = installed_panel._model("__idle__")
        installed_note = installed_model.entries()[-1]
        installed_note_prose = installed_panel._transcript._rows[
            installed_note.id
        ]._segments[0]
    first_row = settings._agents_view._rows_by_id["probe-0"]
    first_button = first_row.findChild(QtWidgets.QPushButton)
    settings_point = settings.mapTo(root, QtCore.QPoint(settings.width() // 2, 100))
    surface_point = composer._surface.mapTo(
        root, QtCore.QPoint(composer._surface.width() // 2, 20)
    )
    settings_color = image.pixelColor(settings_point)
    composer_color = image.pixelColor(surface_point)
    boot_surface_color = boot_image.pixelColor(surface_point)
    boot_bar_point = composer._boot_status._bar.mapTo(
        root, QtCore.QPoint(composer._boot_status._bar.width() // 2, 1)
    )
    boot_bar_color = boot_image.pixelColor(boot_bar_point)
    note_row = transcript._rows[note.id]
    note_prose = note_row._segments[0]
    note_point = note_prose.viewport().mapTo(
        state_root,
        QtCore.QPoint(
            note_prose.viewport().width() - 6,
            note_prose.viewport().height() // 2,
        ),
    )
    note_background_color = state_image.pixelColor(note_point)
    installed_note_background_color = None
    if installed_panel_image is not None and installed_note_prose is not None:
        installed_note_point = installed_note_prose.viewport().mapTo(
            installed_panel,
            QtCore.QPoint(
                installed_note_prose.viewport().width() - 6,
                installed_note_prose.viewport().height() // 2,
            ),
        )
        installed_note_background_color = installed_panel_image.pixelColor(installed_note_point)
    window_color = palette.color(QtGui.QPalette.Window)
    text_color = palette.color(QtGui.QPalette.Text)
    settings_ratio = _surface_ratio(settings_color, window_color, text_color)
    composer_ratio = _surface_ratio(composer_color, window_color, text_color)
    boot_surface_ratio = _surface_ratio(boot_surface_color, window_color, text_color)
    boot_bar_ratio = _surface_ratio(boot_bar_color, window_color, text_color)
    note_background_ratio = _surface_ratio(note_background_color, window_color, text_color)
    installed_note_background_ratio = (
        _surface_ratio(installed_note_background_color, window_color, text_color)
        if installed_note_background_color is not None
        else None
    )
    expect_fx = os.environ.get("HAP_EXPECT_FX") == "1"
    fx_port = None
    if expect_fx:
        from houdini_agent_panel import scene

        fx_port = scene.fx_port()
    roles = (
        "Window", "WindowText", "Base", "AlternateBase", "Text",
        "Button", "ButtonText", "Mid", "Highlight", "HighlightedText",
    )
    result = {
        "qt": QtCore.qVersion(),
        "style": app.style().objectName(),
        "application_stylesheet_length": len(app.styleSheet()),
        "application_palette": {
            name: _rgb(palette.color(getattr(QtGui.QPalette, name))) for name in roles
        },
        "settings_palette": {
            name: _rgb(settings.palette().color(getattr(QtGui.QPalette, name))) for name in roles
        },
        "rendered": {
            "settings": _rgb(settings_color),
            "settings_window_text_ratio": round(settings_ratio, 3),
            "composer": _rgb(composer_color),
            "composer_window_text_ratio": round(composer_ratio, 3),
            "boot_composer": _rgb(boot_surface_color),
            "boot_composer_window_text_ratio": round(boot_surface_ratio, 3),
            "boot_progress_track": _rgb(boot_bar_color),
            "boot_progress_window_text_ratio": round(boot_bar_ratio, 3),
            "system_note_background": _rgb(note_background_color),
            "system_note_window_text_ratio": round(note_background_ratio, 3),
        },
        "fx": {"running": fx_port is not None, "port": fx_port},
        "geometry": {
            "root": [root.width(), root.height()],
            "settings": _point(settings, root),
            "composer": _point(composer, root),
            "composer_editor": _point(composer._text_edit, root),
            "composer_send": _point(composer._send_button, root),
            "settings_header": _point(settings._header_rail, root),
            "agents_header": _point(settings._agents_section._toggle, root),
            "first_agent_row": _point(first_row, root),
            "first_agent_button": _point(first_button, root),
            "composer_surface": _point(composer._surface, root),
            "header": _point(header, root),
            "header_agent": _point(header._agent_button, root),
            "boot_status": _point(composer._boot_status, root),
            "state_header_agent": _point(state_header._agent_button, state_root),
            "system_note": _point(note_prose, state_root),
        },
        "output": str(output_dir / "probe.png"),
        "boot_output": str(output_dir / "probe-boot.png"),
        "state_output": str(output_dir / "probe-panel-state.png"),
        "installed_panel_output": (
            str(output_dir / "probe-installed-panel.png") if installed_panel is not None else None
        ),
    }
    if installed_note_background_color is not None:
        result["rendered"]["installed_system_note_background"] = _rgb(
            installed_note_background_color
        )
        result["rendered"]["installed_system_note_window_text_ratio"] = round(
            installed_note_background_ratio, 3
        )
    failures = []
    if not 0.02 <= settings_ratio <= 0.20:
        failures.append("Settings surface is not a subtle Window -> Text shade")
    if not 0.02 <= composer_ratio <= 0.20:
        failures.append("Composer surface is not a subtle Window -> Text shade")
    if abs(boot_surface_ratio - composer_ratio) > 0.03:
        failures.append("Boot overlay turns the composer darker than its resting surface")
    if boot_bar_ratio < 0.0:
        failures.append("Boot progress track is darker than the Houdini window")
    if note_background_ratio < -0.02:
        failures.append("System note paints a dark Base rectangle instead of the feed background")
    if installed_note_background_ratio is not None and installed_note_background_ratio < -0.02:
        failures.append("Installed panel system note paints a dark Base rectangle")
    if first_button.height() > 23 or first_button.width() > 64:
        failures.append("Agent action button is larger than the Houdini 22 geometry")
    if first_row.height() > 52:
        failures.append("Agent row is taller than the Houdini 22 geometry")
    agent_text_width = QtGui.QFontMetrics(header._agent_button.font()).horizontalAdvance("OpenCode")
    if header._agent_button.width() < agent_text_width + 20:
        failures.append("Header agent chip clips the active agent name")
    state_agent_text_width = QtGui.QFontMetrics(
        state_header._agent_button.font()
    ).horizontalAdvance("Codex")
    if state_header._agent_button.width() < state_agent_text_width + 20:
        failures.append("Live-state header clips the active agent name")
    for chip in [state_composer.mode_chip._combo, *state_composer._config_chips]:
        label = chip._button.text()
        label_width = QtGui.QFontMetrics(chip._button.font()).horizontalAdvance(label)
        if chip._button.width() < label_width + 8:
            failures.append("Composer choice clips its label: " + label)
    if installed_panel is not None:
        installed_choices = [
            installed_panel._composer.mode_chip._combo,
            *installed_panel._composer._config_chips,
        ]
        for chip in installed_choices:
            label = chip._button.text()
            label_width = QtGui.QFontMetrics(chip._button.font()).horizontalAdvance(label)
            # Houdini's docked Python Pane Tab paints 16px of inset on BOTH
            # sides although its native QToolButton hint reports only 38px.
            # A merely nominal text-width geometry still renders A...t.
            if chip._button.width() < label_width + 32:
                failures.append("Python Pane Tab clips its composer choice: " + label)
    if expect_fx and fx_port is None:
        failures.append("fxhoudinimcp plugin did not start in this Houdini")
    result["failures"] = failures
    (output_dir / "result.json").write_text(
        json.dumps(result, indent=2, sort_keys=True), encoding="utf-8"
    )
    print("HAP_UI_PROBE=" + json.dumps(result, sort_keys=True), flush=True)
    # The installed-package liveness runner needs a short window to probe
    # hwebserver from outside Houdini. Ordinary visual runs can exit now.
    exit_code = 1 if failures else 0
    if expect_fx:
        QtCore.QTimer.singleShot(8000, lambda: os._exit(exit_code))
    else:
        # Houdini 22 can keep renderer threads alive after QApplication.quit().
        # This process exists only for the isolated probe, so exit deterministically.
        os._exit(exit_code)


QtCore.QTimer.singleShot(6000 if os.environ.get("HAP_EXPECT_FX") == "1" else 1500, _capture)

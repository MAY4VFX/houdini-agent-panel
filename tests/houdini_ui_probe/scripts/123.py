"""Capture the panel's real in-Houdini palette and layout, then exit.

This is loaded by Houdini through ``HOUDINI_PATH``.  It is deliberately a
real GUI probe rather than a hython test: hython does not install Houdini's
application style/palette, which is the subject under test here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# The launcher must provide a throwaway directory.  Refuse to import the
# package if it did not: accidentally using the owner's settings may autostart
# a real agent on their account.
if not os.environ.get("HAP_DATA_DIR", "").startswith("/tmp/hap-ui-probe."):
    raise RuntimeError("HAP_DATA_DIR must be a /tmp/hap-ui-probe.* directory")

from hutil.PySide import QtCore, QtGui, QtWidgets

from houdini_agent_panel.ui.chips import HeaderBar
from houdini_agent_panel.ui.composer import Composer
from houdini_agent_panel.ui.settings_view import SettingsView
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
    window_color = palette.color(QtGui.QPalette.Window)
    text_color = palette.color(QtGui.QPalette.Text)
    settings_ratio = _surface_ratio(settings_color, window_color, text_color)
    composer_ratio = _surface_ratio(composer_color, window_color, text_color)
    boot_surface_ratio = _surface_ratio(boot_surface_color, window_color, text_color)
    boot_bar_ratio = _surface_ratio(boot_bar_color, window_color, text_color)
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
        },
        "output": str(output_dir / "probe.png"),
        "boot_output": str(output_dir / "probe-boot.png"),
    }
    failures = []
    if not 0.02 <= settings_ratio <= 0.20:
        failures.append("Settings surface is not a subtle Window -> Text shade")
    if not 0.02 <= composer_ratio <= 0.20:
        failures.append("Composer surface is not a subtle Window -> Text shade")
    if abs(boot_surface_ratio - composer_ratio) > 0.03:
        failures.append("Boot overlay turns the composer darker than its resting surface")
    if boot_bar_ratio < 0.0:
        failures.append("Boot progress track is darker than the Houdini window")
    if first_button.height() > 23 or first_button.width() > 64:
        failures.append("Agent action button is larger than the Houdini 22 geometry")
    if first_row.height() > 52:
        failures.append("Agent row is taller than the Houdini 22 geometry")
    agent_text_width = QtGui.QFontMetrics(header._agent_button.font()).horizontalAdvance("OpenCode")
    if header._agent_button.width() < agent_text_width + 20:
        failures.append("Header agent chip clips the active agent name")
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

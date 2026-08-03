"""Tests for `ui/theme.py`'s theme-following colour helpers.

No `hou` is importable here (plain pytest, no Houdini) — every function has
to fall back to the `QApplication` palette cleanly, which is exactly the
path a real Houdini session also takes whenever a scheme name doesn't
resolve. Needs `QApplication` (the `qapp` fixture).
"""

from __future__ import annotations

from houdini_agent_panel.ui import theme
from houdini_agent_panel.ui.qt import QtGui


def test_accent_color_falls_back_to_palette_highlight_outside_houdini(qapp):
    qapp.setPalette(QtGui.QPalette())  # a clean, known palette
    highlight = qapp.palette().color(QtGui.QPalette.Highlight)

    assert theme.accent_color() == highlight


def test_accent_color_follows_a_different_application_palette(qapp):
    """No `hou` here, so the fallback IS the live behaviour — swapping the
    app's palette (what a Houdini colour-scheme switch amounts to for
    everything downstream of `QApplication.palette()`) must change the
    accent the same way it would inside Houdini."""
    palette = QtGui.QPalette()
    palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor("#ff33aa"))
    qapp.setPalette(palette)

    assert theme.accent_color() == QtGui.QColor("#ff33aa")


def test_to_hex_produces_a_literal_stylesheet_can_use():
    assert theme.to_hex(QtGui.QColor(255, 0, 128)) == "#ff0080"


def test_contrasting_text_color_picks_dark_on_light_background():
    assert theme.contrasting_text_color(QtGui.QColor(240, 240, 240)) == QtGui.QColor(20, 20, 20)


def test_contrasting_text_color_picks_light_on_dark_background():
    assert theme.contrasting_text_color(QtGui.QColor(20, 20, 20)) == QtGui.QColor(240, 240, 240)


def test_popup_stylesheet_contains_only_colours_derived_from_the_live_palette(qapp):
    palette = QtGui.QPalette()
    palette.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor("#112233"))
    palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor("#ff33aa"))
    qapp.setPalette(palette)

    sheet = theme.popup_stylesheet("choicePopup")

    assert "#112233" in sheet  # the surface fill, straight from AlternateBase
    assert "#ff33aa" in sheet  # the accent, used for the checked item
    assert "QFrame#choicePopup" in sheet


def test_popup_background_reads_alternate_base(qapp):
    palette = QtGui.QPalette()
    palette.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor("#445566"))
    qapp.setPalette(palette)

    assert theme.popup_background() == QtGui.QColor("#445566")


def test_no_hardcoded_hex_literals_remain_in_ui_sources():
    """The regression test the whole task is really about: `ui/**` builds
    every colour from `theme.py`, never from a `#rrggbb` written by hand."""
    import re
    from pathlib import Path

    ui_dir = Path(__file__).resolve().parents[1] / "python" / "houdini_agent_panel" / "ui"
    hex_literal = re.compile(r"#[0-9a-fA-F]{3,8}\b")
    offenders = []
    for path in sorted(ui_dir.glob("*.py")):
        if path.name == "theme.py":
            # theme.py is the one place allowed to construct a QColor from a
            # scheme name's return value — it holds no colour literals of
            # its own, but it's also not a call site this test cares about.
            continue
        for lineno, line in enumerate(path.read_text("utf-8").splitlines(), start=1):
            if hex_literal.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert offenders == [], "hardcoded hex colour(s) found:\n" + "\n".join(offenders)


def test_no_hardcoded_qcolor_literals_remain_in_ui_sources():
    """A hex grep alone missed `_HOUDINI_AMBER = QtGui.QColor(222, 142, 74)`
    in `thinking.py` — a numeric-argument `QColor(...)` call is just as much
    a fixed colour as a `#rrggbb` string. `theme.py` is excluded entirely
    (it's the one place allowed to build a `QColor` from whatever a palette
    or scheme lookup returned). Elsewhere, a line marked `# theme-exception:`
    is a DELIBERATE, individually-justified fixed colour — currently just
    the permission popover's drop shadow (`permissions.py`): a shadow reads
    as depth/occlusion, which is dark by convention in light and dark UI
    alike, and Houdini's own `.hcs` files don't even define a scheme name
    for one. Anything else has to come from `theme.py`.
    """
    import re
    from pathlib import Path

    ui_dir = Path(__file__).resolve().parents[1] / "python" / "houdini_agent_panel" / "ui"
    numeric_qcolor = re.compile(r"QColor\(\s*\d")
    offenders = []
    for path in sorted(ui_dir.glob("*.py")):
        if path.name == "theme.py":
            continue
        for lineno, line in enumerate(path.read_text("utf-8").splitlines(), start=1):
            if "# theme-exception:" in line:
                continue
            if numeric_qcolor.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert offenders == [], "hardcoded QColor(...) literal(s) found:\n" + "\n".join(offenders)


def test_no_direct_application_palette_reads_outside_theme():
    """`theme.palette()` is the one place `QApplication.palette()` gets read.

    Every other widget goes through `theme.color()`/`theme.accent_color()`/
    `theme.palette()` — a call site reading `QApplication.palette()` (or
    `QtWidgets.QApplication.palette()`) directly would quietly start
    bypassing whatever priority `theme.py` settles on, and nothing else
    would catch that regression. (`self.palette()` — a widget's OWN, already
    theme-derived palette, used to read back or locally tweak one role — is
    a different, legitimate pattern and isn't what this checks for.)
    """
    import re
    from pathlib import Path

    ui_dir = Path(__file__).resolve().parents[1] / "python" / "houdini_agent_panel" / "ui"
    direct_read = re.compile(r"QApplication\.palette\(\)")
    offenders = []
    for path in sorted(ui_dir.glob("*.py")):
        if path.name == "theme.py":
            continue
        for lineno, line in enumerate(path.read_text("utf-8").splitlines(), start=1):
            if direct_read.search(line):
                offenders.append(f"{path.name}:{lineno}: {line.strip()}")
    assert offenders == [], "direct QApplication.palette() read(s) found:\n" + "\n".join(offenders)

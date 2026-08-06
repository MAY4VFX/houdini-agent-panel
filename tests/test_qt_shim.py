"""`ui/qt.py` — the one place PySide2 (Houdini 20.5) and PySide6 (Houdini 22)
differences get reconciled, so no other call site has to know about them.

This suite runs on PySide6 only (`ui/qt.py`'s own module docstring: outside
Houdini, tests take PySide6 directly), so a real cross-binding regression —
a symbol that only exists on one of the two — CANNOT be caught here. That is
what actually happened: `QShortcut` used directly from `QtGui` crashed the
panel's construction on real Houdini 20.5 (PySide2 keeps it in `QtWidgets`),
with all 889 tests here still green, because none of them import PySide2 at
all. The only check that catches that class of bug is running the smoke
script under BOTH real `hython`s (`QT_QPA_PLATFORM=offscreen <hython>
<script>`), which is not something pytest can do from inside a `.venv`.

What IS worth checking here: that the shim's own resolution logic doesn't
silently break (e.g. a typo'd attribute name) and that its output is
actually a usable Qt class — a much weaker guarantee, but a real one, and
free to run on every commit unlike the hython check.
"""

from __future__ import annotations

from houdini_agent_panel.ui import qt as qt_mod
from houdini_agent_panel.ui.qt import QAction, QShortcut, QtGui, QtWidgets


def test_qshortcut_is_exported_and_matches_the_live_binding():
    """On PySide6 (what this suite runs on) `QShortcut` lives in `QtGui` —
    confirms the shim resolved to a real class, not `None`, and that it's
    the SAME one the live binding actually offers, not a stand-in."""
    assert QShortcut is not None
    assert QShortcut is QtGui.QShortcut


def test_qaction_is_exported_and_matches_the_live_binding():
    """Same shape, the shim's other already-established PySide2/PySide6
    split (`ui/qt.py`'s own docstring on `QAction`) — checked here too so
    the pattern this file exists to guard has more than one example."""
    assert QAction is not None
    assert QAction is QtGui.QAction


def test_qshortcut_is_listed_in_all_and_actually_usable(qapp):
    """`__all__` is the shim's own contract for what it exports — a symbol
    missing from it is a silent trap for the next `from .qt import *`-style
    read of what's available. Constructing one for real (not just checking
    the class object) is what caught the actual bug on PySide2, where the
    attribute lookup itself raised before construction was ever reached."""
    assert "QShortcut" in qt_mod.__all__
    widget = QtWidgets.QWidget()
    shortcut = QShortcut(QtGui.QKeySequence("Esc"), widget)
    assert shortcut.parent() is widget

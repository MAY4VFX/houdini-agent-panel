"""The single Qt import point for the whole project.

Houdini brings its own Qt: PySide6 on 22.0, PySide2 on 20.5. The
``hutil.PySide`` shim is Houdini's own code and already knows which one is
live, which is why a direct ``import PySide6`` is forbidden by the project's
rules: it would build on one version and fall apart on the other.

Outside Houdini (unit tests, linters) there is no ``hutil``. There we take
PySide6 directly — the same Qt as in H22, so the tests exercise the real code
rather than a stand-in.

Import like this::

    from houdini_agent_panel.ui.qt import QtCore, QtGui, QtWidgets, Signal, Slot
"""

from __future__ import annotations

#: Where Qt actually came from. Useful in diagnostics for bug reports.
QT_SOURCE: str

try:  # inside Houdini
    from hutil.PySide import QtCore, QtGui, QtWidgets  # type: ignore[import-not-found]

    QT_SOURCE = "hutil.PySide"
except ImportError:  # pragma: no cover - outside Houdini
    try:
        from PySide6 import QtCore, QtGui, QtWidgets  # type: ignore[no-redef]

        QT_SOURCE = "PySide6"
    except ImportError:
        from PySide2 import QtCore, QtGui, QtWidgets  # type: ignore[no-redef]

        QT_SOURCE = "PySide2"

# PySide2 and PySide6 differ in small ways that surface in every UI file.
# They're reconciled here once so the rest of the code never sees the split.
Signal = QtCore.Signal
Slot = QtCore.Slot
Property = QtCore.Property
Qt = QtCore.Qt

#: PySide6 moved things: ``QAction`` now lives in QtGui, PySide2 had it in QtWidgets.
QAction = getattr(QtGui, "QAction", None) or QtWidgets.QAction  # type: ignore[attr-defined]

QT_VERSION = QtCore.qVersion()

__all__ = [
    "QAction",
    "Property",
    "QT_SOURCE",
    "QT_VERSION",
    "Qt",
    "QtCore",
    "QtGui",
    "QtWidgets",
    "Signal",
    "Slot",
]

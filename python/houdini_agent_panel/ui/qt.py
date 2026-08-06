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

#: Same split, same reason: ``QShortcut`` moved to QtGui in PySide6 (Qt6),
#: PySide2 (Qt5, Houdini 20.5's own binding) still has it in QtWidgets. A
#: call site reaching into `QtGui.QShortcut` directly crashed the panel on
#: 20.5 with `AttributeError: module 'PySide2.QtGui' has no attribute
#: 'QShortcut'` — the panel never even constructed. Resolved here, once,
#: the same way `QAction` already is, so no other call site can repeat it.
QShortcut = getattr(QtGui, "QShortcut", None) or QtWidgets.QShortcut  # type: ignore[attr-defined]

QT_VERSION = QtCore.qVersion()


def discard(widget, layout=None) -> None:
    """Take a widget off the screen for good, in the one order that is safe.

    The four lines below were written out at a dozen call sites, and the two
    that skipped a step are exactly the bugs this project has already paid
    for once each:

    - `hide()` BEFORE orphaning, because a parentless `QWidget` is a
      top-level window and macOS gives it a real native one the moment it
      exists — a feed that draws a row per message produced hundreds of
      stray windows that way.
    - `setParent(None)` right away rather than trusting `deleteLater()`,
      because until the next event-loop pass the widget is still a child:
      it still answers `findChildren`, still counts in a rebuild, and still
      paints at its old geometry under whatever replaced it.

    `layout` is the layout the widget was placed in, when the caller has
    already taken it out itself (`takeAt`) it can be left out.
    """
    if widget is None:
        return
    if layout is not None:
        layout.removeWidget(widget)
    widget.hide()
    widget.setParent(None)
    widget.deleteLater()


def clear_layout(layout) -> None:
    """Empty a layout of its widgets, discarding each one properly.

    Nested layouts are left alone deliberately: nothing in the panel builds
    one, and silently deleting a sub-layout's widgets would be a much bigger
    thing to do than "clear these rows".
    """
    while layout.count():
        item = layout.takeAt(0)
        discard(item.widget())


__all__ = [
    "QAction",
    "QShortcut",
    "clear_layout",
    "discard",
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

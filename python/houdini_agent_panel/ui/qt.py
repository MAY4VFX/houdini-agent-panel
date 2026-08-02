"""Единственная точка импорта Qt во всём проекте.

Houdini приносит свой Qt: на 22.0 это PySide6, на 20.5 — PySide2. Шим
``hutil.PySide`` — код самой Houdini, он уже знает, какая из них живая, поэтому
прямые ``import PySide6`` запрещены правилами проекта: они соберутся на одной
версии и развалятся на другой.

Вне Houdini (юнит-тесты, линтеры) ``hutil`` нет. Там берём PySide6 напрямую —
это тот же Qt, что в H22, так что тесты проверяют настоящий код, а не заглушку.

Импортировать так::

    from houdini_agent_panel.ui.qt import QtCore, QtGui, QtWidgets, Signal, Slot
"""

from __future__ import annotations

#: Откуда реально приехал Qt. Полезно в диагностике для баг-репортов.
QT_SOURCE: str

try:  # внутри Houdini
    from hutil.PySide import QtCore, QtGui, QtWidgets  # type: ignore[import-not-found]

    QT_SOURCE = "hutil.PySide"
except ImportError:  # pragma: no cover - вне Houdini
    try:
        from PySide6 import QtCore, QtGui, QtWidgets  # type: ignore[no-redef]

        QT_SOURCE = "PySide6"
    except ImportError:
        from PySide2 import QtCore, QtGui, QtWidgets  # type: ignore[no-redef]

        QT_SOURCE = "PySide2"

# PySide2 и PySide6 расходятся в мелочах, которые всплывают в каждом файле UI.
# Сводим их здесь один раз, чтобы остальной код не знал о разнице.
Signal = QtCore.Signal
Slot = QtCore.Slot
Property = QtCore.Property
Qt = QtCore.Qt

#: PySide6 переехал: ``QAction`` теперь в QtGui, в PySide2 он был в QtWidgets.
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

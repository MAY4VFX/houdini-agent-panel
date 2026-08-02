"""Анимация компаньона живёт ровно столько, сколько её видно.

Регрессия из релиза 0.1.2: таймер запускался в `__init__` и не останавливался
никогда — 20 тиков в секунду всё время жизни панели, включая моменты, когда её
вкладка неактивна или скрыта. Панель живёт в чужом процессе, в котором человек
работает со сценой, и тратить его такт на перерисовку невидимого маскота она
права не имеет.
"""

from __future__ import annotations

import pytest

from houdini_agent_panel.ui.qt import QtWidgets
from houdini_agent_panel.ui.thinking import _BuddySprite


@pytest.fixture
def sprite(qapp):
    host = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(host)
    widget = _BuddySprite(host)
    layout.addWidget(widget)
    yield host, widget
    host.deleteLater()


def test_timer_is_idle_until_shown(sprite):
    _host, widget = sprite
    assert not widget._timer.isActive()


def test_timer_stops_when_hidden_and_resumes_when_shown(sprite, qapp):
    host, widget = sprite

    host.show()
    qapp.processEvents()
    assert widget._timer.isActive()

    host.hide()
    qapp.processEvents()
    assert not widget._timer.isActive(), "скрытая панель не имеет права тикать"

    host.show()
    qapp.processEvents()
    assert widget._timer.isActive()


def test_reduced_motion_never_starts_the_timer(qapp, monkeypatch):
    monkeypatch.setenv("HOUDINI_AGENT_REDUCED_MOTION", "1")

    host = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(host)
    widget = _BuddySprite(host)
    layout.addWidget(widget)

    host.show()
    qapp.processEvents()

    assert not widget._timer.isActive()
    host.deleteLater()

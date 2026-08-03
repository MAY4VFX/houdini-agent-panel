"""The companion's animation lives exactly as long as it is visible.

A regression from release 0.1.2: the timer started in `__init__` and never
stopped — twenty ticks a second for the whole life of the panel, including
while its tab was inactive or hidden. The panel lives in someone else's
process, where a human is working on a scene, and it has no right to spend
their frame time redrawing a mascot nobody can see.
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
    assert not widget._timer.isActive(), "a hidden panel has no right to tick"

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

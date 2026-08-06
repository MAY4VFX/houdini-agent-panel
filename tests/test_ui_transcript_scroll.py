"""The feed follows a streaming answer.

Reported from a real session: "when the agent is answering the scroll
doesn't scroll — it just stays put". Following used to be re-derived on
every refresh from "is the bar at the bottom right now" — an answer that
is wrong for one frame whenever content grows faster than layout settles.
One such frame and the
feed concluded the artist had scrolled away, and stopped following for the
rest of the answer.
"""

from __future__ import annotations

import pytest

from houdini_agent_panel.transcript_model import TranscriptModel
from houdini_agent_panel.ui.transcript import TranscriptView


def _view(qapp) -> TranscriptView:
    view = TranscriptView()
    view.resize(400, 200)
    view.set_model(TranscriptModel())
    view.show()
    qapp.processEvents()
    return view


def _stream(qapp, view: TranscriptView, chunks: int, *, message_id: str = "m1") -> None:
    model = view._model
    for index in range(chunks):
        entry = model.apply_chunk(message_id, f"line {index} " * 12 + "\n")
        view.refresh(entry.id)
        qapp.processEvents()


def test_the_view_keeps_following_a_long_answer(qapp):
    view = _view(qapp)
    _stream(qapp, view, 30)

    bar = view.verticalScrollBar()
    assert bar.maximum() > 0, "the feed never overflowed — the test proves nothing"
    assert view._follow_bottom, "the feed stopped following on its own"
    assert bar.value() >= bar.maximum() - 4, (
        f"the view fell behind: at {bar.value()} of {bar.maximum()}"
    )
    view.deleteLater()


def test_scrolling_up_stops_the_follow_and_returning_resumes_it(qapp):
    """Someone reading back must be left alone — and must be able to rejoin."""
    view = _view(qapp)
    _stream(qapp, view, 30)
    bar = view.verticalScrollBar()
    assert bar.maximum() > 0, "nothing to scroll — the test proves nothing"

    bar.setValue(0)          # the artist scrolls up to read
    qapp.processEvents()
    assert not view._follow_bottom

    _stream(qapp, view, 10, message_id="m2")
    assert bar.value() == 0, "the feed yanked the reader back to the bottom"

    bar.setValue(bar.maximum())   # ...and scrolls back down themselves
    qapp.processEvents()
    assert view._follow_bottom, "returning to the bottom must resume following"
    view.deleteLater()


def test_rebuilding_a_message_never_makes_a_window(qapp):
    """The reported "little panes appear over Houdini and then hide
    themselves".

    A widget with no parent IS a top-level window, and macOS composites it
    for a frame the moment it exists. Rebuilding a message's segments
    detached each old block with `setParent(None)` before deleting it — so
    every streamed chunk flashed a small panel over Houdini and left a native
    window that is never reclaimed. Measured: 100+ windows realised while one
    agent connected, against 2 afterwards, both of them the panel itself.

    Hiding first is what fixes it: a hidden widget going top-level is never
    shown. The detach itself has to stay — several call sites need the widget
    to stop being a child immediately.
    """
    from houdini_agent_panel.ui.qt import QtCore, QtWidgets

    realised: list[str] = []

    class _Spy(QtCore.QObject):
        def eventFilter(self, obj, event):
            if (
                isinstance(obj, QtWidgets.QWidget)
                and obj.isWindow()
                and event.type() in (QtCore.QEvent.Show, QtCore.QEvent.WinIdChange)
            ):
                realised.append(type(obj).__name__)
            return False

    view = TranscriptView()
    view.set_model(TranscriptModel())
    view.resize(400, 300)
    qapp.processEvents()

    spy = _Spy()
    qapp.installEventFilter(spy)
    try:
        model = view._model
        for i in range(12):
            entry = model.apply_chunk("m1", f"chunk {i} " * 8)
            view.refresh(entry.id)
            qapp.processEvents()
    finally:
        qapp.removeEventFilter(spy)

    assert not realised, f"rebuilding a message made windows: {sorted(set(realised))}"
    view.deleteLater()

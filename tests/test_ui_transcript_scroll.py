"""The feed follows a streaming answer.

Reported from a real session: "когда агент отвечает скрол не прокручивается,
а стоит на месте". Following used to be re-derived on every refresh from
"is the bar at the bottom right now" — an answer that is wrong for one frame
whenever content grows faster than layout settles. One such frame and the
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

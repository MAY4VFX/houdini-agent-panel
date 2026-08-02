"""The input grows with the text, then scrolls — not the other way round.

Reported from a live panel: one long paragraph typed without a single Enter
stayed one line tall and went straight to a scrollbar. The height was
computed by counting "\\n", which sees explicit line breaks and is blind to
wrapping.
"""

from __future__ import annotations

import pytest

from houdini_agent_panel.ui.composer import Composer
from houdini_agent_panel.ui.qt import QtCore


@pytest.fixture
def composer(qapp):
    widget = Composer()
    widget.resize(700, 300)
    widget.show()
    qapp.processEvents()
    yield widget
    widget.deleteLater()


def test_wrapped_text_grows_the_field(composer, qapp):
    """No newlines at all — growth has to come from wrapping alone."""
    empty_height = composer._text_edit.height()

    composer._text_edit.setPlainText("wrap me " * 30)
    qapp.processEvents()

    assert composer._text_edit.height() > empty_height


def test_growth_stops_at_the_ceiling(composer, qapp):
    composer._text_edit.setPlainText("wrap me " * 60)
    qapp.processEvents()
    medium = composer._text_edit.height()

    composer._text_edit.setPlainText("wrap me " * 600)
    qapp.processEvents()
    huge = composer._text_edit.height()

    assert huge >= medium
    from houdini_agent_panel.ui.composer import _MAX_LINES

    assert huge < 100 * _MAX_LINES, "the field must not swallow the whole panel"


def test_scrollbar_appears_only_once_there_is_no_room_left(composer, qapp):
    composer._text_edit.setPlainText("short")
    qapp.processEvents()
    assert composer._text_edit.verticalScrollBarPolicy() == QtCore.Qt.ScrollBarAlwaysOff

    composer._text_edit.setPlainText("wrap me " * 600)
    qapp.processEvents()
    assert composer._text_edit.verticalScrollBarPolicy() == QtCore.Qt.ScrollBarAsNeeded


def test_height_is_sane_without_a_layout_pass(qapp):
    """Headless, never shown: the fallback must still give a usable field."""
    widget = Composer()
    widget._text_edit.setPlainText("one\ntwo\nthree")
    widget._adjust_text_height()

    assert widget._text_edit.height() >= 55
    widget.deleteLater()

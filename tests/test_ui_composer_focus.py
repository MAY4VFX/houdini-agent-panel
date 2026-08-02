"""One click anywhere on the input card starts typing.

Reported from a live panel: the field appeared to need a double-click. The
text edit only occupies part of the rounded card — there's padding around it
and a control row below — and a click on that padding landed on the frame
and did nothing.
"""

from __future__ import annotations

from houdini_agent_panel.ui.qt import QtCore, QtGui, QtWidgets


def _click(widget, point: QtCore.QPoint) -> None:
    event = QtGui.QMouseEvent(
        QtCore.QEvent.MouseButtonPress,
        QtCore.QPointF(point),
        QtCore.Qt.LeftButton,
        QtCore.Qt.LeftButton,
        QtCore.Qt.NoModifier,
    )
    QtWidgets.QApplication.sendEvent(widget, event)


def test_click_on_the_card_padding_focuses_the_input(qapp):
    from houdini_agent_panel.ui.composer import Composer

    composer = Composer()
    composer.show()
    qapp.processEvents()
    # `hasFocus()` is not usable here: it is only true when the widget's
    # window is the active one, and in a full test run some other window
    # holds activation. `focusWidget()` answers the question we actually
    # care about — which child inside this composer is the focus target.
    composer._text_edit.clearFocus()
    qapp.processEvents()
    assert composer.focusWidget() is not composer._text_edit

    # The very top of the card — padding above the text edit, the spot that
    # used to swallow the click.
    _click(composer._surface, QtCore.QPoint(composer._surface.width() // 2, 4))
    qapp.processEvents()

    assert composer.focusWidget() is composer._text_edit, (
        "one click on the card must start typing"
    )
    composer.deleteLater()


def test_blocked_input_does_not_steal_focus(qapp):
    """A blocking announcement disables input — clicking the card must not
    pretend it's usable."""
    from houdini_agent_panel.ui.composer import Composer

    composer = Composer()
    composer.show()
    qapp.processEvents()
    composer.block_input("Limit reached")
    composer._text_edit.clearFocus()
    qapp.processEvents()

    _click(composer._surface, QtCore.QPoint(composer._surface.width() // 2, 4))
    qapp.processEvents()

    assert composer.focusWidget() is not composer._text_edit
    composer.deleteLater()

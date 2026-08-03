"""A failed sign-in has to be visible where the sign-in happens.

From a live session: Google authenticated the browser, Gemini CLI then
refused the method ("no longer supported for individuals"), and the panel
showed nothing at all — the error went into a feed the artist could not see
because they were on the sign-in screen.
"""

from __future__ import annotations

import pytest

from houdini_agent_panel.client import AuthMethod
from houdini_agent_panel.ui.auth_view import AuthView


@pytest.fixture
def view(qapp):
    widget = AuthView()
    widget.set_methods(
        [
            AuthMethod(id="oauth-personal", name="Log in with Google"),
            AuthMethod(id="gemini-api-key", name="Gemini API key"),
        ],
        can_logout=False,
    )
    yield widget
    widget.deleteLater()


def test_failure_is_shown_on_the_sign_in_screen(view):
    view.show_error("No longer supported for individuals.", "oauth-personal")

    # `isVisible()` needs a shown parent; `isHidden()` answers what we
    # actually mean — did the widget get switched on.
    assert not view._error_label.isHidden()
    assert "individuals" in view._error_label.text()


def test_the_failed_method_is_marked_but_kept(view):
    """Which methods exist is the agent's word. Hiding one on our own
    initiative means that the day it starts working, the panel keeps the
    working door shut."""
    view.show_error("Refused.", "oauth-personal")

    assert view._buttons["oauth-personal"].property("signInFailed") is True
    assert not view._buttons["oauth-personal"].isHidden()
    assert view._buttons["gemini-api-key"].property("signInFailed") is False


def test_redrawing_the_methods_clears_an_old_failure(view):
    view.show_error("Refused.", "oauth-personal")

    view.set_methods([AuthMethod(id="oauth-personal", name="Log in with Google")], can_logout=False)

    assert view._error_label.isHidden()
    # A freshly built button never carried the flag at all, which is the same
    # thing as not failed — hence falsy rather than exactly False.
    assert not view._buttons["oauth-personal"].property("signInFailed")


def test_buttons_do_not_stretch_across_a_wide_panel(view, qapp):
    """Edge-to-edge buttons across a docked panel read as a form, not as a
    choice of four."""
    view.resize(1500, 400)
    view.show()
    qapp.processEvents()

    button = view._buttons["oauth-personal"]
    assert button.width() < 900, f"button spans {button.width()}px of a 1500px panel"

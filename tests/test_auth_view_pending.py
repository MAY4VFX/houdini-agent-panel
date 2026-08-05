"""Waiting for `authenticate()` has to be visible where the artist is
looking, not silent.

From the issue: a WORKING Codex login (browser opens, artist finishes it
there) and a stuck Kimi one (nothing visibly happens) looked identical on
the sign-in screen — both were just silence. `AuthView.set_pending` is what
draws the difference between "nothing is happening" and "something is
happening, here is what to expect, and here is how to give up waiting".
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
            AuthMethod(id="chat-gpt", name="ChatGPT"),
            AuthMethod(id="api-key", name="API key"),
        ],
        can_logout=True,
    )
    yield widget
    widget.deleteLater()


def test_pending_message_is_shown_and_buttons_are_disabled(view):
    view.set_pending("Opening ChatGPT in your browser…")

    assert not view._pending_label.isHidden()
    assert "browser" in view._pending_label.text()
    assert not view._buttons["chat-gpt"].isEnabled()
    assert not view._buttons["api-key"].isEnabled()
    assert not view._logout_button.isEnabled()


def test_cancel_clears_pending_and_re_enables_the_list(view):
    """Cancel is UI-only — there is no protocol call to actually stop
    `authenticate()` (docs/facts/acp-sdk.md §12) — so this just gives the
    method list back, it doesn't claim to have stopped anything."""
    view.set_pending("Waiting…")

    cancelled = []
    view.cancel_pending.connect(lambda: cancelled.append(True))
    view._cancel_pending_button.click()

    assert cancelled == [True]
    assert view._pending_label.isHidden()
    assert view._buttons["chat-gpt"].isEnabled()
    assert view._logout_button.isEnabled()


def test_an_error_ends_the_pending_state(view):
    """A quick failure (Codex `api-key`: "CODEX_API_KEY ... is not set")
    must not leave the wait indicator showing forever alongside it."""
    view.set_pending("Codex reads its API key from the environment…")

    view.show_error("CODEX_API_KEY or OPENAI_API_KEY is not set", "api-key")

    assert view._pending_label.isHidden()
    assert view._buttons["api-key"].isEnabled()


def test_redrawing_the_methods_clears_a_stale_pending_state(view):
    """`auth_required` firing again (a fresh method list) means whatever was
    in flight before is moot — e.g. the artist switched to a different
    agent's sign-in screen entirely."""
    view.set_pending("Waiting…")

    view.set_methods([AuthMethod(id="chat-gpt", name="ChatGPT")], can_logout=False)

    assert view._pending_label.isHidden()
    assert view._buttons["chat-gpt"].isEnabled()


def test_a_long_pending_message_does_not_get_clipped(view, qapp):
    """Found while verifying this screen visually: `rail` only had
    `setMaximumWidth` — its REAL width was whatever its narrowest button
    wanted, often far short of 736px. A one-line error never showed it, but
    a several-sentence pending message (docs/facts/acp-sdk.md §12's own
    length) wrapped to more lines than the layout reserved height for,
    silently cutting off the last line. `AuthView.resizeEvent` (mirroring
    `Composer`/`SettingsView`'s own) gives the rail a real, known width
    before the wrapped label measures itself — this asserts the label ends
    up tall enough for everything `heightForWidth` says it needs, at the
    width it is actually given.
    """
    message = (
        "Waiting for the agent to finish signing in — this can take a few "
        "seconds. If nothing opens in Houdini, this agent may be expecting "
        "it in its OWN command-line window instead; check there. The panel "
        "is watching either way and will move on the moment it succeeds."
    )
    view.set_pending(message)
    view.resize(760, 500)
    view.show()
    for _ in range(5):
        qapp.processEvents()

    label = view._pending_label
    assert label.height() >= label.heightForWidth(label.width())

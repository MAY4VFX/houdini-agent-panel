"""Escape cancels a running turn — the owner's own ask, verbatim: "хочу
чтобы эскейп прерывал задачу если он сделан над панелью."

Reuses the ONE cancellation path the panel already had (the Stop button:
`Composer.cancelled` -> `AgentPanel._on_cancelled` -> `AcpClient.cancel` ->
`session/cancel`) rather than adding a second one — see `_on_escape_pressed`'s
own docstring in `ui/panel.py` for the full priority order this settled on:
the composer's own slash popup first, then Settings, then a pending
permission request (answered as "cancelled" — there is no such thing as
just closing one, the agent is genuinely waiting on `session/request_
permission`), and only then a running turn.

The shortcut this reuses (`AgentPanel._escape_shortcut`, a `QShortcut` with
`WidgetWithChildrenShortcut` context, first built for closing Settings) is
kept DISABLED whenever none of its four things applies — a `QShortcut`
consumes its key unconditionally once matched, so disabling it is the only
way Escape can ever reach Houdini instead, per the owner's own requirement
that the panel must never swallow the key "just because."
"""

from __future__ import annotations

import pytest

from houdini_agent_panel import sessions
from houdini_agent_panel.ui import panel as panel_mod


@pytest.fixture(autouse=True)
def isolated(qapp, monkeypatch):
    monkeypatch.setattr(panel_mod.scene, "hip_dir", lambda: "/tmp")
    monkeypatch.setattr(
        panel_mod.scene, "mcp_servers",
        lambda: [{"name": "fxhoudini", "command": "python", "args": [], "env": []}],
    )
    monkeypatch.setattr(panel_mod._RefreshWorker, "start", lambda self: None)
    panel_mod.reset_shared_state_for_tests()
    yield
    panel_mod.reset_shared_state_for_tests()


def _state(session_id: str = "s1") -> sessions.SessionState:
    return sessions.SessionState(
        session_id=session_id, title="New conversation", cwd="/tmp", created_at=0.0
    )


def _live_widget(qapp, monkeypatch, session_id: str = "s1"):
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    client = panel_mod.shared_client(widget._agent_id)
    state = _state(session_id)
    client.session_started.emit(state.session_id, state)
    qapp.processEvents()
    calls: list[tuple[str, list]] = []
    monkeypatch.setattr(client, "prompt", lambda sid, blocks: calls.append((sid, blocks)))
    return widget, client, state, calls


# --- the core ask: Escape cancels a running turn ----------------------------


def test_escape_cancels_a_running_turn(qapp, monkeypatch):
    widget, client, state, calls = _live_widget(qapp, monkeypatch)
    cancelled: list[str] = []
    monkeypatch.setattr(client, "cancel", lambda sid: cancelled.append(sid))
    widget._on_submitted([{"type": "text", "text": "long task"}])
    assert state.busy is True
    assert widget._escape_shortcut.isEnabled(), "something IS running — Escape must be armed"

    widget._on_escape_pressed()

    assert cancelled == [state.session_id]
    widget.shutdown()


def test_a_real_escape_keypress_over_the_composer_cancels_the_turn(qapp, monkeypatch):
    """Driven through the real `QShortcut`, not a direct call to `_on_
    escape_pressed` — same discipline as `test_ui_panel.py::test_escape_
    closes_settings_when_it_is_open`'s own comment: a shortcut with the
    wrong context or key would pass every other test here and still leave
    the artist stuck for real. Key-clicked on the composer's own text
    edit specifically — "над панелью" (over the panel) has to include the
    input field, the most likely place a hand actually is when reaching
    for Escape."""
    from houdini_agent_panel.ui.qt import QtCore
    from PySide6 import QtTest

    widget, client, state, calls = _live_widget(qapp, monkeypatch)
    widget.show()
    widget.activateWindow()
    cancelled: list[str] = []
    monkeypatch.setattr(client, "cancel", lambda sid: cancelled.append(sid))
    widget._on_submitted([{"type": "text", "text": "long task"}])
    widget._composer._text_edit.setFocus()
    qapp.processEvents()

    QtTest.QTest.keyClick(widget._composer._text_edit, QtCore.Qt.Key_Escape)
    qapp.processEvents()

    assert cancelled == [state.session_id]
    widget.shutdown()


def test_escape_shortcut_is_disabled_when_there_is_nothing_to_cancel(qapp, monkeypatch):
    """The owner's own requirement: the panel must not swallow Escape "just
    because" — when nothing applies, the underlying `QShortcut` has to be
    OFF, or Houdini never sees the key at all."""
    widget, client, state, calls = _live_widget(qapp, monkeypatch)

    assert widget._escape_shortcut.isEnabled() is False
    widget.shutdown()


def test_escape_shortcut_rearms_once_a_turn_starts_and_disarms_once_it_ends(qapp, monkeypatch):
    widget, client, state, calls = _live_widget(qapp, monkeypatch)
    assert widget._escape_shortcut.isEnabled() is False

    widget._on_submitted([{"type": "text", "text": "go"}])
    assert widget._escape_shortcut.isEnabled() is True

    client.turn_finished.emit(state.session_id, "end_turn")
    assert widget._escape_shortcut.isEnabled() is False
    widget.shutdown()


def test_escape_pressed_with_nothing_to_do_does_not_touch_anything(qapp, monkeypatch):
    """Defensive: even if `_on_escape_pressed` somehow ran anyway (the
    shortcut being enabled is only a fast-path guard, per its own
    docstring), each branch re-checks its own condition — nothing happens."""
    widget, client, state, calls = _live_widget(qapp, monkeypatch)
    cancelled: list[str] = []
    monkeypatch.setattr(client, "cancel", lambda sid: cancelled.append(sid))

    widget._on_escape_pressed()

    assert cancelled == []
    widget.shutdown()


def test_a_second_escape_while_already_cancelling_does_not_break_anything(qapp, monkeypatch):
    """`_on_cancelled` (the Stop button's own handler, reused unchanged —
    no second cancellation mechanism was added) has no dedup against being
    called twice: it never did, a double-click on Stop already sends
    `session/cancel` twice today. Escape inherits exactly that, not
    something worse — the second call is harmless (`session/cancel` is a
    notification the agent already handles), busy stays True, and the
    session is not left in some third, broken state."""
    widget, client, state, calls = _live_widget(qapp, monkeypatch)
    cancelled: list[str] = []
    monkeypatch.setattr(client, "cancel", lambda sid: cancelled.append(sid))
    widget._on_submitted([{"type": "text", "text": "go"}])

    widget._on_escape_pressed()
    widget._on_escape_pressed()

    assert cancelled == [state.session_id, state.session_id]
    assert state.busy is True, "still waiting on the agent to actually end the turn"
    widget.shutdown()


# --- the queue: kept, not cancelled -----------------------------------------


def test_escape_keeps_the_queue_and_only_cancels_the_current_turn(qapp, monkeypatch):
    """`_on_cancelled` already decided this (its own note to the artist) —
    Escape reuses that path unchanged, so the same rule applies: the queue
    is a separate thing from the turn Escape just stopped, removed only by
    the Remove button."""
    widget, client, state, calls = _live_widget(qapp, monkeypatch)
    monkeypatch.setattr(client, "cancel", lambda sid: None)
    widget._on_submitted([{"type": "text", "text": "first"}])
    widget._on_enqueue_requested([{"type": "text", "text": "second"}])

    widget._on_escape_pressed()

    assert len(state.queued) == 1
    assert state.queued[0].blocks[0]["text"] == "second"
    widget.shutdown()


# --- priority: popups close instead of cancelling ---------------------------


def test_escape_closes_the_slash_popup_instead_of_cancelling(qapp, monkeypatch):
    widget, client, state, calls = _live_widget(qapp, monkeypatch)
    cancelled: list[str] = []
    monkeypatch.setattr(client, "cancel", lambda sid: cancelled.append(sid))
    widget._on_submitted([{"type": "text", "text": "go"}])  # turn running
    widget._composer.set_commands([_command("model"), _command("mode")])
    widget._composer._text_edit.setPlainText("/mo")
    qapp.processEvents()
    assert widget._composer.is_popup_active()

    widget._on_escape_pressed()

    assert not widget._composer.is_popup_active(), "the popup must close first"
    assert cancelled == [], "the turn must not be cancelled while a popup is up"
    widget.shutdown()


def test_escape_closes_settings_instead_of_cancelling(qapp, monkeypatch):
    widget, client, state, calls = _live_widget(qapp, monkeypatch)
    cancelled: list[str] = []
    monkeypatch.setattr(client, "cancel", lambda sid: cancelled.append(sid))
    widget._on_submitted([{"type": "text", "text": "go"}])
    widget._show_page(widget.PAGE_SETTINGS)

    widget._on_escape_pressed()

    assert widget._pages.currentIndex() == widget.PAGE_TRANSCRIPT
    assert cancelled == [], "the turn must not be cancelled while Settings is open"
    widget.shutdown()


def test_escape_over_the_mode_chip_dropdown_does_not_cancel_the_turn(qapp, monkeypatch):
    """The mode/model/agent pickers (`ChoiceButton`) render their own
    `Qt.Popup` top-level window (`ui/chips.py`'s own module docstring) —
    NOT a child widget of the panel. On a real desktop, showing a `Qt.
    Popup` grabs the keyboard, which is exactly what keeps the panel's
    own `WidgetWithChildrenShortcut` from firing while it's open (that
    context only matches while the panel or a focused DESCENDANT holds
    focus, and the grab moves focus OUT of that subtree). Measured
    (`docs/facts/acp-sdk.md`-style, not assumed): the offscreen QPA
    platform this suite runs under does not actually support grabbing the
    keyboard ("This plugin does not support grabbing the keyboard",
    printed by this exact test) — `popup.show()` alone leaves
    `QApplication.focusWidget()` still pointing at whatever the panel had
    before, and the test flaked exactly the way that predicts (the popup
    "open" but the shortcut still firing). The explicit `setFocus()`
    below is the offscreen-only compensation for that one platform gap,
    confirmed to reproduce the real desktop's own focus-ownership by hand
    before adding it here — nothing about the PANEL's own logic needed
    changing to make this deterministic.
    """
    from houdini_agent_panel.sessions import SessionMode

    widget, client, state, calls = _live_widget(qapp, monkeypatch)
    widget.resize(900, 700)
    widget.show()
    qapp.processEvents()
    cancelled: list[str] = []
    monkeypatch.setattr(client, "cancel", lambda sid: cancelled.append(sid))
    widget._on_submitted([{"type": "text", "text": "go"}])  # turn running
    widget._composer.set_modes(
        [SessionMode(id="code", name="Code"), SessionMode(id="plan", name="Plan")], "code"
    )
    qapp.processEvents()

    combo = widget._composer.mode_chip._combo
    combo._toggle_popup()
    qapp.processEvents()
    popup = combo._popup
    assert popup is not None and popup.isVisible()
    # Offscreen-only compensation — see the docstring above.
    popup.setFocus()
    qapp.processEvents()

    from houdini_agent_panel.ui.qt import QtCore
    from PySide6 import QtTest

    QtTest.QTest.keyClick(popup, QtCore.Qt.Key_Escape)
    qapp.processEvents()

    # `popup` itself, not `combo._popup` — `ChoiceButton.eventFilter` frees
    # the attribute (`_release_popup`, via a queued `QTimer.singleShot(0,
    # ...)`) the moment it hides, and the `processEvents()` above already
    # ran that timer.
    assert not popup.isVisible(), "the dropdown's own native Escape handling must still work"

    assert cancelled == [], "the running turn must not have been cancelled by the same keypress"
    widget.shutdown()


def test_escape_answers_a_pending_permission_as_cancelled_instead_of_cancelling_the_turn(
    qapp, monkeypatch
):
    """There is no "just close" for a permission request — the agent is
    genuinely waiting on `session/request_permission`. The protocol has a
    name for exactly this (`docs/facts/acp-sdk.md` §4): `DeniedOutcome(
    outcome="cancelled")`, sent here through the same path a real button
    click already takes (`answer_permission(key, None)`)."""
    widget, client, state, calls = _live_widget(qapp, monkeypatch)
    widget.resize(900, 700)
    widget.show()
    qapp.processEvents()
    turn_cancelled: list[str] = []
    monkeypatch.setattr(client, "cancel", lambda sid: turn_cancelled.append(sid))
    answered: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        client, "answer_permission", lambda key, option: answered.append((key, option))
    )
    widget._on_submitted([{"type": "text", "text": "go"}])

    class _Option:
        def __init__(self, option_id, name, kind):
            self.option_id, self.name, self.kind = option_id, name, kind

    client.permission_requested.emit(
        "req-1", state.session_id, object(), [_Option("allow_once", "Allow once", "allow_once")]
    )
    qapp.processEvents()
    assert widget._permission_popover is not None

    widget._on_escape_pressed()

    assert answered == [("req-1", None)]
    assert widget._permission_popover is None
    assert turn_cancelled == [], "the turn is not what Escape acted on here"
    widget.shutdown()


def _command(name: str):
    from houdini_agent_panel.sessions import AvailableCommand

    return AvailableCommand(name=name, description="")

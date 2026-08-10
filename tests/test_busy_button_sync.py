"""The send/stop button must always agree with the session it's showing.

Investigated after a report ("сломалась кнопка стоп... снова висит
'отправить', а не 'стоп'") pointing at `AgentPanel._dispatch_prompt`'s
`_is_current` gate — the worry was a prompt going out for a session whose id
no longer matches what the tab considers current at that moment (a restored
conversation adopting a real session, or the queue-batch send from
`_drain_queue`, which can dispatch for a session that isn't even on screen).

Every one of those paths is covered below and all of them already agree with
what's on screen on current `main` — `_show_session`/`_set_current_session`
re-reads `state.busy` fresh on every switch, so a session that started busy
in the background still shows "stop" the moment the artist switches into it.
No fix landed here because none of this was found broken; this closes the
coverage gap the report surfaced, the same way `test_ui_attachments.py`'s
persist round-trip did for a different report.
"""

from __future__ import annotations

import pytest

from houdini_agent_panel import conversations_store as store
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


def _stored(title: str, text: str, cwd: str = "/tmp") -> store.StoredConversation:
    conversation = store.StoredConversation.new(title=title, cwd=cwd)
    conversation.entries = [{"kind": "user", "id": "e1", "text": text}]
    return conversation


def test_stop_button_shows_after_sending_into_a_restored_conversation(qapp, monkeypatch):
    """The scenario the report's own hypothesis named: a tab's current
    session is still `restored:<id>` when `_on_submitted` runs, a real
    session comes up under it, and the pending prompt is re-dispatched.
    `_on_session_started` switches the tab onto the real id BEFORE
    replaying the pending prompt, so `_is_current` sees the right id by
    the time `_dispatch_prompt` checks it."""
    conversation = _stored("Rotor pyro", "make dust")
    store.save([conversation])

    widget = panel_mod.AgentPanel()
    widget._restore_conversations()
    qapp.processEvents()
    widget._set_current_session(panel_mod._RESTORED_PREFIX + conversation.id)
    monkeypatch.setattr(widget, "_start_new_session", lambda: None)
    client = panel_mod.shared_client(widget._agent_id)
    calls = []
    monkeypatch.setattr(client, "prompt", lambda sid, blocks: calls.append((sid, blocks)))

    widget._on_submitted([{"type": "text", "text": "and more dust"}])
    live = _state("live-1")
    client.session_started.emit("live-1", live)
    qapp.processEvents()

    assert calls, "the pending prompt must actually have been sent"
    assert widget._pool.get("live-1").busy is True
    assert widget._composer._busy is True
    assert widget._composer._send_button.text() == "■"
    widget.shutdown()


def test_stop_button_shows_again_when_the_drained_queue_dispatches(qapp, monkeypatch):
    """`_drain_queue`'s own `_dispatch_prompt` call, for the still-visible
    session that just finished its previous turn."""
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    client = panel_mod.shared_client(widget._agent_id)
    state = _state()
    client.session_started.emit(state.session_id, state)
    qapp.processEvents()
    calls = []
    monkeypatch.setattr(client, "prompt", lambda sid, blocks: calls.append((sid, blocks)))

    widget._on_submitted([{"type": "text", "text": "first"}])
    assert widget._composer._busy is True
    widget._on_enqueue_requested([{"type": "text", "text": "second"}])

    client.turn_finished.emit(state.session_id, "end_turn")
    qapp.processEvents()

    assert len(calls) == 2, "the queued batch must have gone out as its own turn"
    assert widget._composer._busy is True, "the drained queue's turn must show stop too"
    assert widget._composer._send_button.text() == "■"
    widget.shutdown()


def test_stop_button_does_not_stick_if_the_tab_switched_away_mid_turn(qapp, monkeypatch):
    """A turn finishes on session A while the tab is showing session B. B's
    button must not be affected by A's turn, and switching back to A
    afterward must show the correct (already finished) state."""
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    client = panel_mod.shared_client(widget._agent_id)
    state_a = _state("a")
    client.session_started.emit("a", state_a)
    qapp.processEvents()
    monkeypatch.setattr(client, "prompt", lambda sid, blocks: None)
    widget._on_submitted([{"type": "text", "text": "go"}])
    assert widget._composer._busy is True

    state_b = _state("b")
    client.session_started.emit("b", state_b)
    qapp.processEvents()
    # `_on_session_started` already switches the tab onto "b".
    assert widget._current_session().session_id == "b"
    assert widget._composer._busy is False, "must reflect b's own (idle) state, not a's"

    client.turn_finished.emit("a", "end_turn")
    qapp.processEvents()
    assert widget._composer._busy is False, "a's turn finishing must not touch b's button"

    widget._set_current_session("a")
    qapp.processEvents()
    assert widget._composer._busy is False, "switching back to a must show its real, finished state"
    widget.shutdown()


def test_switching_into_a_session_that_started_busy_in_the_background_shows_stop(
    qapp, monkeypatch
):
    """A's turn is dispatched via `_drain_queue` while B is the visible
    tab — the one path where `_dispatch_prompt`'s own `set_busy(True)`
    never touches the composer at all, by its own docstring ("may be
    draining a session that isn't even the one on screen"). Switching TO A
    while it's still running must still show stop, read fresh off
    `state.busy` by `_show_session`."""
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    client = panel_mod.shared_client(widget._agent_id)
    state_a = _state("a")
    client.session_started.emit("a", state_a)
    qapp.processEvents()
    monkeypatch.setattr(client, "prompt", lambda sid, blocks: None)

    widget._on_submitted([{"type": "text", "text": "first"}])
    widget._on_enqueue_requested([{"type": "text", "text": "second"}])

    state_b = _state("b")
    client.session_started.emit("b", state_b)
    qapp.processEvents()
    assert widget._current_session().session_id == "b"

    # a's turn finishes while b is showing -> _drain_queue fires the queued
    # batch as a's next turn, entirely in the background.
    client.turn_finished.emit("a", "end_turn")
    qapp.processEvents()
    assert widget._pool.get("a").busy is True, "the drained batch must be a's new turn"
    assert widget._composer._busy is False, "b is showing, b is idle — correct so far"

    widget._set_current_session("a")
    qapp.processEvents()

    assert widget._composer._busy is True, "switching into a running turn must show stop"
    assert widget._composer._send_button.text() == "■"
    widget.shutdown()

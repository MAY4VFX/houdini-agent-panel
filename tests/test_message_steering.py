"""A message queued while the agent is busy used to sit at "Queued —
waiting to send" through the rest of that turn, no matter how many more
tool calls the agent made — `_drain_queue` (tests/test_message_queue.py)
only ever sends everything queued once the turn is fully over. The owner's
own report, verbatim: a message stuck "Queued" while the agent worked
through five more tool calls and started answering, proving the batch-at-
end-of-turn design never actually interjects mid-turn.

`claude-agent-acp` carries its own extension for exactly this,
`_session/steering`, measured live against a real agent process
(docs/facts/acp-sdk.md §31) before anything here was built: inject a
message into the turn that is currently running, instead of waiting for it
to end. These tests exercise `ui/panel.py`'s own decision logic — steer if
the agent advertises support and a turn is running, otherwise fall back to
the unchanged queueing path from test_message_queue.py — using a real
`AcpClient` object with `prompt`/`steer` monkeypatched to record calls
instead of actually talking to a subprocess, the same pattern
test_message_queue.py itself already uses. The wire contract those two
outcomes assume (`"injected"` / `"prompt_required"`) is verified for real,
against a real agent process, in tests/test_client.py's own steering tests
and in §31.
"""

from __future__ import annotations

import pytest

from houdini_agent_panel import sessions
from houdini_agent_panel.client import AgentInfo
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


#: An `AgentInfo` that advertises `_session/steering` — everything else at
#: the same defaults `test_agent_switch.py` already uses for a bare info.
def _steering_info() -> AgentInfo:
    return AgentInfo(
        name="claude-acp", version="0.66.0", protocol_version=1,
        supports_image=False, supports_audio=False, supports_embedded_context=False,
        supports_load_session=False, supports_logout=False, auth_methods=(),
        supports_steering=True,
    )


def _live_widget(qapp, monkeypatch, *, steering: bool, session_id: str = "s1"):
    """Same shape as test_message_queue.py's own `_live_widget`, plus a
    connected `AgentInfo` (steering support on or off) and `steer`
    recorded the same way `prompt` already is."""
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    client = panel_mod.shared_client(widget._agent_id)
    if steering:
        # Same pattern as test_busy_button_sync.py::test_stop_button_shows_
        # after_a_real_session_load_resume — `agent_info()` reads this
        # private attribute directly; nothing in this test needs a full
        # `connected` round trip.
        client._agent_info = _steering_info()
    state = _state(session_id)
    client.session_started.emit(state.session_id, state)
    qapp.processEvents()
    prompt_calls: list[tuple[str, list]] = []
    steer_calls: list[tuple[str, str, list]] = []
    monkeypatch.setattr(client, "prompt", lambda sid, blocks: prompt_calls.append((sid, blocks)))
    monkeypatch.setattr(
        client, "steer", lambda sid, entry_id, blocks: steer_calls.append((sid, entry_id, blocks))
    )
    return widget, client, state, prompt_calls, steer_calls


def _text_of(blocks: list[dict]) -> str:
    return " ".join(b.get("text", "") for b in blocks if b.get("type") == "text")


# --- deciding whether to even try -------------------------------------------


def test_steering_is_attempted_when_supported_and_busy(qapp, monkeypatch):
    widget, client, state, prompt_calls, steer_calls = _live_widget(qapp, monkeypatch, steering=True)
    widget._on_submitted([{"type": "text", "text": "first"}])
    assert len(prompt_calls) == 1

    widget._on_enqueue_requested([{"type": "text", "text": "second thought"}])

    assert len(prompt_calls) == 1, "steering must never send a second session/prompt"
    assert len(steer_calls) == 1
    assert steer_calls[0][0] == state.session_id
    assert _text_of(steer_calls[0][2]) == "second thought"
    # Still queued too — the safety net `_on_steered` falls back on.
    assert len(state.queued) == 1
    widget.shutdown()


def test_steering_is_never_attempted_when_the_agent_does_not_advertise_it(qapp, monkeypatch):
    """The exact test_message_queue.py behavior, byte for byte — an agent
    that never advertises `steering.supported` must see no change at all
    ("the agent doesn't support it — the control doesn't get drawn")."""
    widget, client, state, prompt_calls, steer_calls = _live_widget(qapp, monkeypatch, steering=False)
    widget._on_submitted([{"type": "text", "text": "first"}])
    widget._on_enqueue_requested([{"type": "text", "text": "second thought"}])

    assert steer_calls == []
    assert len(state.queued) == 1
    assert state.queued[0].blocks[0]["text"] == "second thought"
    widget.shutdown()


def test_steering_is_never_attempted_when_nothing_is_running(qapp, monkeypatch):
    """`_on_enqueue_requested` is only ever reached while busy — but
    `_can_steer` checks `state.busy` itself too, defensively, rather than
    trust the caller."""
    widget, client, state, prompt_calls, steer_calls = _live_widget(qapp, monkeypatch, steering=True)
    assert widget._can_steer(state) is False


# --- outcome: injected -------------------------------------------------------


def test_injected_promotes_the_queued_row_immediately_without_waiting_for_turn_finished(
    qapp, monkeypatch
):
    widget, client, state, prompt_calls, steer_calls = _live_widget(qapp, monkeypatch, steering=True)
    widget._on_submitted([{"type": "text", "text": "first"}])
    widget._on_enqueue_requested([{"type": "text", "text": "second"}])
    entry_id = steer_calls[0][1]
    entry = next(e for e in widget._model(state.session_id).entries() if e.id == entry_id)
    assert entry.kind == "queued"

    client.steered.emit(state.session_id, entry_id, "injected")

    entry = next(e for e in widget._model(state.session_id).entries() if e.id == entry_id)
    assert entry.kind == "user", "an injected message must read as sent right away"
    assert state.queued == [], "no longer waiting — it already went out"
    # And no second session/prompt was ever sent for it.
    assert len(prompt_calls) == 1

    # A LATER turn_finished for the original turn must not resend it.
    client.turn_finished.emit(state.session_id, "end_turn")
    assert len(prompt_calls) == 1
    widget.shutdown()


def test_injected_recreates_the_row_if_a_stale_remove_already_deleted_it(qapp, monkeypatch, caplog):
    """The one new race steering creates on top of test_message_queue.py's
    own stale-Remove case: Remove is clicked in the narrow window between
    the steer request going out and its `injected` answer coming back. The
    message was already, genuinely delivered — it cannot be unsent — so the
    feed must still end up saying "sent", the same principle
    test_message_queue.py's own recreate test pins for the ordinary drain
    path."""
    import logging

    caplog.set_level(logging.INFO, logger="houdini_agent_panel.ui.panel")
    widget, client, state, prompt_calls, steer_calls = _live_widget(qapp, monkeypatch, steering=True)
    widget._on_submitted([{"type": "text", "text": "first"}])
    widget._on_enqueue_requested([{"type": "text", "text": "second"}])
    entry_id = steer_calls[0][1]

    # The artist clicks Remove before the steer's answer lands.
    widget._on_queue_remove_requested(entry_id)
    assert state.queued == []
    assert all(e.id != entry_id for e in widget._model(state.session_id).entries())

    # ...but the agent had already accepted it.
    client.steered.emit(state.session_id, entry_id, "injected")

    recreated = next(e for e in widget._model(state.session_id).entries() if e.text == "second")
    assert recreated.kind == "user"
    assert recreated.id != entry_id, "a fresh row, not a resurrection of the removed one"
    messages = [r.getMessage() for r in caplog.records]
    assert any("no matching row" in m for m in messages), messages
    widget.shutdown()


# --- outcome: prompt_required / failed --------------------------------------


def test_prompt_required_falls_back_to_the_ordinary_queue_and_drains_at_turn_finished(
    qapp, monkeypatch
):
    """No turn was actually running by the time the steer reached the
    agent (docs/facts/acp-sdk.md §31's own "can happen mid-turn" finding) —
    the row stays exactly as `_on_enqueue_requested` left it, and the
    unchanged `_drain_queue` path sends it, same as if steering had never
    been attempted."""
    widget, client, state, prompt_calls, steer_calls = _live_widget(qapp, monkeypatch, steering=True)
    widget._on_submitted([{"type": "text", "text": "first"}])
    widget._on_enqueue_requested([{"type": "text", "text": "second"}])
    entry_id = steer_calls[0][1]

    client.steered.emit(state.session_id, entry_id, "prompt_required")

    # Still busy by our own bookkeeping (no turn_finished happened) — must
    # not resend early.
    assert len(prompt_calls) == 1
    entry = next(e for e in widget._model(state.session_id).entries() if e.id == entry_id)
    assert entry.kind == "queued"
    assert len(state.queued) == 1

    client.turn_finished.emit(state.session_id, "end_turn")

    assert len(prompt_calls) == 2
    assert _text_of(prompt_calls[1][1]) == "second"
    assert state.queued == []
    widget.shutdown()


def test_late_prompt_required_after_turn_finished_already_drained_does_not_resend(qapp, monkeypatch):
    """The other half of the same race: `turn_finished` for the original
    turn arrives before the steer's own `prompt_required` answer does.
    `_on_turn_finished` already calls `_drain_queue` unconditionally
    (test_message_queue.py's own behavior, unchanged) — the still-queued
    row (steering never removes it except on `"injected"`) goes out
    immediately as part of that, busy flips back to True for the resend,
    and the LATE `prompt_required` — for a message that has, by the time it
    arrives, already been sent as part of an ordinary drain — must not
    trigger a second, duplicate send."""
    widget, client, state, prompt_calls, steer_calls = _live_widget(qapp, monkeypatch, steering=True)
    widget._on_submitted([{"type": "text", "text": "first"}])
    widget._on_enqueue_requested([{"type": "text", "text": "second"}])
    entry_id = steer_calls[0][1]

    client.turn_finished.emit(state.session_id, "end_turn")
    assert len(prompt_calls) == 2, "the ordinary drain must not wait on the steer's own outcome"
    assert _text_of(prompt_calls[1][1]) == "second"
    assert state.queued == []
    assert state.busy is True, "the drained resend is itself a new turn"

    client.steered.emit(state.session_id, entry_id, "prompt_required")

    assert len(prompt_calls) == 2, "a late prompt_required must never cause a duplicate send"
    widget.shutdown()


def test_failed_steer_falls_back_the_same_way_as_prompt_required(qapp, monkeypatch):
    widget, client, state, prompt_calls, steer_calls = _live_widget(qapp, monkeypatch, steering=True)
    widget._on_submitted([{"type": "text", "text": "first"}])
    widget._on_enqueue_requested([{"type": "text", "text": "second"}])
    entry_id = steer_calls[0][1]

    client.steered.emit(state.session_id, entry_id, "failed")
    client.turn_finished.emit(state.session_id, "end_turn")

    assert len(prompt_calls) == 2
    assert _text_of(prompt_calls[1][1]) == "second"
    widget.shutdown()


# --- no third behavior -------------------------------------------------------


def test_multiple_queued_messages_each_steer_independently(qapp, monkeypatch):
    """No batching for steering — each message typed while busy gets its
    own immediate `_session/steering` attempt, unlike the drain path's
    single combined call (test_message_queue.py's own batching test)."""
    widget, client, state, prompt_calls, steer_calls = _live_widget(qapp, monkeypatch, steering=True)
    widget._on_submitted([{"type": "text", "text": "first"}])
    widget._on_enqueue_requested([{"type": "text", "text": "second"}])
    widget._on_enqueue_requested([{"type": "text", "text": "third"}])

    assert len(steer_calls) == 2
    assert _text_of(steer_calls[0][2]) == "second"
    assert _text_of(steer_calls[1][2]) == "third"

    client.steered.emit(state.session_id, steer_calls[0][1], "injected")
    client.steered.emit(state.session_id, steer_calls[1][1], "injected")

    assert state.queued == []
    for text in ("second", "third"):
        entry = next(e for e in widget._model(state.session_id).entries() if e.text == text)
        assert entry.kind == "user"
    assert len(prompt_calls) == 1, "steering must never trigger a second session/prompt"
    widget.shutdown()

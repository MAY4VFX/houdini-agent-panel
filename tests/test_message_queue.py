"""A thought that arrives mid-turn used to have nowhere to go.

The owner's own ask, verbatim: "надо чтобы когда агент уже работает, я мог
всё равно отправлять сообщения и они вставали в очередь и автоматом
отправлялись, когда он прерывается между тасками" — while the agent is
already working, typing and sending should queue the message and send it
automatically once the current turn ends, not refuse it outright.

Measured first, before building anything (see the queueing commit): nothing
in the ACP SDK or transport serializes concurrent `session/prompt` calls for
one session — sending two before the first's `turn_finished` arrives, even
against the real SDK's own fake agent, produced chunks from both turns
landing under the same message id and interleaving into one garbled entry,
with `turn_finished` arriving out of order relative to content still in
flight. So the queue is not a UX nicety sitting on top of an SDK that would
have been fine either way — it is the only thing standing between "the
artist typed two things" and a corrupted transcript.
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


def _live_widget(qapp, monkeypatch, session_id: str = "s1"):
    """A panel with one live session, its outgoing prompt recorded instead
    of actually sent — same shape as test_ui_panel.py's own turn-driving
    tests: the client is real, just never started, so its signals are the
    genuine ones."""
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    client = panel_mod.shared_client(widget._agent_id)
    state = _state(session_id)
    client.session_started.emit(state.session_id, state)
    qapp.processEvents()
    calls: list[tuple[str, list]] = []
    monkeypatch.setattr(client, "prompt", lambda sid, blocks: calls.append((sid, blocks)))
    return widget, client, state, calls


def _text_of(blocks: list[dict]) -> str:
    return " ".join(b.get("text", "") for b in blocks if b.get("type") == "text")


# --- enqueue instead of refuse ---------------------------------------------


def test_a_message_typed_while_busy_is_queued_not_sent(qapp, monkeypatch):
    widget, client, state, calls = _live_widget(qapp, monkeypatch)
    widget._on_submitted([{"type": "text", "text": "first"}])
    assert len(calls) == 1

    widget._on_enqueue_requested([{"type": "text", "text": "second thought"}])

    assert len(calls) == 1, "a queued message must not be sent while a turn is running"
    assert len(state.queued) == 1
    assert state.queued[0].blocks[0]["text"] == "second thought"
    queued_entries = [e for e in widget._model(state.session_id).entries() if e.kind == "queued"]
    assert len(queued_entries) == 1
    assert queued_entries[0].text == "second thought"
    widget.shutdown()


# --- drain order: one at a time, oldest first -------------------------------


def test_queue_drains_one_message_at_a_time_in_order(qapp, monkeypatch):
    widget, client, state, calls = _live_widget(qapp, monkeypatch)
    widget._on_submitted([{"type": "text", "text": "first"}])
    widget._on_enqueue_requested([{"type": "text", "text": "second"}])
    widget._on_enqueue_requested([{"type": "text", "text": "third"}])
    assert len(calls) == 1
    assert len(state.queued) == 2

    client.turn_finished.emit(state.session_id, "end_turn")

    # Draining is never "the whole backlog at once" — each queued message
    # is its own separate turn, so only ONE more send happens here.
    assert len(calls) == 2, "only the next queued message goes out, not the whole backlog"
    assert _text_of(calls[1][1]) == "second"
    assert len(state.queued) == 1
    assert state.queued[0].blocks[0]["text"] == "third"
    entries = {e.id: e for e in widget._model(state.session_id).entries() if e.text}
    promoted = next(e for e in entries.values() if e.text == "second")
    assert promoted.kind == "user", "a drained message must stop reading as queued"
    still_waiting = next(e for e in entries.values() if e.text == "third")
    assert still_waiting.kind == "queued"

    client.turn_finished.emit(state.session_id, "end_turn")

    assert len(calls) == 3
    assert _text_of(calls[2][1]) == "third"
    assert state.queued == []

    # Nothing left to drain — a further turn end must not resend anything.
    client.turn_finished.emit(state.session_id, "end_turn")
    assert len(calls) == 3
    widget.shutdown()


def test_an_error_ending_the_turn_still_drains_the_queue(qapp, monkeypatch):
    """`turn_finished` is not the only way a turn ends. A stuck `busy` from
    an error path used to leave a queue behind it waiting forever — found
    while wiring the drain through every way a turn can end, not just the
    tidy one."""
    widget, client, state, calls = _live_widget(qapp, monkeypatch)
    widget._on_submitted([{"type": "text", "text": "first"}])
    widget._on_enqueue_requested([{"type": "text", "text": "second"}])

    client.error.emit(state.session_id, "the agent process died")

    # Draining resends immediately, so `busy` is back to True for the
    # message that just went out — the same shape as any other drain
    # (see test_queue_drains_one_message_at_a_time_in_order). What this
    # test pins is that the error path reaches that drain AT ALL, which a
    # `busy` left stuck True forever would have prevented.
    assert len(calls) == 2
    assert _text_of(calls[1][1]) == "second"
    assert state.queued == []
    widget.shutdown()


# --- removal -----------------------------------------------------------------


def test_removing_a_queued_message_takes_it_out_and_it_is_never_sent(qapp, monkeypatch):
    widget, client, state, calls = _live_widget(qapp, monkeypatch)
    widget._on_submitted([{"type": "text", "text": "first"}])
    widget._on_enqueue_requested([{"type": "text", "text": "keep"}])
    widget._on_enqueue_requested([{"type": "text", "text": "drop"}])
    drop_id = state.queued[1].id

    widget._on_queue_remove_requested(drop_id)

    assert [q.blocks[0]["text"] for q in state.queued] == ["keep"]
    assert all(e.id != drop_id for e in widget._model(state.session_id).entries())

    client.turn_finished.emit(state.session_id, "end_turn")
    assert _text_of(calls[1][1]) == "keep"
    client.turn_finished.emit(state.session_id, "end_turn")

    # "drop" must never have gone out, at any point.
    assert all(_text_of(sid_blocks[1]) != "drop" for sid_blocks in calls)
    assert len(calls) == 2
    widget.shutdown()


# --- per-conversation scoping -------------------------------------------------


def test_queue_is_scoped_to_its_own_conversation_not_the_panel(qapp, monkeypatch):
    """The owner's own constraint: a queue lives on `sessions.SessionState`
    — per conversation — never on the panel or the tab. Switching to a
    different conversation must not carry another one's still-typed words
    along, or show them as if they belonged here."""
    widget, client, state_a, calls = _live_widget(qapp, monkeypatch, session_id="a")
    widget._on_submitted([{"type": "text", "text": "a1"}])
    widget._on_enqueue_requested([{"type": "text", "text": "a2 waiting"}])

    state_b = _state("b")
    client.session_started.emit("b", state_b)
    qapp.processEvents()
    widget._set_current_session("b")

    assert state_b.queued == [], "a fresh conversation must not inherit another one's queue"
    assert not any(e.kind == "queued" for e in widget._model("b").entries())

    widget._set_current_session("a")
    assert len(state_a.queued) == 1
    assert state_a.queued[0].blocks[0]["text"] == "a2 waiting"
    assert any(
        e.kind == "queued" and e.text == "a2 waiting" for e in widget._model("a").entries()
    )
    widget.shutdown()


# --- cancel: kept, and said out loud ------------------------------------------


def test_cancelling_with_a_queue_says_so_and_keeps_it(qapp, monkeypatch):
    # Short grace period, same pattern as test_agent_switch.py::
    # test_stop_releases_the_input_even_if_the_agent_never_answers — the
    # fallback timer this arms is unparented and outlives this function's
    # own scope otherwise, ready to fire mid some LATER test.
    monkeypatch.setattr(panel_mod, "_CANCEL_GRACE_MS", 10)
    widget, client, state, calls = _live_widget(qapp, monkeypatch)
    widget._on_submitted([{"type": "text", "text": "first"}])
    widget._on_enqueue_requested([{"type": "text", "text": "second"}])
    monkeypatch.setattr(client, "cancel", lambda sid: None)  # agent stays silent

    widget._on_cancelled()

    # Checked before the grace period resolves anything: this is the
    # decision itself, not its eventual fallout.
    assert len(state.queued) == 1, "cancelling a turn must not silently drop what's queued"
    notes = [e.text for e in widget._model(state.session_id).entries() if e.kind == "error"]
    assert any("queued" in text.lower() for text in notes), (
        "what happens to the queue on cancel must be visible, not a surprise"
    )

    # Let the grace-period fallback actually fire (`_release_if_still_busy`
    # -> `_drain_queue`, since the queue is still there) rather than leaving
    # its QTimer to go off during a later, unrelated test. Waiting for the
    # drained send rather than for `busy` to go False: `_drain_queue` sets
    # it True again immediately for the message it just sent.
    from houdini_agent_panel.ui.qt import QtCore

    deadline = QtCore.QElapsedTimer()
    deadline.start()
    while deadline.elapsed() < 3000 and len(calls) < 2:
        qapp.processEvents()
        QtCore.QThread.msleep(5)
    assert len(calls) == 2, "the grace-period fallback must still drain the queue"
    widget.shutdown()


# --- durability: a hang must not lose a queued message ------------------------


def test_a_queued_message_survives_a_hang_that_never_reaches_shutdown(qapp, monkeypatch):
    """Same discipline as the conversation-loss fix this builds on
    (tests/test_persist_on_hang.py): the artist's own typed words are on
    disk the instant they exist, whether they were sent immediately or
    queued behind a turn that was still running when Houdini died."""
    widget, client, state, calls = _live_widget(qapp, monkeypatch)
    widget._on_submitted([{"type": "text", "text": "first"}])
    widget._on_enqueue_requested([{"type": "text", "text": "queued before the hang"}])
    # `_on_submitted`'s own persist opened `_persist_conversations_soon`'s
    # short coalescing cooldown (see tests/test_persist_on_hang.py), and
    # enqueueing landed inside it here — a real artist typing a follow-up
    # while a turn runs practically never does, since typing it takes
    # longer than the window. Draining it directly stands in for the real
    # `QTimer` firing, i.e. an event loop that kept turning right up to the
    # hang, exactly like the sibling test this one is modeled on.
    widget._end_persist_cooldown()

    # No `turn_finished`, no `shutdown()` — the hang lands with a message
    # still sitting in the queue.

    stored = store.load()
    assert len(stored) == 1
    texts = [e.get("text", "") for e in stored[0].entries]
    assert "queued before the hang" in texts
    kinds = {e.get("text"): e.get("kind") for e in stored[0].entries}
    assert kinds["queued before the hang"] == "queued"


# --- restore: a queue survives a restart and drains once live again -----------


def test_a_restored_queue_drains_once_the_conversation_gets_a_live_session(qapp, monkeypatch):
    conversation = store.StoredConversation.new(title="Rotor pyro", cwd="/tmp")
    conversation.entries = [
        {"kind": "user", "id": "u1", "text": "make it rain"},
        {"kind": "queued", "id": "q1", "text": "queued before crash"},
    ]
    store.save([conversation])

    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    widget._restore_conversations()

    restored_key = panel_mod._RESTORED_PREFIX + conversation.id
    restored_state = widget._pool.get(restored_key)
    assert restored_state is not None
    assert [q.id for q in restored_state.queued] == ["q1"]
    assert restored_state.queued[0].blocks == [
        {"type": "text", "text": "queued before crash"}
    ]

    widget._set_current_session(restored_key)
    monkeypatch.setattr(widget, "_start_new_session", lambda: None)
    widget._on_submitted([{"type": "text", "text": "fresh message"}])

    client = panel_mod.shared_client(widget._agent_id)
    live = sessions.SessionState(
        session_id="live-1", title="New conversation", cwd="/tmp", created_at=0.0
    )
    client.session_started.emit("live-1", live)
    qapp.processEvents()

    # The fresh message the artist just typed goes out first — it's what
    # they're doing right now — and the restored queue waits behind it,
    # exactly like any other queue behind a running turn.
    live_state = widget._pool.get("live-1")
    assert live_state.busy is True
    assert [q.id for q in live_state.queued] == ["q1"]
    restored_entry = next(e for e in widget._model("live-1").entries() if e.id == "q1")
    assert restored_entry.kind == "queued"

    client.turn_finished.emit("live-1", "end_turn")

    promoted = next(e for e in widget._model("live-1").entries() if e.id == "q1")
    assert promoted.kind == "user"
    assert live_state.queued == []
    widget.shutdown()

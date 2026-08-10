"""Arrow-key history: the owner's own asks, verbatim.

Two, on top of the queue batching in `test_message_queue.py`:

  "не забудь чтобы по стрелке вверх можно было его отредактировать" — a
  message still sitting in the queue, not yet sent, should come back into
  the field for editing on Up, exactly like clicking Remove but with the
  text handed back instead of thrown away.

  "стрелка вверх должна как в терминале работать, показывать прошлые
  сообщения" — Up should also walk back through what was actually SENT,
  oldest as far back as the conversation goes, Down walks forward again,
  and a still-unsent draft must not be lost while browsing.

The order between the two is the owner's own call, made for this feature:
a queued message is more recently TYPED than anything already sent (it's
only sitting there because the agent was busy), so the first Up press
reaches for it before ever looking at history.
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
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    client = panel_mod.shared_client(widget._agent_id)
    state = _state(session_id)
    client.session_started.emit(state.session_id, state)
    qapp.processEvents()
    calls: list[tuple[str, list]] = []
    monkeypatch.setattr(client, "prompt", lambda sid, blocks: calls.append((sid, blocks)))
    return widget, client, state, calls


# --- Task 2: Up in an empty field pulls a queued message back out ----------


def test_up_in_empty_field_recalls_the_queued_message_for_editing(qapp, monkeypatch):
    widget, client, state, calls = _live_widget(qapp, monkeypatch)
    widget._on_submitted([{"type": "text", "text": "first"}])
    widget._on_enqueue_requested([{"type": "text", "text": "second thought"}])
    assert len(state.queued) == 1

    widget._on_history_navigate(-1)

    assert widget._composer.current_text() == "second thought"
    widget.shutdown()


def test_recalling_a_queued_message_removes_it_from_the_queue_like_remove(qapp, monkeypatch):
    widget, client, state, calls = _live_widget(qapp, monkeypatch)
    widget._on_submitted([{"type": "text", "text": "first"}])
    widget._on_enqueue_requested([{"type": "text", "text": "second thought"}])

    widget._on_history_navigate(-1)

    assert state.queued == [], "recalling it is exactly Remove — it must stop waiting"
    assert not any(
        e.kind == "queued" for e in widget._model(state.session_id).entries()
    ), "its transcript row goes with it, same as Remove"
    widget.shutdown()


def test_resending_a_recalled_queued_message_queues_it_again(qapp, monkeypatch):
    """"Отправив снова, артист кладёт исправленное обратно в очередь.\""""
    widget, client, state, calls = _live_widget(qapp, monkeypatch)
    widget._on_submitted([{"type": "text", "text": "first"}])
    widget._on_enqueue_requested([{"type": "text", "text": "second thought"}])
    widget._on_history_navigate(-1)
    assert state.queued == []

    widget._on_enqueue_requested([{"type": "text", "text": "second thought, edited"}])

    assert len(state.queued) == 1
    assert state.queued[0].blocks[0]["text"] == "second thought, edited"
    widget.shutdown()


def test_up_recalls_the_most_recently_queued_message_first(qapp, monkeypatch):
    widget, client, state, calls = _live_widget(qapp, monkeypatch)
    widget._on_submitted([{"type": "text", "text": "first"}])
    widget._on_enqueue_requested([{"type": "text", "text": "second"}])
    widget._on_enqueue_requested([{"type": "text", "text": "third"}])

    widget._on_history_navigate(-1)

    assert widget._composer.current_text() == "third"
    # "second" is untouched — still queued, still going out once its turn
    # comes, this gesture only ever reaches for the ONE most recent.
    assert len(state.queued) == 1
    assert state.queued[0].blocks[0]["text"] == "second"
    widget.shutdown()


def test_up_with_no_queue_and_an_empty_field_goes_straight_to_history(qapp, monkeypatch):
    widget, client, state, calls = _live_widget(qapp, monkeypatch)
    widget._on_submitted([{"type": "text", "text": "make it rain"}])
    client.turn_finished.emit(state.session_id, "end_turn")

    widget._on_history_navigate(-1)

    assert widget._composer.current_text() == "make it rain"
    widget.shutdown()


def test_up_from_a_nonempty_field_skips_the_queue_and_goes_to_history(qapp, monkeypatch):
    """The queue-recall gesture is specifically an EMPTY-field one (the
    owner's own words) — an artist already mid-edit of something else gets
    plain history instead."""
    widget, client, state, calls = _live_widget(qapp, monkeypatch)
    widget._on_submitted([{"type": "text", "text": "make it rain"}])
    widget._on_enqueue_requested([{"type": "text", "text": "queued"}])
    widget._composer._text_edit.setPlainText("something I'm already typing")

    widget._on_history_navigate(-1)

    assert widget._composer.current_text() == "make it rain"
    assert len(state.queued) == 1, "the queued message must be left untouched"
    widget.shutdown()


# --- Task 3: Up/Down walk sent-message history, newest first ---------------


def test_up_twice_walks_further_back_through_sent_messages(qapp, monkeypatch):
    widget, client, state, calls = _live_widget(qapp, monkeypatch)
    widget._on_submitted([{"type": "text", "text": "one"}])
    client.turn_finished.emit(state.session_id, "end_turn")
    widget._on_submitted([{"type": "text", "text": "two"}])
    client.turn_finished.emit(state.session_id, "end_turn")

    widget._on_history_navigate(-1)
    assert widget._composer.current_text() == "two"
    widget._on_history_navigate(-1)
    assert widget._composer.current_text() == "one"
    widget.shutdown()


def test_up_past_the_oldest_message_stays_put(qapp, monkeypatch):
    widget, client, state, calls = _live_widget(qapp, monkeypatch)
    widget._on_submitted([{"type": "text", "text": "only one"}])
    client.turn_finished.emit(state.session_id, "end_turn")

    widget._on_history_navigate(-1)
    widget._on_history_navigate(-1)  # nothing further back — must not error

    assert widget._composer.current_text() == "only one"
    widget.shutdown()


def test_down_walks_forward_through_history_again(qapp, monkeypatch):
    widget, client, state, calls = _live_widget(qapp, monkeypatch)
    widget._on_submitted([{"type": "text", "text": "one"}])
    client.turn_finished.emit(state.session_id, "end_turn")
    widget._on_submitted([{"type": "text", "text": "two"}])
    client.turn_finished.emit(state.session_id, "end_turn")

    widget._on_history_navigate(-1)  # -> "two"
    widget._on_history_navigate(-1)  # -> "one"
    widget._on_history_navigate(1)  # -> back to "two"

    assert widget._composer.current_text() == "two"
    widget.shutdown()


def test_down_from_the_newest_history_entry_restores_the_draft(qapp, monkeypatch):
    widget, client, state, calls = _live_widget(qapp, monkeypatch)
    widget._on_submitted([{"type": "text", "text": "one"}])
    client.turn_finished.emit(state.session_id, "end_turn")
    widget._composer._text_edit.setPlainText("half-typed draft")

    widget._on_history_navigate(-1)
    assert widget._composer.current_text() == "one"

    widget._on_history_navigate(1)

    assert widget._composer.current_text() == "half-typed draft"
    widget.shutdown()


def test_down_with_nothing_browsed_yet_does_nothing(qapp, monkeypatch):
    widget, client, state, calls = _live_widget(qapp, monkeypatch)
    widget._on_submitted([{"type": "text", "text": "one"}])
    client.turn_finished.emit(state.session_id, "end_turn")

    widget._on_history_navigate(1)  # Down, never having pressed Up

    assert widget._composer.current_text() == ""
    widget.shutdown()


def test_a_send_resets_history_browsing_to_the_newest_message(qapp, monkeypatch):
    widget, client, state, calls = _live_widget(qapp, monkeypatch)
    widget._on_submitted([{"type": "text", "text": "one"}])
    client.turn_finished.emit(state.session_id, "end_turn")
    widget._on_history_navigate(-1)
    assert widget._composer.current_text() == "one"

    widget._composer.show_history_text("")  # clear, as a real send would
    widget._on_submitted([{"type": "text", "text": "two"}])
    client.turn_finished.emit(state.session_id, "end_turn")

    widget._on_history_navigate(-1)

    assert widget._composer.current_text() == "two", (
        "browsing must restart from the newest message, not resume mid-walk"
    )
    widget.shutdown()


# --- ordering: queue, then history, in one continuous walk -----------------


def test_up_walks_the_queued_message_then_continues_into_sent_history(qapp, monkeypatch):
    widget, client, state, calls = _live_widget(qapp, monkeypatch)
    widget._on_submitted([{"type": "text", "text": "sent earlier"}])
    client.turn_finished.emit(state.session_id, "end_turn")
    widget._on_submitted([{"type": "text", "text": "sent, now busy"}])
    widget._on_enqueue_requested([{"type": "text", "text": "queued thought"}])

    widget._on_history_navigate(-1)
    assert widget._composer.current_text() == "queued thought"
    widget._on_history_navigate(-1)
    assert widget._composer.current_text() == "sent, now busy"
    widget._on_history_navigate(-1)
    assert widget._composer.current_text() == "sent earlier"
    widget.shutdown()


# --- per-conversation scoping: switching tabs resets browsing --------------


def test_switching_conversations_resets_history_browsing(qapp, monkeypatch):
    widget, client, state_a, calls = _live_widget(qapp, monkeypatch, session_id="a")
    widget._on_submitted([{"type": "text", "text": "a message"}])
    client.turn_finished.emit(state_a.session_id, "end_turn")
    widget._on_history_navigate(-1)
    assert widget._composer.current_text() == "a message"

    state_b = _state("b")
    client.session_started.emit("b", state_b)
    qapp.processEvents()
    widget._set_current_session("b")

    # `_show_session` clears the field via nothing of ours — but browsing
    # state itself must not still be pointed at conversation "a"'s history.
    assert widget._history_index == -1
    widget.shutdown()


# --- durability: history is read straight off the persisted transcript ----


def test_history_survives_a_restart_via_the_restored_transcript(qapp, monkeypatch):
    """No second store: a restored conversation's sent messages are exactly
    what `conversations_store.py` already wrote to disk (`load_records`),
    so arrow-key history works for it the same as a live session."""
    conversation = store.StoredConversation.new(title="Rotor pyro", cwd="/tmp")
    conversation.entries = [
        {"kind": "user", "id": "u1", "text": "first ever message"},
        {"kind": "user", "id": "u2", "text": "second ever message"},
    ]
    store.save([conversation])

    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    widget._restore_conversations()
    restored_key = panel_mod._RESTORED_PREFIX + conversation.id
    widget._set_current_session(restored_key)

    widget._on_history_navigate(-1)
    assert widget._composer.current_text() == "second ever message"
    widget._on_history_navigate(-1)
    assert widget._composer.current_text() == "first ever message"
    widget.shutdown()

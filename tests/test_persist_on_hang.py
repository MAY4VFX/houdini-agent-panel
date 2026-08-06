"""A hang, not a close, is what actually loses a conversation.

The real incident: an artist sent a prompt, the agent was still mid-turn,
Houdini hung, and the artist force-restarted it. `shutdown()` — the only
place besides an agent switch that ever wrote `conversations.json` — never
ran, so the conversation had never touched disk at all. `panel.log` showed
the prompt going out and then nothing for 21 minutes until a fresh
`--- panel start ---`; the store's newest `updated_at` predated the prompt.

These tests simulate exactly that: a prompt (and, separately, a finished
turn) with the panel destroyed or abandoned *without* `shutdown()` ever
being called — the shape of a hard kill, not a clean close — and assert the
conversation is on disk anyway.
"""

from __future__ import annotations

import logging

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


def _session(session_id: str = "s1") -> sessions.SessionState:
    return sessions.SessionState(
        session_id=session_id, title="New conversation", cwd="/tmp", created_at=0.0
    )


def _live_widget(qapp, monkeypatch):
    """A panel with one live session, its outgoing prompt swallowed rather
    than actually sent — same shape as `test_turn_drives_activity_burst_
    tool_reset_and_completion` in test_ui_panel.py: the client is real, just
    never started, so its signals are the genuine ones."""
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    client = panel_mod.shared_client(widget._agent_id)
    state = _session()
    client.session_started.emit(state.session_id, state)
    qapp.processEvents()
    monkeypatch.setattr(client, "prompt", lambda _session_id, _blocks: None)
    return widget, client, state


def test_a_prompt_sent_survives_a_hang_that_never_reaches_shutdown(qapp, monkeypatch):
    """The exact failure: prompt out, turn never finishes, no clean close."""
    widget, client, state = _live_widget(qapp, monkeypatch)

    widget._on_submitted([{"type": "text", "text": "и она отвисла"}])

    # No `client.turn_finished.emit(...)`, no `widget.shutdown()` — the turn
    # is still "in flight", exactly the shape of a hang: the only thing
    # that ran is what a real Houdini process running normally would also
    # have run before a hard kill.

    stored = store.load()
    assert len(stored) == 1
    assert stored[0].entries[-1]["text"] == "и она отвисла"


def test_the_agents_answer_survives_a_hang_after_the_turn_finishes(qapp, monkeypatch):
    """Not just the prompt — losing a finished answer costs real minutes of
    agent work too, and a hang can just as easily land right after a turn
    completes as while one is in flight."""
    widget, client, state = _live_widget(qapp, monkeypatch)
    widget._on_submitted([{"type": "text", "text": "почини материал"}])
    client.message_chunk.emit(state.session_id, "m1", "готово, поправил шейдер")
    client.turn_finished.emit(state.session_id, "end_turn")
    qapp.processEvents()
    # The prompt's own persist (from `_on_submitted`) opened a short
    # coalescing cooldown (`_persist_conversations_soon`'s docstring), and
    # the turn finished well inside it here — a real agent round trip
    # practically never does. Draining it directly stands in for the real
    # `QTimer` firing, i.e. an event loop that kept turning, which is what
    # actually happened up to the point of the hang: it is what a hang
    # AFTER a completed turn looks like, as opposed to a crash landing IN
    # that cooldown window, which the previous test already covers via the
    # prompt itself never being deferred.
    widget._end_persist_cooldown()

    # Again: no `widget.shutdown()` — the hang lands right after the turn
    # completes, before anything closes cleanly.

    stored = store.load()
    assert len(stored) == 1
    texts = [e.get("text", "") for e in stored[0].entries]
    assert any("готово, поправил шейдер" in t for t in texts)


def test_bursts_of_prompts_do_not_each_pay_for_a_full_store_write(qapp, monkeypatch):
    """Two agent switches (or two tabs' prompts) landing within the same
    short window must not mean two full read-modify-write cycles — but the
    call that actually triggers the burst must still be the one that goes
    to disk immediately, not something a later flush might get around to."""
    widget, client, state = _live_widget(qapp, monkeypatch)
    writes: list[int] = []
    real_save = store.save

    def counting_save(*args, **kwargs):
        writes.append(1)
        return real_save(*args, **kwargs)

    # `_persist_conversations` imports `conversations_store` locally on
    # every call, so the module object itself is what needs patching —
    # there is no `panel_mod.store` name to intercept instead.
    monkeypatch.setattr(store, "save", counting_save)

    widget._persist_conversations_soon()
    assert len(writes) == 1, "the triggering call must write immediately"

    widget._persist_conversations_soon()
    widget._persist_conversations_soon()
    assert len(writes) == 1, "calls inside the cooldown must coalesce, not each write"
    assert widget._persist_dirty is True

    # The trailing half of the window, run directly rather than waiting out
    # the real timer.
    widget._end_persist_cooldown()
    assert len(writes) == 2, "the dirty flag from the burst must still be drained once"

    widget.shutdown()


def test_a_clean_close_leaves_an_orderly_marker_in_the_log(qapp, monkeypatch, caplog):
    """The other half of telling a hang apart from a normal close: the log
    had no line anywhere marking an orderly shutdown, so an investigator
    could only infer a hard kill from a gap in timestamps. A marker on the
    clean path makes its ABSENCE the signal instead."""
    widget, client, state = _live_widget(qapp, monkeypatch)
    caplog.set_level(logging.INFO, logger="houdini_agent_panel.ui.panel")

    widget.shutdown()

    assert any("closing" in record.message for record in caplog.records)
    # `logbook`'s one rule: nothing from inside the conversation.
    assert not any(state.session_id in record.message for record in caplog.records)
    assert not any("отвис" in record.message for record in caplog.records)

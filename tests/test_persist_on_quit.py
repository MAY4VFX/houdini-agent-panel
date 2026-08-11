"""Houdini quitting outright is a different event than a tab closing.

The real incident (may-hub, 2026-08-11): an artist's conversation ended
with their own last message and no answer at all, even though they had
watched the agent answer on screen. `panel.log` showed the prompt going
out, a turn finishing (`stop=cancelled`), two `disconnected:` lines — and
then nothing until the next `--- panel start ---` an hour later. The line
`shutdown()` logs on every orderly close, "panel tab closing", never once
appeared anywhere in that log, across every one of dozens of restarts.

The reason: Houdini quitting the whole process does not run
`onDestroyInterface()` (Houdini's own pane teardown, wired to `shutdown()`)
— only closing one tab while Houdini stays open does, by that function's
own docstring. What DOES fire on a normal quit is Qt's own
`QCoreApplication.aboutToQuit` — and `AcpClient.__init__` (client.py)
already connects `self.stop()` to exactly that signal, to cancel the live
turn and tear the connection down. Nothing connected the ON-DISK SAVE to
the same signal, so the cancel-and-disconnect ran with the conversation's
newest answer still only in memory.

These tests simulate a real quit — `aboutToQuit` firing, `shutdown()`
never called — and assert the conversation reaches disk anyway.
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
    """Same shape as `test_persist_on_hang.py`'s own helper: a panel with
    one live session, its outgoing prompt swallowed rather than actually
    sent, wired to the real client so its signals are the genuine ones."""
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    client = panel_mod.shared_client(widget._agent_id)
    state = _session()
    client.session_started.emit(state.session_id, state)
    qapp.processEvents()
    monkeypatch.setattr(client, "prompt", lambda _session_id, _blocks: None)
    return widget, client, state


def test_the_agents_answer_survives_houdini_quitting_mid_turn(qapp, monkeypatch):
    """The exact shape of the report: the artist sent a message, the agent
    was streaming an answer that was genuinely on screen, and Houdini quit
    — via `aboutToQuit`, not a tab close — before the turn ever finished."""
    widget, client, state = _live_widget(qapp, monkeypatch)

    widget._on_submitted([{"type": "text", "text": "дак ты посмотри через мсп"}])
    client.message_chunk.emit(state.session_id, "m1", "смотрю, вижу шейдер")
    qapp.processEvents()

    # No `turn_finished`, no `widget.shutdown()` — the turn is still in
    # flight when the app starts quitting, exactly as `panel.log` showed:
    # a `prompt:` line with nothing else of this panel's own doing after
    # it until the next boot.
    qapp.aboutToQuit.emit()

    stored = store.load()
    assert len(stored) == 1
    texts = [e.get("text", "") for e in stored[0].entries]
    assert any("смотрю, вижу шейдер" in t for t in texts)


def test_the_agents_answer_survives_a_quit_right_after_the_turn_finishes(qapp, monkeypatch):
    """Not mid-stream: the turn already finished (as `panel.log` actually
    showed, `stop=cancelled`), but the debounced trailing write from
    `_persist_conversations_soon` never got a turning event loop to fire
    in — the app started quitting before that timer did."""
    widget, client, state = _live_widget(qapp, monkeypatch)

    widget._on_submitted([{"type": "text", "text": "дак ты посмотри через мсп"}])
    client.message_chunk.emit(state.session_id, "m1", "готово, поправил")
    client.turn_finished.emit(state.session_id, "cancelled")
    qapp.processEvents()

    # Deliberately NOT draining `_end_persist_cooldown()` here — this is
    # what a quit landing inside that short window looks like.
    qapp.aboutToQuit.emit()

    stored = store.load()
    assert len(stored) == 1
    texts = [e.get("text", "") for e in stored[0].entries]
    assert any("готово, поправил" in t for t in texts)


def test_a_quit_is_logged_distinctly_from_a_tab_closing(qapp, monkeypatch, caplog):
    """The other half of the fix the owner asked for: an investigator
    should be able to read the log and tell "Houdini quit" apart from "a
    tab closed" apart from neither happening at all, instead of doing
    timestamp arithmetic against a JSON file by hand."""
    widget, client, state = _live_widget(qapp, monkeypatch)
    caplog.set_level(logging.INFO, logger="houdini_agent_panel.ui.panel")

    qapp.aboutToQuit.emit()

    assert any("quitting" in record.message for record in caplog.records)
    assert any("persisted conversations" in record.message for record in caplog.records)
    # `logbook`'s one rule: nothing from inside the conversation.
    assert not any(state.session_id in record.message for record in caplog.records)

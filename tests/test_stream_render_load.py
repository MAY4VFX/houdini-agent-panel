"""A streamed answer must not cost the UI thread more than it has to.

Drawing a message hands its whole text back to `QTextDocument.setMarkdown`,
so an answer that grows by a chunk at a time is re-parsed from the top every
time — quadratic in the length of the answer. Measured on this code before
the fix: a 150 KB answer arriving in 3000 chunks spent about eight seconds
inside `refresh`, on Houdini's own UI thread.

The panel collapses a burst of chunks into one render per event-loop pass.
What this file guards is that the collapsing is real AND that nothing is
lost by it — a coalesced feed that drops the last chunk would be a far worse
bug than a slow one.
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
    monkeypatch.setattr(panel_mod._OrphanSweepWorker, "start", lambda self: None)
    panel_mod.reset_shared_state_for_tests()
    yield
    panel_mod.reset_shared_state_for_tests()


def _panel_with_session(qapp):
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    client = panel_mod.shared_client(widget._agent_id)
    state = sessions.SessionState(
        session_id="s1", title="New conversation", cwd="/tmp", created_at=0.0
    )
    client.session_started.emit(state.session_id, state)
    qapp.processEvents()
    return widget, client


def test_a_burst_of_chunks_is_drawn_once_and_in_full(qapp):
    widget, client = _panel_with_session(qapp)
    renders: list[str] = []
    original = widget._transcript.refresh
    widget._transcript.refresh = lambda entry_id=None: (
        renders.append(entry_id), original(entry_id)
    )[1]

    for index in range(50):
        client.message_chunk.emit("s1", "m1", f"word{index} ")
    qapp.processEvents()

    assert len(renders) == 1, f"50 chunks in one pass should draw once, drew {len(renders)}"
    row = widget._transcript._rows[renders[0]]
    assert row._segments[0].toPlainText().endswith("word49")
    widget.shutdown()


def test_a_chunk_arriving_alone_is_still_drawn(qapp):
    """Coalescing must never mean waiting for a second chunk that isn't coming."""
    widget, client = _panel_with_session(qapp)

    client.message_chunk.emit("s1", "m1", "the only thing it had to say")
    qapp.processEvents()

    texts = [
        row._segments[0].toPlainText()
        for row in widget._transcript._rows.values()
        if hasattr(row, "_segments")
    ]
    assert "the only thing it had to say" in texts
    widget.shutdown()


def test_a_tool_call_draws_immediately_over_queued_chunks(qapp):
    """Anything that isn't a chunk must see a feed that is up to date.

    `reset_thinking_after_tool` reads the drawn rows, so a tool call
    arriving in the same pass as the chunks before it cannot be allowed to
    look at a feed that hasn't caught up.
    """
    from types import SimpleNamespace

    widget, client = _panel_with_session(qapp)
    client.message_chunk.emit("s1", "m1", "let me look")
    assert widget._dirty_entries, "a chunk should be queued, not drawn"

    client.tool_call.emit(
        "s1",
        SimpleNamespace(
            tool_call_id="tc1", title="Create geometry", kind="edit",
            status="pending", content=None, locations=None,
        ),
    )

    assert not widget._dirty_entries, "the queued chunk should have been flushed first"
    kinds = [type(row).__name__ for row in widget._transcript._rows.values()]
    assert "_ToolCallRow" in kinds
    widget.shutdown()


def test_switching_conversation_drops_the_previous_one_s_queue(qapp):
    widget, client = _panel_with_session(qapp)
    second = sessions.SessionState(
        session_id="s2", title="Another", cwd="/tmp", created_at=1.0
    )
    client.session_started.emit("s2", second)
    qapp.processEvents()

    widget._set_current_session("s1")
    client.message_chunk.emit("s1", "m1", "half a sentence")
    assert widget._dirty_entries

    widget._set_current_session("s2")

    assert not widget._dirty_entries
    assert not widget._render_timer.isActive()
    widget.shutdown()

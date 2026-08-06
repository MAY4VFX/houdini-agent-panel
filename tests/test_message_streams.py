"""Reasoning and answer under ONE `messageId`.

Measured on opencode: the reasoning stream and the answer stream carry the
same `messageId` — the id names the agent's message, not one stream inside
it. Both entries then took that id as their own, and `TranscriptView`
resolves an id by finding the FIRST entry carrying it, so every chunk of the
answer was rendered into the thought's row: the panel showed the agent
thinking and never showed what it said.
"""

from __future__ import annotations

from houdini_agent_panel.transcript_model import TranscriptModel
from houdini_agent_panel.ui.transcript import TranscriptView


def test_one_message_id_keeps_thought_and_answer_apart():
    model = TranscriptModel()

    model.apply_chunk("msg-1", "Let me check the scene", thought=True)
    model.apply_chunk("msg-1", "There are 3 lights", thought=False)
    model.apply_chunk("msg-1", " in /obj.", thought=False)
    model.apply_chunk("msg-1", " ...and their intensities", thought=True)

    entries = model.entries()
    assert [(e.kind, e.text) for e in entries] == [
        ("thought", "Let me check the scene ...and their intensities"),
        ("agent", "There are 3 lights in /obj."),
    ]
    # Two entries, two ids: one id shared between them is what made the
    # answer unreachable in the view.
    assert entries[0].id != entries[1].id


def test_the_answer_reaches_the_screen_when_it_shares_the_thought_s_id(qapp):
    model = TranscriptModel()
    view = TranscriptView()
    view.set_model(model)

    thought = model.apply_chunk("msg-1", "thinking about it", thought=True)
    view.refresh(thought.id)
    answer = model.apply_chunk("msg-1", "here is the answer", thought=False)
    view.refresh(answer.id)
    answer = model.apply_chunk("msg-1", " in full", thought=False)
    view.refresh(answer.id)

    assert view._rows[thought.id] is not view._rows[answer.id]
    rendered = view._rows[answer.id]._segments[0].toPlainText()
    assert rendered == "here is the answer in full"


def test_a_kept_id_still_stitches_its_own_chunks(qapp):
    """The fix must not turn one message back into a message per chunk."""
    model = TranscriptModel()
    view = TranscriptView()
    view.set_model(model)

    for word in ("one ", "two ", "three"):
        entry = model.apply_chunk("msg-1", word)
        view.refresh(entry.id)

    assert len(model.entries()) == 1
    assert len(view._rows) == 1

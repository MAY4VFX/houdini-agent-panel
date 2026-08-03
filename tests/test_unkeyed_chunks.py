"""Agents that stream without a `messageId`.

`messageId` is optional in ACP and Grok omits it on every chunk. Each one
became its own feed entry, so a two-sentence answer arrived shredded one
word per line down the page.
"""

from __future__ import annotations

from houdini_agent_panel.transcript_model import TranscriptModel


def test_unkeyed_chunks_form_one_message():
    model = TranscriptModel()

    for word in ("При", "вет", "! ", "Чем ", "помочь", "?"):
        model.apply_chunk("", word)

    entries = model.entries()
    assert len(entries) == 1
    assert entries[0].text == "Привет! Чем помочь?"


def test_a_user_line_ends_the_run():
    """Anything else in the feed means the next chunk is a new message."""
    model = TranscriptModel()
    model.apply_chunk("", "first answer")
    model.append_user("a question")
    model.apply_chunk("", "second answer")

    assert [(e.kind, e.text) for e in model.entries()] == [
        ("agent", "first answer"),
        ("user", "a question"),
        ("agent", "second answer"),
    ]


def test_a_tool_call_ends_the_run():
    from types import SimpleNamespace

    model = TranscriptModel()
    model.apply_chunk("", "before")
    model.apply_tool_call(
        SimpleNamespace(tool_call_id="t1", title="read", kind="read", status="pending",
                        content=None, locations=None)
    )
    model.apply_chunk("", "after")

    kinds = [e.kind for e in model.entries()]
    assert kinds == ["agent", "tool", "agent"]


def test_thoughts_do_not_merge_into_the_answer():
    model = TranscriptModel()
    model.apply_chunk("", "thinking", thought=True)
    model.apply_chunk("", "answering")

    assert [(e.kind, e.text) for e in model.entries()] == [
        ("thought", "thinking"),
        ("agent", "answering"),
    ]


def test_keyed_chunks_are_untouched_by_the_fallback():
    """An agent that does send ids must not get its messages merged."""
    model = TranscriptModel()
    model.apply_chunk("m1", "one")
    model.apply_chunk("m2", "two")

    assert [e.text for e in model.entries()] == ["one", "two"]

"""The full-message resend under one `message_id` doubles the text.

Measured on `@agentclientprotocol/claude-agent-acp` 0.66.0: the adapter
streams chunks as usual, then re-emits the WHOLE consolidated message under
the SAME `message_id`, because its own streamed-content bookkeeping
(`streamedBlocks`) got reset on activation (see docs/facts/acp-sdk.md). What
reached the panel and got captured on screen:

    Причина найдена — дело не в HDRI, а в форм## Причина найдена — дело не
    в HDRI, а в формате новых текстур

i.e. a partial chunk ("...в форм"), immediately followed by the full text
of the same message ("...в формате новых текстур") under the same id.
`apply_chunk` used to always append, so the second chunk landed glued onto
the tail of the first instead of replacing it.
"""

from __future__ import annotations

from houdini_agent_panel.transcript_model import TranscriptModel


def test_full_resend_replaces_the_partial_chunk_instead_of_appending():
    model = TranscriptModel()

    partial = "## Причина найдена — дело не в HDRI, а в форм"
    full = "## Причина найдена — дело не в HDRI, а в формате новых текстур"

    model.apply_chunk("msg-1", partial)
    entry = model.apply_chunk("msg-1", full)

    assert entry.text == full
    assert partial + full not in entry.text  # the doubled-up shape from the bug report


def test_exact_duplicate_of_a_long_accumulated_message_is_not_doubled():
    model = TranscriptModel()
    long_text = "This sentence is long enough to clear the coincidence guard easily."

    model.apply_chunk("msg-1", long_text)
    entry = model.apply_chunk("msg-1", long_text)  # the very same text, resent whole

    assert entry.text == long_text


def test_empty_chunk_after_a_long_message_changes_nothing():
    model = TranscriptModel()
    long_text = "This sentence is long enough to clear the coincidence guard easily."

    model.apply_chunk("msg-1", long_text)
    entry = model.apply_chunk("msg-1", "")

    assert entry.text == long_text


def test_short_accumulated_text_still_appends_a_genuine_continuation():
    """The guard must not fire on a short accumulated fragment: "Да" is a
    literal prefix of the real continuation "Давай посмотрим", but that's
    coincidence, not a resend — appending is still the right call below the
    guard's minimum length."""
    model = TranscriptModel()

    model.apply_chunk("msg-1", "Да")
    entry = model.apply_chunk("msg-1", "Давай посмотрим")

    assert entry.text == "ДаДавай посмотрим"


def test_deltas_resume_normally_after_a_resend_is_replaced():
    """A resend isn't necessarily the last chunk of the message — real
    deltas can keep arriving afterwards and must keep appending onto the
    now-corrected text, not onto the discarded partial one."""
    model = TranscriptModel()

    partial = "## Причина найдена — дело не в HDRI, а в форм"
    full = "## Причина найдена — дело не в HDRI, а в формате новых текстур"

    model.apply_chunk("msg-1", partial)
    model.apply_chunk("msg-1", full)
    entry = model.apply_chunk("msg-1", " (уточнение)")

    assert entry.text == full + " (уточнение)"


def test_reasoning_and_answer_under_one_id_are_unaffected_by_the_guard():
    """Must not regress the opencode case (tests/test_message_streams.py):
    a thought and an answer sharing one message_id are different KEYS
    (kind, message_id), so a long thought never collides with the guard for
    the separate answer entry."""
    model = TranscriptModel()

    model.apply_chunk("msg-1", "Let me check the scene in detail before answering", thought=True)
    answer = model.apply_chunk("msg-1", "There are 3 lights in /obj.", thought=False)

    assert answer.text == "There are 3 lights in /obj."

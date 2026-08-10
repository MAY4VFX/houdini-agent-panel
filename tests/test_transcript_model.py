"""Tests for `transcript_model.py` — plain Python, no QApplication."""

from __future__ import annotations

from types import SimpleNamespace

from houdini_agent_panel.transcript_model import PermissionView, TranscriptModel


def _tool_call(**overrides):
    defaults = dict(
        tool_call_id="tc1",
        title="Read scene.py",
        kind="read",
        status="pending",
        content=None,
        locations=None,
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _tool_update(**overrides):
    defaults = dict(
        tool_call_id="tc1", title=None, kind=None, status=None, content=None, locations=None
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _plan_entry(content, priority="medium", status="pending"):
    return SimpleNamespace(content=content, priority=priority, status=status)


# --- user / chunks -----------------------------------------------------


def test_append_user_creates_entry_with_text():
    model = TranscriptModel()
    entry = model.append_user("hi")
    assert entry.kind == "user"
    assert entry.text == "hi"
    assert model.entries() == [entry]


def test_activity_is_inserted_in_chronology_and_finished_in_place():
    model = TranscriptModel()
    user = model.append_user("question")
    activity = model.start_activity()
    answer = model.apply_chunk("m1", "answer")

    finished = model.finish_activity()

    assert model.entries() == [user, activity, answer]
    assert finished is activity
    assert activity.activity.finished_at >= activity.activity.started_at


def test_chunks_with_same_message_id_are_merged():
    model = TranscriptModel()
    first = model.apply_chunk("m1", "Hello")
    second = model.apply_chunk("m1", ", world")

    assert first is second
    assert first.text == "Hello, world"
    assert model.entries() == [first]


def test_chunks_with_different_message_id_are_separate_entries():
    model = TranscriptModel()
    model.apply_chunk("m1", "first")
    model.apply_chunk("m2", "second")

    assert [e.text for e in model.entries()] == ["first", "second"]


def test_thought_chunk_is_a_separate_kind_from_agent_chunk():
    model = TranscriptModel()
    thought = model.apply_chunk("m1", "thinking", thought=True)
    agent = model.apply_chunk("m1", "speaking", thought=False)

    assert thought.kind == "thought"
    assert agent.kind == "agent"
    assert thought is not agent
    assert len(model.entries()) == 2


def test_chunks_without_a_message_id_merge_into_one_message():
    """`messageId` is optional in ACP and Grok omits it on every chunk.
    Treating each one as its own entry shredded an answer one word per line
    down the page; consecutive unkeyed chunks belong to the same message."""
    model = TranscriptModel()

    model.apply_chunk("", "one ")
    model.apply_chunk("", "two")

    assert [e.text for e in model.entries()] == ["one two"]



def test_apply_tool_call_creates_entry_with_defaults_for_missing_fields():
    model = TranscriptModel()
    call = _tool_call(kind=None, status=None)
    entry = model.apply_tool_call(call)

    assert entry.kind == "tool"
    assert entry.tool.kind == "other"
    assert entry.tool.status == "pending"
    assert entry.tool.title == "Read scene.py"


def test_apply_tool_update_patches_only_provided_fields():
    model = TranscriptModel()
    model.apply_tool_call(_tool_call(status="pending"))

    entry = model.apply_tool_update(_tool_update(status="in_progress"))

    assert entry.tool.status == "in_progress"
    assert entry.tool.title == "Read scene.py"  # left untouched


def test_apply_tool_update_replaces_content_wholesale():
    model = TranscriptModel()
    old_content = [SimpleNamespace(model_dump=lambda exclude_none: {"a": 1})]
    model.apply_tool_call(_tool_call(content=old_content))

    entry = model.apply_tool_update(
        _tool_update(content=[SimpleNamespace(model_dump=lambda exclude_none: {"b": 2})])
    )

    assert entry.tool.content == [{"b": 2}]


def test_apply_tool_update_for_unknown_id_returns_none():
    model = TranscriptModel()
    assert model.apply_tool_update(_tool_update(tool_call_id="ghost")) is None


# --- plan ----------------------------------------------------------------


def test_apply_plan_creates_single_entry():
    model = TranscriptModel()
    entry = model.apply_plan([_plan_entry("step 1"), _plan_entry("step 2")])

    assert entry.kind == "plan"
    assert [p.content for p in entry.plan] == ["step 1", "step 2"]
    assert model.entries() == [entry]


def test_apply_plan_replaces_whole_list_and_reuses_entry():
    model = TranscriptModel()
    first = model.apply_plan([_plan_entry("step 1")])
    second = model.apply_plan([_plan_entry("step 1", status="completed"), _plan_entry("step 2")])

    assert first is second
    assert len(model.entries()) == 1  # didn't get duplicated
    assert [p.status for p in second.plan] == ["completed", "pending"]


# --- permissions -----------------------------------------------------------


def test_apply_permission_then_resolve_with_selected_option():
    model = TranscriptModel()
    view = PermissionView(
        request_key="req1", tool_title="rm -rf", options=[("allow_once", "Allow", "allow_once")]
    )
    model.apply_permission(view)

    resolved = model.resolve_permission("req1", "allow_once")

    assert resolved.permission.answered == "allow_once"


def test_resolve_permission_with_none_option_id_means_cancelled():
    model = TranscriptModel()
    view = PermissionView(request_key="req1", tool_title="rm -rf", options=[])
    model.apply_permission(view)

    resolved = model.resolve_permission("req1", None)

    assert resolved.permission.answered == ""


def test_resolve_permission_unknown_key_returns_none():
    model = TranscriptModel()
    assert model.resolve_permission("nope", "x") is None


# --- errors ----------------------------------------------------------------


def test_append_error_creates_entry():
    model = TranscriptModel()
    entry = model.append_error("something went wrong")
    assert entry.kind == "error"
    assert entry.text == "something went wrong"


# --- notes -------------------------------------------------------------------
#
# Reported for real: 408 of 570 entries in an owner's own persisted store
# were `kind="error"`, and the ones sampled ("Preparing Claude Agent…",
# "Agent stopped.") were routine commentary, not failures — every one of
# `ui/panel.py::_note`'s call sites used to route through `append_error`
# unconditionally, with nothing else to route the informational ones to.


def test_append_note_creates_entry():
    model = TranscriptModel()
    entry = model.append_note("Agent stopped.")
    assert entry.kind == "note"
    assert entry.text == "Agent stopped."


def test_note_and_error_both_survive_a_round_trip_through_records():
    """`to_records`/`load_records` is what a restored conversation actually
    goes through — a kind that isn't in `to_records`'s own allow-list
    silently vanishes on restart, which is exactly how the two used to be
    indistinguishable there in the first place (both WERE "error")."""
    model = TranscriptModel()
    note = model.append_note("Agent stopped.")
    error = model.append_error("Agent failed to start: boom")

    restored = TranscriptModel()
    restored.load_records(model.to_records())

    kinds = {entry.id: entry.kind for entry in restored.entries()}
    assert kinds[note.id] == "note"
    assert kinds[error.id] == "error"


# --- queue / promote / remove -------------------------------------------------


def test_promote_queued_flips_kind_and_keeps_the_same_entry():
    model = TranscriptModel()
    queued = model.queue_message("q1", "make it rain")

    promoted = model.promote_queued("q1")

    assert promoted is queued
    assert promoted.kind == "user"


def test_promote_queued_returns_none_for_an_unknown_id():
    model = TranscriptModel()
    assert model.promote_queued("does-not-exist") is None


def test_remove_entry_takes_out_a_queued_message():
    model = TranscriptModel()
    model.queue_message("q1", "second thought")

    assert model.remove_entry("q1") is True
    assert all(e.id != "q1" for e in model.entries())


def test_remove_entry_refuses_a_message_that_already_sent():
    """The other half of the owner's report: a stale "Queued" row on
    screen must never let Remove delete a message that has ALREADY been
    promoted to `"user"` — the id can still match a row on screen (it
    hasn't been re-rendered yet), but the row is no longer a queued one,
    and `remove_entry` is not the button that un-sends a real message."""
    model = TranscriptModel()
    model.queue_message("q1", "make it rain")
    model.promote_queued("q1")

    assert model.remove_entry("q1") is False
    entry = next(e for e in model.entries() if e.id == "q1")
    assert entry.kind == "user"
    assert entry.text == "make it rain"


def test_remove_entry_refuses_any_other_kind_too():
    model = TranscriptModel()
    model.append_user("hello")
    entry_id = model.entries()[0].id

    assert model.remove_entry(entry_id) is False
    assert len(model.entries()) == 1

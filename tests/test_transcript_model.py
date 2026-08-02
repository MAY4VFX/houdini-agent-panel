"""Тесты `transcript_model.py` — чистый Python, никакого QApplication."""

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
    defaults = dict(tool_call_id="tc1", title=None, kind=None, status=None, content=None, locations=None)
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _plan_entry(content, priority="medium", status="pending"):
    return SimpleNamespace(content=content, priority=priority, status=status)


# --- user / chunks -----------------------------------------------------


def test_append_user_creates_entry_with_text():
    model = TranscriptModel()
    entry = model.append_user("привет")
    assert entry.kind == "user"
    assert entry.text == "привет"
    assert model.entries() == [entry]


def test_chunks_with_same_message_id_are_merged():
    model = TranscriptModel()
    first = model.apply_chunk("m1", "Привет")
    second = model.apply_chunk("m1", ", мир")

    assert first is second
    assert first.text == "Привет, мир"
    assert model.entries() == [first]


def test_chunks_with_different_message_id_are_separate_entries():
    model = TranscriptModel()
    model.apply_chunk("m1", "первое")
    model.apply_chunk("m2", "второе")

    assert [e.text for e in model.entries()] == ["первое", "второе"]


def test_thought_chunk_is_a_separate_kind_from_agent_chunk():
    model = TranscriptModel()
    thought = model.apply_chunk("m1", "думаю", thought=True)
    agent = model.apply_chunk("m1", "говорю", thought=False)

    assert thought.kind == "thought"
    assert agent.kind == "agent"
    assert thought is not agent
    assert len(model.entries()) == 2


def test_chunk_without_message_id_never_merges():
    model = TranscriptModel()
    first = model.apply_chunk("", "a")
    second = model.apply_chunk("", "b")

    assert first is not second
    assert [e.text for e in model.entries()] == ["a", "b"]


# --- tool calls --------------------------------------------------------


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
    assert entry.tool.title == "Read scene.py"  # не тронуто


def test_apply_tool_update_replaces_content_wholesale():
    model = TranscriptModel()
    model.apply_tool_call(_tool_call(content=[SimpleNamespace(model_dump=lambda exclude_none: {"a": 1})]))

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
    entry = model.apply_plan([_plan_entry("шаг 1"), _plan_entry("шаг 2")])

    assert entry.kind == "plan"
    assert [p.content for p in entry.plan] == ["шаг 1", "шаг 2"]
    assert model.entries() == [entry]


def test_apply_plan_replaces_whole_list_and_reuses_entry():
    model = TranscriptModel()
    first = model.apply_plan([_plan_entry("шаг 1")])
    second = model.apply_plan([_plan_entry("шаг 1", status="completed"), _plan_entry("шаг 2")])

    assert first is second
    assert len(model.entries()) == 1  # не задублировалось
    assert [p.status for p in second.plan] == ["completed", "pending"]


# --- permissions -----------------------------------------------------------


def test_apply_permission_then_resolve_with_selected_option():
    model = TranscriptModel()
    view = PermissionView(request_key="req1", tool_title="rm -rf", options=[("allow_once", "Allow", "allow_once")])
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
    entry = model.append_error("что-то пошло не так")
    assert entry.kind == "error"
    assert entry.text == "что-то пошло не так"

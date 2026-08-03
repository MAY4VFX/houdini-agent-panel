"""Tests for `ui/transcript.py::TranscriptView`. Needs `QApplication` (the `qapp` fixture)."""

from __future__ import annotations

from types import SimpleNamespace

from houdini_agent_panel.transcript_model import PermissionView, TranscriptModel
from houdini_agent_panel.ui.qt import QtCore, QtGui
from houdini_agent_panel.ui.transcript import TranscriptView


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


def _view_and_model():
    model = TranscriptModel()
    view = TranscriptView()
    view.set_model(model)
    return view, model


# --- basic rendering -----------------------------------------------------


def test_set_model_renders_existing_entries(qapp):
    model = TranscriptModel()
    model.append_user("hello")
    view = TranscriptView()

    view.set_model(model)

    assert len(view._rows) == 1


def test_horizontal_scrollbar_always_off(qapp):
    """Long tool output must not stretch the panel horizontally."""
    view = TranscriptView()
    assert view.horizontalScrollBarPolicy() == QtCore.Qt.ScrollBarAlwaysOff


def test_refresh_none_rebuilds_everything(qapp):
    view, model = _view_and_model()
    model.append_user("hello")

    view.refresh(None)

    assert len(view._rows) == 1


# --- partial redraw ------------------------------------------------------


def test_refresh_entry_id_inserts_new_row_without_recreating_others(qapp):
    view, model = _view_and_model()

    e1 = model.append_user("first")
    view.refresh(e1.id)
    row1_before = view._rows[e1.id]

    e2 = model.append_user("second")
    view.refresh(e2.id)

    assert view._rows[e1.id] is row1_before
    assert len(view._rows) == 2


def test_refresh_entry_id_patches_streaming_chunk_in_place(qapp):
    view, model = _view_and_model()

    entry = model.apply_chunk("m1", "Hello")
    view.refresh(entry.id)
    row_before = view._rows[entry.id]

    model.apply_chunk("m1", ", world")
    view.refresh(entry.id)

    row_after = view._rows[entry.id]
    assert row_after is row_before  # not recreated
    assert row_after._segments[0].toPlainText() == "Hello, world"


def test_refresh_unrelated_entry_id_does_not_touch_other_rows(qapp):
    view, model = _view_and_model()
    e1 = model.append_user("first")
    view.refresh(e1.id)
    e2 = model.append_user("second")
    view.refresh(e2.id)

    row1 = view._rows[e1.id]
    row2 = view._rows[e2.id]

    model.apply_chunk("m1", "third")
    view.refresh("m1")

    assert view._rows[e1.id] is row1
    assert view._rows[e2.id] is row2


# --- messages --------------------------------------------------------------


def test_message_text_is_selectable_by_mouse(qapp):
    view, model = _view_and_model()
    entry = model.append_user("selectable text")
    view.refresh(entry.id)

    row = view._rows[entry.id]
    assert bool(row._segments[0].textInteractionFlags() & QtCore.Qt.TextSelectableByMouse)


def test_backticks_render_as_code_not_raw_text(qapp):
    view, model = _view_and_model()
    entry = model.apply_chunk("m1", "look at `/obj/heli/rotor` — done")
    view.refresh(entry.id)

    row = view._rows[entry.id]
    rendered = "".join(segment.toPlainText() for segment in row._segments)
    assert "`" not in rendered
    assert "/obj/heli/rotor" in rendered


def test_fenced_code_block_becomes_dedicated_code_widget(qapp):
    from houdini_agent_panel.ui.transcript import _CodeBlock

    view, model = _view_and_model()
    text = "text before\n\n```python\nprint('hi')\n```\n\nand after"
    entry = model.apply_chunk("m1", text)
    view.refresh(entry.id)

    row = view._rows[entry.id]
    code_widgets = [s for s in row._segments if isinstance(s, _CodeBlock)]
    assert len(code_widgets) == 1
    assert code_widgets[0].toPlainText() == "print('hi')"
    # no backticks leaked into either prose chunk around the code
    prose = "".join(s.toPlainText() for s in row._segments if s is not code_widgets[0])
    assert "```" not in prose


def test_long_code_line_does_not_grow_row_size_hint(qapp):
    view, model = _view_and_model()
    short_entry = model.apply_chunk("short", "an ordinary short reply")
    view.refresh(short_entry.id)
    short_row = view._rows[short_entry.id]

    long_code = "x = " + "1" * 2000
    long_entry = model.apply_chunk("long", f"```\n{long_code}\n```")
    view.refresh(long_entry.id)
    long_row = view._rows[long_entry.id]

    # The code block scrolls inside itself (NoWrap + horizontal scroll)
    # instead of pushing the row or the panel wider.
    assert long_row.sizeHint().width() <= short_row.sizeHint().width() + 50


def test_user_and_agent_messages_get_different_visual_roles(qapp):
    view, model = _view_and_model()
    user_entry = model.append_user("a human question")
    view.refresh(user_entry.id)
    agent_entry = model.apply_chunk("m1", "the agent's reply", thought=False)
    view.refresh(agent_entry.id)

    user_row = view._rows[user_entry.id]
    agent_row = view._rows[agent_entry.id]

    # The left indent marks "a human typed this"; the agent's reply has none.
    assert user_row.layout().contentsMargins().left() > 0
    assert agent_row.layout().contentsMargins().left() == 0

    # The muted colour of a human's line is distinguishable from a reply's.
    user_color = user_row._segments[0].palette().color(QtGui.QPalette.Text)
    agent_color = agent_row._segments[0].palette().color(QtGui.QPalette.Text)
    assert user_color != agent_color


def test_thought_differs_from_agent_message(qapp):
    view, model = _view_and_model()
    thought_entry = model.apply_chunk("m1", "thinking about pyro", thought=True)
    view.refresh(thought_entry.id)
    agent_entry = model.apply_chunk("m2", "an ordinary reply", thought=False)
    view.refresh(agent_entry.id)

    thought_row = view._rows[thought_entry.id]
    agent_row = view._rows[agent_entry.id]

    assert thought_row._segments[0].font().italic() is True
    assert agent_row._segments[0].font().italic() is False


# --- tool calls --------------------------------------------------------------


def test_tool_call_row_shows_title_and_status(qapp):
    view, model = _view_and_model()
    entry = model.apply_tool_call(_tool_call(title="Read scene.py", status="pending"))
    view.refresh(entry.id)

    row = view._rows[entry.id]
    assert "Read scene.py" in row._toggle.text()


def test_tool_call_row_status_update_patches_same_widget(qapp):
    view, model = _view_and_model()
    entry = model.apply_tool_call(_tool_call(status="pending"))
    view.refresh(entry.id)
    row_before = view._rows[entry.id]

    model.apply_tool_update(_tool_update(status="completed"))
    view.refresh(entry.id)

    row_after = view._rows[entry.id]
    assert row_after is row_before
    assert "done" in row_after._toggle.text()


def test_tool_call_row_starts_collapsed(qapp):
    view, model = _view_and_model()
    entry = model.apply_tool_call(_tool_call())
    view.refresh(entry.id)

    row = view._rows[entry.id]
    assert row._details.isHidden() is True


def test_tool_call_row_expands_on_toggle_and_shows_content(qapp):
    view, model = _view_and_model()
    content_item = SimpleNamespace(model_dump=lambda exclude_none: {"type": "diff", "path": "a.py", "new_text": "print(1)"})
    entry = model.apply_tool_call(_tool_call(content=[content_item]))
    view.refresh(entry.id)

    row = view._rows[entry.id]
    row._toggle.click()

    assert row._details.isHidden() is False
    assert "a.py" in row._details.text()
    assert "print(1)" in row._details.text()


def test_tool_call_row_keeps_expanded_state_across_status_update(qapp):
    view, model = _view_and_model()
    entry = model.apply_tool_call(_tool_call(status="pending"))
    view.refresh(entry.id)
    row = view._rows[entry.id]
    row._toggle.click()  # expanded

    model.apply_tool_update(_tool_update(status="in_progress"))
    view.refresh(entry.id)

    assert row._details.isHidden() is False  # the expansion survived the status update


# --- plan --------------------------------------------------------------------


def test_plan_row_lists_steps_with_status(qapp):
    view, model = _view_and_model()
    entry = model.apply_plan([_plan_entry("step 1", status="completed"), _plan_entry("step 2", status="pending")])
    view.refresh(entry.id)

    row = view._rows[entry.id]
    texts = [label.text() for label in row._step_labels]
    assert texts == ["✓ step 1", "○ step 2"]


def test_plan_row_replaces_steps_in_place_on_update(qapp):
    view, model = _view_and_model()
    entry = model.apply_plan([_plan_entry("step 1")])
    view.refresh(entry.id)
    row_before = view._rows[entry.id]

    model.apply_plan([_plan_entry("step 1", status="completed"), _plan_entry("step 2")])
    view.refresh(entry.id)

    row_after = view._rows[entry.id]
    assert row_after is row_before
    assert [label.text() for label in row_after._step_labels] == ["✓ step 1", "○ step 2"]


# --- permissions -------------------------------------------------------------


def test_permission_is_not_embedded_in_transcript(qapp):
    view, model = _view_and_model()
    perm_view = PermissionView(
        request_key="req1", tool_title="rm -rf", options=[("allow_once", "Allow", "allow_once")]
    )
    entry = model.apply_permission(perm_view)
    view.refresh(entry.id)

    assert entry.id not in view._rows


def test_resolved_permission_stays_out_of_transcript(qapp):
    view, model = _view_and_model()
    perm_view = PermissionView(
        request_key="req1", tool_title="rm -rf", options=[("allow_once", "Allow", "allow_once")]
    )
    entry = model.apply_permission(perm_view)
    view.refresh(entry.id)
    model.resolve_permission("req1", "allow_once")
    view.refresh(entry.id)
    assert entry.id not in view._rows


# --- auto-scroll ---------------------------------------------------------------


def test_autoscroll_sticks_to_bottom_when_already_at_bottom(qapp):
    view, model = _view_and_model()
    bar = view.verticalScrollBar()
    bar.setRange(0, 100)
    bar.setValue(100)

    entry = model.append_user("a new message")
    view.refresh(entry.id)

    assert bar.value() == bar.maximum()


def test_no_autoscroll_when_scrolled_up_reading(qapp):
    view, model = _view_and_model()
    bar = view.verticalScrollBar()
    bar.setRange(0, 100)
    bar.setValue(20)

    entry = model.append_user("a new message")
    view.refresh(entry.id)

    assert bar.value() == 20

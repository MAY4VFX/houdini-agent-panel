"""Тесты `ui/transcript.py::TranscriptView`. Нужен `QApplication` (фикстура `qapp`)."""

from __future__ import annotations

from types import SimpleNamespace

from houdini_agent_panel.transcript_model import PermissionView, TranscriptModel
from houdini_agent_panel.ui.permissions import PermissionRow
from houdini_agent_panel.ui.qt import QtCore
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


# --- базовая отрисовка ---------------------------------------------------


def test_set_model_renders_existing_entries(qapp):
    model = TranscriptModel()
    model.append_user("привет")
    view = TranscriptView()

    view.set_model(model)

    assert len(view._rows) == 1


def test_horizontal_scrollbar_always_off(qapp):
    """Длинный вывод инструмента не должен растягивать панель по горизонтали."""
    view = TranscriptView()
    assert view.horizontalScrollBarPolicy() == QtCore.Qt.ScrollBarAlwaysOff


def test_refresh_none_rebuilds_everything(qapp):
    view, model = _view_and_model()
    model.append_user("привет")

    view.refresh(None)

    assert len(view._rows) == 1


# --- частичная перерисовка -----------------------------------------------


def test_refresh_entry_id_inserts_new_row_without_recreating_others(qapp):
    view, model = _view_and_model()

    e1 = model.append_user("первое")
    view.refresh(e1.id)
    row1_before = view._rows[e1.id]

    e2 = model.append_user("второе")
    view.refresh(e2.id)

    assert view._rows[e1.id] is row1_before
    assert len(view._rows) == 2


def test_refresh_entry_id_patches_streaming_chunk_in_place(qapp):
    view, model = _view_and_model()

    entry = model.apply_chunk("m1", "Привет")
    view.refresh(entry.id)
    row_before = view._rows[entry.id]

    model.apply_chunk("m1", ", мир")
    view.refresh(entry.id)

    row_after = view._rows[entry.id]
    assert row_after is row_before  # не пересоздан
    assert row_after._label.text() == "Привет, мир"


def test_refresh_unrelated_entry_id_does_not_touch_other_rows(qapp):
    view, model = _view_and_model()
    e1 = model.append_user("первое")
    view.refresh(e1.id)
    e2 = model.append_user("второе")
    view.refresh(e2.id)

    row1 = view._rows[e1.id]
    row2 = view._rows[e2.id]

    model.apply_chunk("m1", "третье")
    view.refresh("m1")

    assert view._rows[e1.id] is row1
    assert view._rows[e2.id] is row2


# --- сообщения -------------------------------------------------------------


def test_message_text_is_selectable_by_mouse(qapp):
    view, model = _view_and_model()
    entry = model.append_user("выделяемый текст")
    view.refresh(entry.id)

    row = view._rows[entry.id]
    assert bool(row._label.textInteractionFlags() & QtCore.Qt.TextSelectableByMouse)


# --- вызовы инструментов -----------------------------------------------------


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
    assert "готово" in row_after._toggle.text()


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
    row._toggle.click()  # развернули

    model.apply_tool_update(_tool_update(status="in_progress"))
    view.refresh(entry.id)

    assert row._details.isHidden() is False  # разворот пережил обновление статуса


# --- план --------------------------------------------------------------------


def test_plan_row_lists_steps_with_status(qapp):
    view, model = _view_and_model()
    entry = model.apply_plan([_plan_entry("шаг 1", status="completed"), _plan_entry("шаг 2", status="pending")])
    view.refresh(entry.id)

    row = view._rows[entry.id]
    texts = [label.text() for label in row._step_labels]
    assert texts == ["✓ шаг 1", "○ шаг 2"]


def test_plan_row_replaces_steps_in_place_on_update(qapp):
    view, model = _view_and_model()
    entry = model.apply_plan([_plan_entry("шаг 1")])
    view.refresh(entry.id)
    row_before = view._rows[entry.id]

    model.apply_plan([_plan_entry("шаг 1", status="completed"), _plan_entry("шаг 2")])
    view.refresh(entry.id)

    row_after = view._rows[entry.id]
    assert row_after is row_before
    assert [label.text() for label in row_after._step_labels] == ["✓ шаг 1", "○ шаг 2"]


# --- разрешения --------------------------------------------------------------


def test_permission_row_is_embedded_and_forwards_answered_signal(qapp):
    view, model = _view_and_model()
    perm_view = PermissionView(
        request_key="req1", tool_title="rm -rf", options=[("allow_once", "Allow", "allow_once")]
    )
    entry = model.apply_permission(perm_view)
    view.refresh(entry.id)

    row = view._rows[entry.id]
    assert isinstance(row, PermissionRow)

    seen = []
    view.permission_answered.connect(lambda key, option_id: seen.append((key, option_id)))
    row._buttons["allow_once"].click()

    assert seen == [("req1", "allow_once")]


def test_permission_row_updated_in_place_when_resolved_externally(qapp):
    view, model = _view_and_model()
    perm_view = PermissionView(
        request_key="req1", tool_title="rm -rf", options=[("allow_once", "Allow", "allow_once")]
    )
    entry = model.apply_permission(perm_view)
    view.refresh(entry.id)
    row_before = view._rows[entry.id]

    model.resolve_permission("req1", "allow_once")
    view.refresh(entry.id)

    row_after = view._rows[entry.id]
    assert row_after is row_before  # история решения не потеряна пересозданием
    assert all(not b.isEnabled() for b in row_after._buttons.values())


# --- автопрокрутка -------------------------------------------------------------


def test_autoscroll_sticks_to_bottom_when_already_at_bottom(qapp):
    view, model = _view_and_model()
    bar = view.verticalScrollBar()
    bar.setRange(0, 100)
    bar.setValue(100)

    entry = model.append_user("новое сообщение")
    view.refresh(entry.id)

    assert bar.value() == bar.maximum()


def test_no_autoscroll_when_scrolled_up_reading(qapp):
    view, model = _view_and_model()
    bar = view.verticalScrollBar()
    bar.setRange(0, 100)
    bar.setValue(20)

    entry = model.append_user("новое сообщение")
    view.refresh(entry.id)

    assert bar.value() == 20

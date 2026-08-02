"""Activity row and Houdini test-geometry buddy state transitions."""

from __future__ import annotations

from houdini_agent_panel.ui import thinking


def test_indicator_is_empty_while_activity_is_idle(qapp):
    indicator = thinking.ThinkingIndicator()
    indicator.show()

    assert indicator.isVisible()
    assert indicator._dot.text() == ""
    assert indicator._status._text == ""


def test_start_uses_openclaude_pulse(qapp, monkeypatch):
    monkeypatch.setattr(thinking.random, "choice", lambda values: values[0])
    indicator = thinking.ThinkingIndicator()

    indicator.start()

    assert indicator.is_active()
    assert indicator._dot.text() == "·"
    assert indicator._status._text == "Pondering…"


def test_tool_call_starts_a_new_verb_burst_without_resetting_turn(qapp, monkeypatch):
    choices = iter(("Pondering", "Wrangling"))
    monkeypatch.setattr(thinking.random, "choice", lambda _values: next(choices))
    indicator = thinking.ThinkingIndicator()
    indicator.start()
    started_before = indicator._started_at

    indicator.reset_after_tool()

    assert indicator.is_active()
    assert indicator._verb == "Wrangling"
    assert indicator._started_at == started_before


def test_elapsed_timer_appears_only_after_five_seconds(qapp):
    indicator = thinking.ThinkingIndicator()
    indicator._active = True
    indicator._verb = "Crafting"

    indicator._apply_frame(4_999)
    assert " · " not in indicator._status._text

    indicator._apply_frame(5_000)
    assert indicator._status._text == "Crafting…  ·  5s"


def test_finish_leaves_compact_worked_for_receipt(qapp):
    indicator = thinking.ThinkingIndicator()
    indicator.start()
    indicator.finish()

    assert not indicator.is_active()
    assert indicator._dot.text() == ""
    assert indicator._status._text.startswith("Worked for ")


def test_buddy_action_finishes_and_returns_to_idle(qapp):
    buddy = thinking._BuddySprite()
    buddy.set_buddy("squid")
    buddy.start_action()

    for elapsed in range(0, thinking._ACTION_MS + thinking._TICK_MS, thinking._TICK_MS):
        buddy.advance(elapsed)

    assert buddy._action_elapsed is None
    assert buddy._key == "squid"

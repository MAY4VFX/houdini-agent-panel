"""The strip that shows an agent coming up.

Reported by the artist: "во время загрузки агентов никак не оповещается,
что агент ещё грузится" — two lines flashed past in the feed and then, after
a silence, the model and mode chips simply appeared.
"""

from __future__ import annotations

from houdini_agent_panel.ui import boot_status as boot_mod
from houdini_agent_panel.ui.boot_status import (
    PHASE_CONNECTING,
    PHASE_LAUNCHING,
    PHASE_PREPARING,
    PHASE_READY,
    PHASE_SESSION,
    BootStatus,
)


def test_hidden_until_an_agent_starts(qapp):
    strip = BootStatus()

    assert strip.isHidden() is True


def test_begin_shows_the_first_phase_named_after_the_agent(qapp):
    strip = BootStatus()
    strip.begin("Codex")

    assert strip.isHidden() is False
    assert strip.phase() == PHASE_PREPARING
    assert "Codex" in strip.text()


def test_the_bar_fills_by_steps_completed_not_steps_entered(qapp):
    """Entering the first phase means nothing has finished yet. Showing a
    quarter filled there would make the last, longest step look shortest."""
    strip = BootStatus()
    strip.begin("Codex")
    assert strip.fraction() == 0.0

    fractions = []
    for phase in (PHASE_LAUNCHING, PHASE_CONNECTING, PHASE_SESSION):
        strip.set_phase(phase)
        fractions.append(strip.fraction())

    assert fractions == sorted(fractions), "progress went backwards"
    assert fractions[-1] < 1.0, "the last step is not done just because it started"

    strip.finish()
    assert strip.fraction() == 1.0


def test_a_phase_detail_replaces_the_generic_step_name(qapp):
    """The prep worker knows which package it is fetching; the phase name
    does not. A specific truth beats a generic one."""
    strip = BootStatus()
    strip.begin("Codex")

    strip.set_phase(PHASE_PREPARING, "Downloading @agentclientprotocol/codex-acp")

    assert strip.text() == "Downloading @agentclientprotocol/codex-acp"


def test_finish_says_ready_then_removes_itself(qapp, monkeypatch):
    """Ending is as explicit as starting — the strip does not just vanish
    mid-bar, which reads as a crash rather than as success."""
    monkeypatch.setattr(boot_mod, "_READY_LINGER_MS", 1)
    strip = BootStatus()
    strip.begin("Codex")
    strip.set_phase(PHASE_SESSION)

    strip.finish()
    assert strip.phase() == PHASE_READY
    assert "Codex" in strip.text()
    assert strip.isHidden() is False

    strip._hide_timer.timeout.emit()
    assert strip.isHidden() is True


def test_cancel_hides_immediately_without_claiming_success(qapp):
    """A boot that died leaves nothing behind: a bar frozen partway reads as
    "still coming", and the reason is already in the feed."""
    strip = BootStatus()
    strip.begin("Codex")
    strip.set_phase(PHASE_CONNECTING)

    strip.cancel()

    assert strip.isHidden() is True
    assert strip.phase() != PHASE_READY


def test_unknown_phase_is_ignored_rather_than_guessed_at(qapp):
    strip = BootStatus()
    strip.begin("Codex")
    strip.set_phase(PHASE_CONNECTING)

    strip.set_phase("teleporting")

    assert strip.phase() == PHASE_CONNECTING


def test_the_step_counter_counts_every_phase_but_ready(qapp):
    strip = BootStatus()
    strip.begin("Codex")
    assert strip._step.text() == "1/4"

    strip.set_phase(PHASE_SESSION)
    assert strip._step.text() == "4/4"

    strip.finish()
    assert strip._step.text() == "", "'Ready' is an ending, not a fifth step"


def test_nothing_advances_on_a_timer(qapp):
    """There is no animation driving the bar forward on its own. A bar that
    moves while nothing happens is a lie, and it costs the artist their
    trust in every other indicator the panel draws."""
    strip = BootStatus()
    strip.begin("Codex")
    before = strip.fraction()

    for _ in range(20):
        qapp.processEvents()

    assert strip.fraction() == before


def test_finish_still_works_when_the_panel_is_in_a_background_tab(qapp):
    """`isVisible()` is False for every widget whose panel sits in a tab the
    artist is not looking at. Reading boot state off it meant `finish()`
    bailed out there, and the strip stayed frozen on "Opening a
    conversation" until the tab came forward. Found by rendering the phases,
    not by a test — hence this one."""
    parent = boot_mod.QtWidgets.QWidget()  # never shown, like a background tab
    strip = BootStatus(parent)
    strip.begin("Codex")
    strip.set_phase(PHASE_SESSION)
    assert strip.isVisible() is False

    strip.finish()

    assert strip.phase() == PHASE_READY
    assert strip.is_booting() is False


def test_finish_does_nothing_when_no_boot_was_running(qapp):
    """`session/new` also fires when "+" is pressed on an agent that has been
    up for an hour. "Ready" there would mean nothing."""
    strip = BootStatus()

    strip.finish()

    assert strip.phase() == ""
    assert strip.isHidden() is True


# --- the buddy's entrance ---------------------------------------------------


def test_the_buddy_grows_past_full_size_on_the_way_out_and_settles_back(qapp):
    """Asked for: it should climb out and swell a little, not merely appear.
    The swell has to resolve — a companion left permanently 14% too big is
    a bug, not a flourish."""
    from houdini_agent_panel.ui.thinking import BuddyEntrance

    entrance = BuddyEntrance()
    scales = []
    for i in range(41):
        entrance._t = i / 40
        scales.append(entrance._state()[2])

    assert max(scales) > 1.0, "it never grew past its resting size"
    assert scales[-1] == 1.0, f"it settled at {scales[-1]}, not at full size"


def test_the_hole_opens_before_the_buddy_moves_and_closes_after(qapp):
    from houdini_agent_panel.ui.thinking import BuddyEntrance

    entrance = BuddyEntrance()
    entrance._t = 0.10
    hole, rise, _ = entrance._state()
    assert 0 < hole < 1 and rise == 0, "the buddy started climbing through a shut hole"

    entrance._t = 0.50
    hole, rise, _ = entrance._state()
    assert hole == 1.0 and 0 < rise < 1

    entrance._t = 1.0
    hole, rise, _ = entrance._state()
    assert hole == 0.0 and rise == 1.0, "the hole was left open behind it"


def test_reduced_motion_finishes_at_once_rather_than_never(qapp, monkeypatch):
    """The caller reveals the real buddy on `finished`. Skipping the
    animation without emitting would leave the panel with no companion."""
    from houdini_agent_panel.ui.qt import QtCore
    from houdini_agent_panel.ui.thinking import BuddyEntrance, _BuddySprite

    monkeypatch.setenv("HOUDINI_AGENT_REDUCED_MOTION", "1")
    entrance = BuddyEntrance()
    sprite = _BuddySprite()
    done: list[int] = []
    entrance.finished.connect(lambda: done.append(1))

    entrance.play(sprite, QtCore.QRect(0, 0, 54, 54))

    assert done == [1]
    assert entrance.isHidden() is True


def test_nothing_to_draw_still_finishes(qapp):
    """A missing sprite is not a reason to withhold the companion forever."""
    from houdini_agent_panel.ui.qt import QtCore
    from houdini_agent_panel.ui.thinking import BuddyEntrance

    entrance = BuddyEntrance()
    done: list[int] = []
    entrance.finished.connect(lambda: done.append(1))

    entrance.play(None, QtCore.QRect(0, 0, 54, 54))

    assert done == [1]


def test_a_cancelled_boot_stops_the_entrance_without_pretending_it_ended(qapp):
    from houdini_agent_panel.ui.qt import QtCore
    from houdini_agent_panel.ui.thinking import BuddyEntrance, _BuddySprite

    entrance = BuddyEntrance()
    sprite = _BuddySprite()
    done: list[int] = []
    entrance.finished.connect(lambda: done.append(1))
    entrance.play(sprite, QtCore.QRect(0, 0, 54, 54))

    entrance.skip()

    assert done == [], "a cancelled boot announced a companion that never arrived"
    assert entrance.isHidden() is True
    assert sprite._hold_clock is False, "the sprite was left ticking while hidden"


# --- the join ---------------------------------------------------------------


def test_the_animation_lands_exactly_where_the_sprite_sits(qapp):
    """Reported from Houdini: opening a second tab made the buddy jump — it
    ended up bigger and displaced. Three separate mismatches: the raw 64px
    source pixmap drawn where `_BuddySprite` renders 54px, an invented
    resting position, and a fixed idle frame against a sprite that was
    elsewhere in its cadence.

    At `t = 1` the drawn rect must equal the sprite's own rect, or the
    handover is a jump rather than a cut.
    """
    from houdini_agent_panel.ui.qt import QtCore
    from houdini_agent_panel.ui.thinking import BuddyEntrance, _BuddySprite

    sprite = _BuddySprite()
    entrance = BuddyEntrance()
    target = QtCore.QRect(300, 120, 54, 54)
    entrance.setGeometry(entrance.geometry_for(target))
    entrance.play(sprite, target)

    entrance._t = 1.0
    hole, rise, scale = entrance._state()

    assert scale == 1.0, f"it settled at {scale}x the sprite's size"
    assert rise == 1.0
    assert hole == 0.0
    # The rect the paint code builds, recomputed here from the same numbers.
    local = QtCore.QRect(
        target.x() - entrance.x(), target.y() - entrance.y(), target.width(), target.height()
    )
    ground = local.bottom() + 1
    top = ground - local.height() * scale
    assert top == local.y(), "the buddy came to rest above or below its own place"


def test_the_entrance_draws_the_sprites_live_frame_not_a_fixed_one(qapp):
    """Otherwise the pose jumps at the handover, however good the geometry."""
    from houdini_agent_panel.ui.thinking import _BuddySprite

    sprite = _BuddySprite()
    sprite.advance(0)
    first = sprite.current_frame()
    sprite.advance(10_000)
    later = sprite.current_frame()

    assert not first.isNull()
    assert first.cacheKey() != later.cacheKey(), "current_frame() ignores the clock"


def test_the_sprites_clock_keeps_running_while_the_entrance_draws_it(qapp):
    """The sprite stops its timer when hidden — a mascot nobody can see has
    no right to the artist's frame time. The one exception is while somebody
    else is drawing it, or the pose freezes and the handover jumps a frame."""
    from houdini_agent_panel.ui.thinking import _BuddySprite

    sprite = _BuddySprite()
    sprite.show()
    sprite.hold_clock(True)
    sprite.hide()

    assert sprite._timer.isActive() is True

    sprite.hold_clock(False)
    assert sprite._timer.isActive() is False


def test_the_last_frame_is_pixel_identical_to_the_sprite(qapp):
    """The strongest form of "no jump" available: render the finished
    animation and the real sprite over the same background and diff them.

    This caught a half-pixel offset that no geometry assertion would have —
    `QRect.center()` floors, so on a 54px sprite the drawn rect sat at
    x-0.5 and the final frame resampled to something subtly different
    across the buddy's lower half.
    """
    from houdini_agent_panel.ui.qt import QtCore, QtGui, QtWidgets
    from houdini_agent_panel.ui.thinking import BuddyEntrance, _BuddySprite

    host = QtWidgets.QWidget()
    host.resize(160, 130)
    host.setAutoFillBackground(True)
    sprite = _BuddySprite(host)
    sprite.setGeometry(53, 40, 54, 54)
    entrance = BuddyEntrance(host)
    entrance.setGeometry(entrance.geometry_for(sprite.geometry()))
    host.show()
    qapp.processEvents()
    sprite.advance(0)  # freeze the pose, so only geometry can differ

    at_rest = host.grab().toImage()

    sprite.hide()
    entrance._source = sprite
    entrance._target = QtCore.QRect(
        sprite.x() - entrance.x(), sprite.y() - entrance.y(), sprite.width(), sprite.height()
    )
    entrance.show()
    entrance._t = 1.0
    entrance.update()
    qapp.processEvents()
    last_frame = host.grab().toImage()

    assert last_frame.size() == at_rest.size()
    differing = [
        (x, y)
        for y in range(at_rest.height())
        for x in range(at_rest.width())
        if at_rest.pixel(x, y) != last_frame.pixel(x, y)
    ]
    assert differing == [], (
        f"{len(differing)} pixels differ between the animation's last frame and the "
        f"sprite it hands over to, first at {differing[0]}"
    )


def test_phases_from_a_rejoin_do_not_raise_the_strip(qapp):
    """Reported from Houdini: with the agent already up, closing the panel
    tab and opening it again showed the strip full at 4/4 over a live input
    with the model chips already populated. Reopening a tab replays connect
    and `session/new` against the running agent, and `set_phase` used to
    show the strip on its own — a progress report for a start that had
    happened minutes earlier."""
    strip = BootStatus()

    strip.set_phase(PHASE_CONNECTING)
    strip.set_phase(PHASE_SESSION)

    assert strip.isHidden() is True
    assert strip.is_booting() is False
    assert strip.fraction() == 0.0

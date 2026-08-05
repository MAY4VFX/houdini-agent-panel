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
    """The caller shows the real buddy on `finished`. Skipping the animation
    without emitting would leave the panel with no companion at all."""
    from houdini_agent_panel.ui.qt import QtGui
    from houdini_agent_panel.ui.thinking import BuddyEntrance

    monkeypatch.setenv("HOUDINI_AGENT_REDUCED_MOTION", "1")
    entrance = BuddyEntrance()
    done: list[int] = []
    entrance.finished.connect(lambda: done.append(1))

    entrance.play(QtGui.QPixmap(8, 8))

    assert done == [1]
    assert entrance.isHidden() is True


def test_an_empty_sprite_still_finishes(qapp):
    """A missing image is not a reason to withhold the companion forever."""
    from houdini_agent_panel.ui.qt import QtGui
    from houdini_agent_panel.ui.thinking import BuddyEntrance

    entrance = BuddyEntrance()
    done: list[int] = []
    entrance.finished.connect(lambda: done.append(1))

    entrance.play(QtGui.QPixmap())

    assert done == [1]


def test_a_cancelled_boot_stops_the_entrance_without_pretending_it_ended(qapp):
    from houdini_agent_panel.ui.qt import QtGui
    from houdini_agent_panel.ui.thinking import BuddyEntrance

    entrance = BuddyEntrance()
    done: list[int] = []
    entrance.finished.connect(lambda: done.append(1))
    entrance.play(QtGui.QPixmap(8, 8))

    entrance.skip()

    assert done == [], "a cancelled boot announced a companion that never arrived"
    assert entrance.isHidden() is True

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

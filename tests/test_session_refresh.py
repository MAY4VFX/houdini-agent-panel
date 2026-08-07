"""The update banner used to go up to a day stale.

Reported by the owner: he restarted Houdini repeatedly on 0.6.1 while
0.7.0 and 0.7.1 both shipped, and was told nothing every time — the
cached "you're current" answer from before either release existed was
still, by the old day-long rule, "fresh." `updates.py` now has two
windows instead of one (`_FRESH_START_MAX_AGE`/`_SESSION_MAX_AGE`, see
their own comments); these tests cover the panel side that decides WHICH
one applies and when a re-check happens at all without a restart.
"""

from __future__ import annotations

import pytest

from houdini_agent_panel.ui import panel as panel_mod


@pytest.fixture(autouse=True)
def isolated(qapp, monkeypatch):
    monkeypatch.setattr(panel_mod.scene, "hip_dir", lambda: "/tmp")
    monkeypatch.setattr(
        panel_mod.scene, "mcp_servers",
        lambda: [{"name": "fxhoudini", "command": "python", "args": [], "env": []}],
    )
    monkeypatch.setattr(panel_mod._OrphanSweepWorker, "start", lambda self: None)
    monkeypatch.setattr(panel_mod._RefreshWorker, "start", lambda self: None)
    panel_mod.reset_shared_state_for_tests()
    yield
    panel_mod.reset_shared_state_for_tests()


def test_boot_starts_the_session_refresh_timer(qapp):
    widget = panel_mod.AgentPanel()
    qapp.processEvents()

    timer = widget._session_refresh_timer
    assert timer is not None
    assert timer.isActive()
    assert timer.interval() == panel_mod._SESSION_REFRESH_INTERVAL_MS
    widget.shutdown()


def test_the_session_refresh_interval_matches_updates_own_session_window(qapp):
    """One number, not two written separately that could drift apart —
    see `_SESSION_REFRESH_INTERVAL_MS`'s own comment."""
    from houdini_agent_panel import updates

    expected_ms = int(updates._SESSION_MAX_AGE.total_seconds() * 1000)
    assert panel_mod._SESSION_REFRESH_INTERVAL_MS == expected_ms


def test_the_boot_check_is_a_fresh_start(qapp):
    widget = panel_mod.AgentPanel()
    qapp.processEvents()

    assert widget._refresh_worker is not None
    assert widget._refresh_worker._fresh_start is True
    widget.shutdown()


def test_a_session_refresh_tick_starts_a_worker_with_fresh_start_false(qapp):
    """The report's actual fix: a panel that has been open a while checks
    again on its own, without the artist ever restarting Houdini."""
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    boot_worker = widget._refresh_worker

    widget._on_session_refresh_due()

    assert widget._refresh_worker is not None
    assert widget._refresh_worker is not boot_worker
    assert widget._refresh_worker._fresh_start is False
    widget.shutdown()


def test_a_session_refresh_tick_skips_while_the_previous_worker_is_still_running(qapp, monkeypatch):
    """`_SESSION_REFRESH_INTERVAL_MS` is hours, a real check is seconds —
    a still-running worker at tick time is defensive, not the expected
    path, and must not be torn down or duplicated mid-flight."""
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    boot_worker = widget._refresh_worker
    monkeypatch.setattr(boot_worker, "isRunning", lambda: True)

    widget._on_session_refresh_due()

    assert widget._refresh_worker is boot_worker, "a still-running worker must not be replaced"
    widget.shutdown()


def test_shutdown_stops_the_session_refresh_timer(qapp):
    widget = panel_mod.AgentPanel()
    qapp.processEvents()

    widget.shutdown()

    assert widget._session_refresh_timer is None

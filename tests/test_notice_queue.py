"""One notice strip, more than one thing that wants it.

Diagnosed from a live report: the panel knew 0.8.8 was out — it was sitting
right there in `cache/updates.json` — but there was nowhere to say so,
because the sign-in offer (`_maybe_offer_sign_in`), never dismissed, had
already claimed the only notice strip and stayed there. The owner sat on
0.8.5 for three released versions, one of which was the very fix for the
sign-in bug keeping the offer on screen in the first place.

These tests pin the fix: a notice arriving while the strip is occupied
waits in line instead of being silently discarded, and it gets its turn the
moment the strip frees up. `_panel_update_restart_pending`'s own "never
replaced by another banner" guarantee (already covered in test_ui_panel.py)
must keep holding — nothing here is meant to touch that.
"""

from __future__ import annotations

import pytest

from houdini_agent_panel import settings as settings_mod, signin_evidence
from houdini_agent_panel.announcements import Announcement
from houdini_agent_panel.client import AgentInfo
from houdini_agent_panel.ui import panel as panel_mod
from houdini_agent_panel.updates import Update


@pytest.fixture(autouse=True)
def isolated(qapp, monkeypatch):
    monkeypatch.setattr(panel_mod.scene, "hip_dir", lambda: "/tmp")
    monkeypatch.setattr(
        panel_mod.scene, "mcp_servers",
        lambda: [{"name": "fxhoudini", "command": "python", "args": [], "env": []}],
    )
    monkeypatch.setattr(panel_mod._RefreshWorker, "start", lambda self: None)
    monkeypatch.setattr(panel_mod.shellenv, "merged", lambda base, overrides=None: dict(base))
    panel_mod.reset_shared_state_for_tests()
    yield
    panel_mod.reset_shared_state_for_tests()


def _info() -> AgentInfo:
    return AgentInfo(
        name="Claude Agent", version="1", protocol_version=1,
        supports_image=False, supports_audio=False, supports_embedded_context=False,
        supports_load_session=False, supports_logout=False, auth_methods=(),
    )


def _signed_in_offer_showing(qapp, monkeypatch, agent_id: str = "claude-acp"):
    """Get a panel with the persistent sign-in offer already occupying the
    one notice strip — the exact starting state the live report began
    from."""
    monkeypatch.setattr(signin_evidence, "has_credential_evidence", lambda *a, **k: False)
    monkeypatch.setattr(panel_mod.AgentPanel, "_start_agent", lambda self, agent_id: None)
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    widget._on_agent_chosen(agent_id)
    client = panel_mod.shared_client(agent_id)
    client.connected.emit(_info())
    qapp.processEvents()
    assert widget._notice.isHidden() is False, "setup expects the offer to already be showing"
    return widget


def _update_result(update: Update):
    class _Result:
        announcements: list = []
        updates = [update]

    return _Result()


#: An "agent" update for a target that's never been installed is never
#: `_update_is_stale` (that check only fires once something is actually on
#: disk to compare against) — a "panel" update pinned to a literal version
#: string is the wrong fixture here, since it goes stale the moment the
#: dev tree's own `__version__` moves past it.
def _agent_update(target: str = "kimi", latest: str = "2.0") -> Update:
    return Update(kind="agent", target=target, label=f"Kimi CLI {latest}", current="1.0", latest=latest)


def test_an_update_arriving_behind_the_signin_offer_is_not_lost(qapp, monkeypatch):
    """The bug as diagnosed: an update banner arriving from a periodic
    refresh while the sign-in offer is showing used to just vanish — this
    proves it survives, waiting for its turn."""
    widget = _signed_in_offer_showing(qapp, monkeypatch)
    offer_text = widget._notice._label.text()

    update = _agent_update()
    widget._on_refresh_done(_update_result(update), [])
    qapp.processEvents()

    # Still the offer on screen — arriving second must not steal the strip
    # out from under something the artist hasn't acted on yet.
    assert widget._notice._label.text() == offer_text
    assert widget._active_update is None, "not shown yet, so nothing to press"
    assert [qid for qid, _ in widget._notice_queue] == [update.target]

    widget.shutdown()


def test_the_queued_update_appears_once_the_offer_is_dismissed(qapp, monkeypatch):
    widget = _signed_in_offer_showing(qapp, monkeypatch)
    update = _agent_update()
    widget._on_refresh_done(_update_result(update), [])
    qapp.processEvents()

    widget._notice._on_close()  # the artist's own ✕ on the sign-in offer

    assert widget._notice.isHidden() is False, "the queued update must take the now-empty strip"
    assert "Kimi CLI 2.0" in widget._notice._label.text()
    assert widget._active_update is update
    assert widget._notice_queue == []

    widget.shutdown()


def test_the_queued_update_appears_once_sign_in_is_clicked(qapp, monkeypatch):
    """Same guarantee through the OTHER way the offer leaves the strip —
    the artist actually clicking "Sign in", not just dismissing it."""
    widget = _signed_in_offer_showing(qapp, monkeypatch)
    identifier = widget._notice._id
    monkeypatch.setattr(widget, "_offer_sign_in", lambda: None)

    update = _agent_update()
    widget._on_refresh_done(_update_result(update), [])
    qapp.processEvents()

    widget._notice.action_clicked.emit(identifier, "")

    assert widget._notice.isHidden() is False
    assert "Kimi CLI 2.0" in widget._notice._label.text()

    widget.shutdown()


def test_a_second_update_behind_the_first_replaces_it_in_the_queue(qapp, monkeypatch):
    """A later, fresher refresh result for the SAME target must not pile up
    a second, stale queue entry behind the first."""
    widget = _signed_in_offer_showing(qapp, monkeypatch)
    stale = _agent_update(latest="1.9")
    fresh = _agent_update(latest="2.0")
    widget._on_refresh_done(_update_result(stale), [])
    widget._on_refresh_done(_update_result(fresh), [])
    qapp.processEvents()

    assert len(widget._notice_queue) == 1
    widget._notice._on_close()

    assert "Kimi CLI 2.0" in widget._notice._label.text()

    widget.shutdown()


def test_restart_pending_still_dominates_everything_queued_or_not(qapp, monkeypatch):
    """The existing, explicit guarantee (`_on_refresh_done`'s own top guard)
    must keep holding: once a self-update finished and is waiting on a
    Houdini restart, nothing — sign-in offer included — gets to interrupt
    it, and a periodic refresh doesn't even try."""
    widget = _signed_in_offer_showing(qapp, monkeypatch)
    update = Update(
        kind="panel", target="houdini-agent-panel", label="houdini-agent-panel",
        current="1.1.0", latest="1.2.0",
    )
    widget._on_panel_update_succeeded(update)
    restart_text = widget._notice._label.text()
    assert "restart" in restart_text.lower()

    other = Update(kind="agent", target="kimi", label="Kimi CLI 2.0", current="1.0", latest="2.0")

    class _Result:
        announcements: list = [Announcement(id="a1", severity="info", title="unrelated")]
        updates = [other]

    widget._on_refresh_done(_Result(), [])
    qapp.processEvents()

    assert widget._notice._label.text() == restart_text
    assert widget._notice_queue == [], "the guard upstream must stop this before it ever queues"

    widget.shutdown()

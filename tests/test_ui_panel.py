"""Panel integration.

What's checked here isn't rendering but the wiring: who outlives whom, what
happens to a second tab, where a permission answer goes. The client is the
real one — just never started: its Qt signals are real, and emitting them from
a test is more honest than substituting a stand-in with a similar name.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from types import SimpleNamespace

import pytest

from houdini_agent_panel import sessions, settings as settings_mod
from houdini_agent_panel.ui import panel as panel_mod
from houdini_agent_panel.ui.qt import QtCore


@pytest.fixture(autouse=True)
def isolated_panel_state(qapp, monkeypatch):
    """A fresh process-wide singleton per test, and no network out of _boot."""
    monkeypatch.setattr(panel_mod.scene, "hip_dir", lambda: "/tmp")
    monkeypatch.setattr(
        panel_mod.scene,
        "mcp_servers",
        lambda: [{"name": "fxhoudini", "command": "python", "args": [], "env": []}],
    )
    # `_boot` registers a real `hou.hipFile` callback outside this fixture —
    # no `hou` here, so stand in with something `shutdown()` can still hand
    # back to `unwatch_hip_dir_changes` symmetrically.
    monkeypatch.setattr(panel_mod.scene, "watch_hip_dir_changes", lambda callback: "fake-watch-handle")
    monkeypatch.setattr(panel_mod.scene, "unwatch_hip_dir_changes", lambda handle: None)
    monkeypatch.setattr(panel_mod._RefreshWorker, "start", lambda self: None)
    # `_boot` also kicks off an orphan sweep (may-hub task, 2026-08-04) —
    # same reasoning as `_RefreshWorker` above: no real background thread
    # started as a side effect of every test in this file booting a panel.
    # Tests that actually exercise the sweep wiring restore the real
    # `start` themselves.
    monkeypatch.setattr(panel_mod._OrphanSweepWorker, "start", lambda self: None)
    panel_mod.reset_shared_state_for_tests()
    yield
    panel_mod.reset_shared_state_for_tests()


def _make_panel(qapp):
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    return widget


def _session(session_id: str = "s1") -> sessions.SessionState:
    return sessions.SessionState(
        session_id=session_id, title="New conversation", cwd="/tmp", created_at=0.0
    )


def test_default_agent_is_named_before_the_deferred_boot_frame(qapp, monkeypatch):
    """The pane's first frame must not show a bare dot while _boot is queued."""
    current = settings_mod.Settings(default_agent="opencode", autostart_agent=False)
    monkeypatch.setattr(panel_mod.settings_mod, "load", lambda: current)
    queued = []
    monkeypatch.setattr(panel_mod.QtCore.QTimer, "singleShot", lambda *args: queued.append(args))

    widget = panel_mod.AgentPanel()

    assert widget._header._agent_button.text() == "OpenCode"
    assert queued, "boot should remain deferred"
    widget.shutdown()


def test_without_default_agent_panel_opens_on_agents_settings(qapp):
    """First open: no agent picked, so the human is shown what there is to
    pick from — which is now the "Agents" block in settings, not a separate
    screen."""
    widget = _make_panel(qapp)
    widget._boot()

    assert widget._pages.currentIndex() == panel_mod.AgentPanel.PAGE_SETTINGS
    assert widget._settings_view._scroll.verticalScrollBar().value() == 0
    widget.shutdown()


# --- rescoping when the scene changes underneath an open tab ---------------
#
# `_boot()` used to compute `scene.hip_dir()` and run `_restore_conversations`
# exactly once. A tab that started against one scene (often a fresh, unsaved
# one — `hip_dir()`'s own `$HOME` fallback) and then had the artist open a
# real project file into the SAME Houdini session kept the old scope for the
# rest of its life: conversations already on disk for the folder actually
# open never appeared, because nothing ever asked `hip_dir()` again. Reported
# for real: a live panel's pool held a "New chat" scoped to `$HOME` beside the
# one correctly-scoped session that existed only because a brand-new message
# reads `hip_dir()` fresh.


def test_scene_change_rescopes_the_header_and_restores_its_conversations(qapp, monkeypatch, tmp_path):
    from houdini_agent_panel import conversations_store as store

    old_cwd = str(tmp_path / "untitled_fallback")
    new_cwd = str(tmp_path / "shots" / "shot010")
    current_cwd = {"value": old_cwd}
    monkeypatch.setattr(panel_mod.scene, "hip_dir", lambda: current_cwd["value"])

    captured: list = []
    monkeypatch.setattr(
        panel_mod.scene, "watch_hip_dir_changes", lambda callback: captured.append(callback) or callback
    )

    conversation = store.StoredConversation.new(title="Rotor pyro", cwd=new_cwd, agent_id="claude-acp")
    conversation.entries = [{"kind": "user", "id": "e1", "text": "make dust"}]
    store.save([conversation])

    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("claude-acp")
    widget._boot()
    assert captured, "watch_hip_dir_changes was never registered"
    assert widget._header._cwd_label.text() == old_cwd
    assert "Rotor pyro" not in [s.title for s in widget._pool.all()], (
        "a conversation scoped to the scene not yet open must not appear early"
    )

    # The artist opens the real scene into this same Houdini session.
    current_cwd["value"] = new_cwd
    captured[0]("loaded")

    assert widget._header._cwd_label.text() == new_cwd
    assert "Rotor pyro" in [s.title for s in widget._pool.all()]
    widget.shutdown()


def test_opening_a_different_scene_removes_the_old_scenes_live_conversation(qapp, monkeypatch, tmp_path):
    """The bug the owner reproduced: a conversation stays open (and
    `current`) after File > Open swaps the scene underneath it — even
    though its own `SessionState.cwd` now names a folder that isn't open
    any more. `kind="loaded"`/`"new"` sweep it out of the pool; a
    conversation genuinely scoped to the NEW folder is left untouched."""
    old_cwd = str(tmp_path / "old_shot")
    new_cwd = str(tmp_path / "new_shot")
    current_cwd = {"value": old_cwd}
    monkeypatch.setattr(panel_mod.scene, "hip_dir", lambda: current_cwd["value"])
    captured: list = []
    monkeypatch.setattr(
        panel_mod.scene, "watch_hip_dir_changes", lambda callback: captured.append(callback) or callback
    )

    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("claude-acp")
    widget._boot()
    old_state = sessions.SessionState(
        session_id="old-live", title="Old shot work", cwd=old_cwd, created_at=0.0
    )
    other_state = sessions.SessionState(
        session_id="new-live", title="Already the new shot", cwd=new_cwd, created_at=0.0
    )
    widget._pool.add(old_state)
    widget._pool.add(other_state)
    widget._set_current_session("old-live")

    current_cwd["value"] = new_cwd
    captured[0]("loaded")

    ids = [s.session_id for s in widget._pool.all()]
    assert "old-live" not in ids, "the old scene's conversation must not survive opening a new one"
    assert "new-live" in ids, "a conversation already scoped to the new scene must not be swept too"
    assert widget._current_session_id != "old-live"
    widget.shutdown()


def test_opening_a_different_scene_never_closes_the_swept_sessions_agent_session(
    qapp, monkeypatch, tmp_path
):
    """The non-negotiable part: sweeping a stale conversation out of the
    pool must never send `session/cancel` or `session/close` — the agent
    keeps running it, untouched, exactly as `_on_hip_dir_changed`'s own
    docstring promises. Only `SessionPool.remove` (a local bookkeeping
    change) is allowed; `_release_session`/`close_session` is not."""
    old_cwd = str(tmp_path / "old_shot")
    new_cwd = str(tmp_path / "new_shot")
    current_cwd = {"value": old_cwd}
    monkeypatch.setattr(panel_mod.scene, "hip_dir", lambda: current_cwd["value"])
    captured: list = []
    monkeypatch.setattr(
        panel_mod.scene, "watch_hip_dir_changes", lambda callback: captured.append(callback) or callback
    )

    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("claude-acp")
    widget._boot()
    widget._pool.add(
        sessions.SessionState(session_id="old-live", title="Busy", cwd=old_cwd, created_at=0.0, busy=True)
    )
    widget._set_current_session("old-live")

    closed: list = []
    monkeypatch.setattr(panel_mod.shared_client("claude-acp"), "close_session", closed.append)

    current_cwd["value"] = new_cwd
    captured[0]("loaded")

    assert closed == [], "a still-running turn must keep running — its session must not be closed"
    widget.shutdown()


def test_saving_to_a_new_path_rebinds_the_live_conversation_instead_of_removing_it(
    qapp, monkeypatch, tmp_path
):
    """`kind="saved"`: the scene got a new/first real path. The conversation
    that was open when it happened MOVES with it — same session, same
    pool entry, `cwd` updated in place — rather than being swept the way
    an actual File > Open/New would."""
    old_cwd = str(tmp_path / "untitled_fallback")
    new_cwd = str(tmp_path / "shots" / "shot010")
    current_cwd = {"value": old_cwd}
    monkeypatch.setattr(panel_mod.scene, "hip_dir", lambda: current_cwd["value"])
    captured: list = []
    monkeypatch.setattr(
        panel_mod.scene, "watch_hip_dir_changes", lambda callback: captured.append(callback) or callback
    )

    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("claude-acp")
    widget._boot()
    widget._pool.add(
        sessions.SessionState(session_id="live-1", title="Untitled work", cwd=old_cwd, created_at=0.0)
    )
    widget._set_current_session("live-1")

    current_cwd["value"] = new_cwd
    captured[0]("saved")

    ids = [s.session_id for s in widget._pool.all()]
    assert "live-1" in ids, "a saved scene's conversation must not be removed"
    assert widget._pool.get("live-1").cwd == new_cwd
    assert widget._current_session_id == "live-1"
    widget.shutdown()


def test_shutdown_unwatches_the_exact_handle_it_was_given(qapp, monkeypatch):
    handle = object()
    unwatched = []
    monkeypatch.setattr(panel_mod.scene, "watch_hip_dir_changes", lambda callback: handle)
    monkeypatch.setattr(panel_mod.scene, "unwatch_hip_dir_changes", unwatched.append)

    widget = panel_mod.AgentPanel()
    widget._boot()

    widget.shutdown()

    assert unwatched == [handle]


def test_a_watcher_registration_failure_does_not_stop_boot(qapp, monkeypatch):
    """`hou.hipFile` behaving unexpectedly is not a reason to leave the
    artist with no agent and no restored history — see `_maybe_sweep_
    orphans` for the same posture applied to a different optional extra."""

    def _boom(callback):
        raise RuntimeError("no hipFile on this build")

    monkeypatch.setattr(panel_mod.scene, "watch_hip_dir_changes", _boom)

    widget = panel_mod.AgentPanel()
    widget._boot()

    assert widget._hip_watch_handle is None
    widget.shutdown()


def test_settings_button_toggles_open_then_closed(qapp):
    """One control, not "Sign in…"'s own back-and-forth but the same idea
    applied to the "…" button: the owner wanted the back button gone and
    the SAME control that opens Settings to close it again on a second
    press — no separate close affordance. `_toggle_settings` is what the
    header's `settings_clicked` now drives (it used to always open)."""
    widget = _make_panel(qapp)
    widget._show_page(widget.PAGE_TRANSCRIPT)

    widget._toggle_settings()
    assert widget._pages.currentIndex() == widget.PAGE_SETTINGS

    widget._toggle_settings()
    assert widget._pages.currentIndex() == widget.PAGE_TRANSCRIPT
    widget.shutdown()


def test_settings_button_looks_pressed_while_settings_is_open(qapp):
    """Losing the back button means the "…" button itself has to say
    which of "open" or "close" a click will do next — a checked/pressed
    look is that signal (owner: it should read as linked to the state,
    not a guess). Driven from `_show_page` itself, not only the button's
    own click, so it stays correct no matter which route opened or closed
    Settings (Escape, an agent switch back to the transcript, ...)."""
    widget = _make_panel(qapp)
    widget._show_page(widget.PAGE_TRANSCRIPT)
    assert widget._header._settings_button.isChecked() is False

    widget._show_page(widget.PAGE_SETTINGS)
    assert widget._header._settings_button.isChecked() is True

    widget._show_page(widget.PAGE_TRANSCRIPT)
    assert widget._header._settings_button.isChecked() is False
    widget.shutdown()


def test_escape_closes_settings_when_it_is_open(qapp):
    """The back button's removal has to leave a real way out — Escape is
    one of the two named explicitly (the other being the "…" toggle
    itself, covered above). Driven through the same `QShortcut` the real
    panel wires up in `_build`, not a direct call to a private handler —
    a shortcut with the wrong context or key would pass a test that only
    called the handler by name and still leave the artist stuck for real.
    """
    from PySide6 import QtTest

    widget = _make_panel(qapp)
    widget.show()
    widget.activateWindow()
    widget.setFocus()
    qapp.processEvents()
    widget._show_page(widget.PAGE_SETTINGS)
    assert widget._pages.currentIndex() == widget.PAGE_SETTINGS

    QtTest.QTest.keyClick(widget, QtCore.Qt.Key_Escape)
    qapp.processEvents()

    assert widget._pages.currentIndex() == widget.PAGE_TRANSCRIPT
    widget.shutdown()


def test_escape_does_nothing_when_settings_is_not_open(qapp):
    """Scoped to Settings only — Escape must not, say, jump out of a
    transcript or cancel something unrelated just because the shortcut is
    always installed."""
    from PySide6 import QtTest

    widget = _make_panel(qapp)
    widget.show()
    widget.activateWindow()
    widget.setFocus()
    qapp.processEvents()
    widget._show_page(widget.PAGE_TRANSCRIPT)

    QtTest.QTest.keyClick(widget, QtCore.Qt.Key_Escape)
    qapp.processEvents()

    assert widget._pages.currentIndex() == widget.PAGE_TRANSCRIPT
    widget.shutdown()


def test_settings_overlay_paints_a_visibly_different_shade(qapp):
    """Owner: Settings should "lay as an overlay, differing slightly in
    shade from the rest of the background" — checked by actually
    rendering both pages and comparing real pixels, not just confirming a
    stylesheet string was set. A `QScrollArea` paints its own opaque
    background regardless of what's styled underneath it (real bug found
    while building this: the overlay's background was invisible under the
    scroll area that covers nearly the whole page, fixed by making the
    scroll area and its viewport transparent — `SettingsView.__init__`'s
    own note on `_scroll`), so this has to grab pixels from deep inside
    the scrolled content, not just the thin header strip above it.
    """
    widget = _make_panel(qapp)
    widget.resize(500, 700)
    widget.show()
    qapp.processEvents()

    widget._show_page(widget.PAGE_TRANSCRIPT)
    qapp.processEvents()
    transcript_pixel = widget.grab().toImage().pixelColor(100, 300)

    widget._show_page(widget.PAGE_SETTINGS)
    qapp.processEvents()
    settings_pixel = widget.grab().toImage().pixelColor(100, 300)

    assert settings_pixel != transcript_pixel
    widget.shutdown()


def test_two_panels_on_the_same_agent_share_one_client_and_one_pool(qapp):
    """Two tabs on the SAME agent, one process. A direct design.md
    requirement: "one agent, many sessions" — per agent id, not per
    Houdini process (see `AgentPanel._agent_id`): a tab on a DIFFERENT
    agent gets its own, covered by
    `test_agent_switch.py::test_switching_one_tabs_agent_does_not_disturb_another_tabs_agent`.
    """
    current = settings_mod.load()
    current.default_agent = "claude-acp"
    current.autostart_agent = False
    settings_mod.save(current)

    first = _make_panel(qapp)
    second = _make_panel(qapp)
    assert first._agent_id == second._agent_id == "claude-acp"

    assert first._pool is second._pool
    assert panel_mod.shared_client(first._agent_id) is panel_mod.shared_client(second._agent_id)

    first.shutdown()
    second.shutdown()


def test_shared_client_carries_its_own_agent_id(qapp):
    """`orphans.record_started` (client.py) needs to know which agent a
    process belongs to, and the only place that fact exists is here —
    `shared_client` builds each `AcpClient` for a specific agent id and
    never reuses one across ids."""
    client = panel_mod.shared_client("claude-acp")
    assert client._agent_id == "claude-acp"


def test_boot_sweeps_orphans_exactly_once_per_process_not_per_tab(qapp, monkeypatch):
    """Two tabs opening together must not both read-modify-write
    `orphans.py`'s JSON file at once — and there's nothing left to find
    after the first tab's sweep anyway (may-hub task, 2026-08-04)."""
    starts = []
    monkeypatch.setattr(panel_mod._OrphanSweepWorker, "start", lambda self: starts.append(self))

    first = _make_panel(qapp)
    second = _make_panel(qapp)

    assert len(starts) == 1
    first.shutdown()
    second.shutdown()


def test_orphans_swept_reports_cleaned_agents_in_the_feed(qapp):
    """A silent cleanup is indistinguishable from nothing having been
    wrong — the artist should know a past crash left something running."""
    from houdini_agent_panel import orphans

    widget = _make_panel(qapp)
    widget._boot()

    widget._on_orphans_swept([orphans.SweptAgent(agent_id="claude-acp", pid=4242)])

    # `_note` appends an error-styled entry to whatever session is
    # currently showing; simplest reliable check is that SOME entry
    # mentions the cleaned agent.
    current = widget._current_session()
    session_id = current.session_id if current else "__idle__"
    records = widget._model(session_id).to_records()
    assert any("claude-acp" in r.get("text", "") or "Claude Agent" in r.get("text", "") for r in records), records
    widget.shutdown()


def test_nothing_swept_says_nothing_in_the_feed(qapp):
    widget = _make_panel(qapp)
    widget._boot()
    session_id = widget._current_session().session_id if widget._current_session() else "__idle__"
    before = list(widget._model(session_id).to_records())

    widget._on_orphans_swept([])

    after = list(widget._model(session_id).to_records())
    assert after == before
    widget.shutdown()


def test_buddy_selection_is_saved_and_restored(qapp):
    from houdini_agent_panel import settings as settings_mod

    first = _make_panel(qapp)
    first._composer.buddy_selected.emit("squid")
    assert settings_mod.load().buddy == "squid"
    first.shutdown()

    second = _make_panel(qapp)
    assert second._composer._buddy._key == "squid"
    second.shutdown()


def test_closing_one_tab_leaves_the_other_receiving_updates(qapp):
    """The most expensive spot in the integration.

    The client is shared, so a naive `signal.disconnect()` in one tab's
    shutdown() would unsubscribe its neighbour too: that one would keep
    looking alive and stay silent in reply to every prompt.
    """
    first = _make_panel(qapp)
    second = _make_panel(qapp)
    assert first._agent_id == second._agent_id  # both "" — same client either way

    client = panel_mod.shared_client(first._agent_id)
    state = _session()
    client.session_started.emit(state.session_id, state)
    qapp.processEvents()

    first.shutdown()
    qapp.processEvents()

    client.message_chunk.emit(state.session_id, "m1", "hello")
    qapp.processEvents()

    entries = second._model(state.session_id).entries()
    assert [entry.text for entry in entries if entry.kind == "agent"] == ["hello"]

    second.shutdown()


def test_clicking_a_restored_conversation_actually_shows_it(qapp, monkeypatch):
    """Reported for real: his entire history was unreachable — clicking a
    past conversation in the drawer left the pane showing whatever screen
    was up before (the sign-in advice text, in his case), not the
    conversation's own contents. The drawer itself was fine (right count,
    right titles, the row highlighted as selected) and the transcript MODEL
    was fine too (loaded, correctly keyed) — only the visible PAGE never
    came forward to PAGE_TRANSCRIPT to show it. `_on_session_started` is
    the one place that already calls `_show_page(PAGE_TRANSCRIPT)`
    explicitly; the drawer's click goes straight to `_set_current_session`
    with no such step."""
    from houdini_agent_panel import conversations_store as store

    conversation = store.StoredConversation.new(title="che to ne mogu vs", cwd="/tmp", agent_id="claude-acp")
    conversation.entries = [
        {"kind": "user", "id": "u1", "text": "hello there"},
        {"kind": "agent", "id": "a1", "text": "hi, how can I help"},
    ]
    store.save([conversation])

    widget = _make_panel(qapp)
    widget._rejoin_agent("claude-acp")
    widget._restore_conversations()
    restored_key = panel_mod._RESTORED_PREFIX + conversation.id
    assert widget._pool.get(restored_key) is not None, "the conversation never entered the pool"

    # The artist is on the sign-in screen — a very real starting point
    # (`_maybe_offer_sign_in` lands here on connect) — when they open the
    # drawer and click an old conversation.
    widget._show_page(widget.PAGE_AUTH)

    widget._conversations.session_selected.emit(restored_key)

    assert widget._pages.currentIndex() == widget.PAGE_TRANSCRIPT, (
        "selecting a conversation must bring the transcript page forward"
    )
    assert [widget._model(restored_key).entries()[i].text for i in range(2)] == [
        "hello there", "hi, how can I help",
    ]
    widget.shutdown()


def test_switching_conversation_in_one_tab_does_not_move_the_other(qapp):
    """Issue #21: two tabs share one `SessionPool` — same session list, same
    live agent process, per `sessions.py`'s own docstring — but "which
    conversation is on screen" used to be a single shared field on the pool
    too (`_current_id`/`set_current`/`current_changed`). Picking a different
    conversation in one tab's drawer silently dragged every other open tab
    onto that same conversation. `_current_session_id` now lives on
    `AgentPanel` itself, one per tab.
    """
    first = _make_panel(qapp)
    second = _make_panel(qapp)

    client = panel_mod.shared_client(first._agent_id)
    state_a = _session("a")
    state_b = _session("b")
    client.session_started.emit(state_a.session_id, state_a)
    qapp.processEvents()
    client.session_started.emit(state_b.session_id, state_b)
    qapp.processEvents()

    # Each tab independently ended up on whichever session IT last opened
    # (both saw both `session_started` emissions, since the pool is shared)
    # — pin down the starting point explicitly rather than assume it.
    first._set_current_session("a")
    second._set_current_session("a")
    assert first._current_session().session_id == "a"
    assert second._current_session().session_id == "a"

    first._set_current_session("b")

    assert first._current_session().session_id == "b"
    assert second._current_session().session_id == "a", (
        "a different tab's conversation switch must not move this one"
    )

    first.shutdown()
    second.shutdown()


def test_deleting_a_conversation_from_one_tab_only_falls_back_in_that_tab(qapp):
    """The other half of #21: removal must be scoped to the tab that had the
    deleted conversation open, not broadcast through the shared pool."""
    first = _make_panel(qapp)
    second = _make_panel(qapp)

    client = panel_mod.shared_client(first._agent_id)
    for sid in ("a", "b", "c"):
        client.session_started.emit(sid, _session(sid))
        qapp.processEvents()

    first._set_current_session("b")
    second._set_current_session("c")

    first._on_session_removed("b")

    assert second._current_session().session_id == "c", (
        "a sibling tab's own conversation must survive someone else's delete"
    )
    assert first._current_session() is not None
    assert first._current_session().session_id != "b"

    first.shutdown()
    second.shutdown()


def test_last_tab_closing_stops_the_agent(qapp):
    """While one tab is alive the conversation goes on; once the last tab
    ON THAT AGENT is closed there is nothing left to keep its process for.

    The dangerous spot team-lead called out for this exact refactor: get
    the ref-count wrong one way and closing a sibling tab kills an agent
    someone else is still using; get it wrong the other way and the
    process outlives every tab that ever asked for it. Both directions
    checked here, with a real agent id shared by two tabs — not the empty
    "no agent chosen" placeholder, which would pass this test either way.
    """
    current = settings_mod.load()
    current.default_agent = "claude-acp"
    current.autostart_agent = False
    settings_mod.save(current)

    first = _make_panel(qapp)
    second = _make_panel(qapp)
    assert first._agent_id == second._agent_id == "claude-acp"

    first.shutdown()
    assert "claude-acp" in panel_mod._shared_clients, (
        "closing one of two tabs on the same agent must not stop it — the other tab still needs it"
    )

    second.shutdown()
    assert "claude-acp" not in panel_mod._shared_clients, (
        "closing the last tab on an agent must actually stop it"
    )


def test_last_tab_closing_also_clears_that_agents_dead_sessions(qapp):
    """A session id belongs to the process that issued it — leaving it in
    the pool after that process is stopped would greet the next tab that
    picks this SAME agent with session ids from a process that no longer
    exists (the exact bug `_on_agent_chosen`'s own cleanup already guards
    against for a mid-tab switch; closing the last tab is the other way
    an agent's connection ends, and needs the same guard).
    """
    from houdini_agent_panel import sessions

    current = settings_mod.load()
    current.default_agent = "claude-acp"
    current.autostart_agent = False
    settings_mod.save(current)

    widget = _make_panel(qapp)
    client = panel_mod.shared_client("claude-acp")
    client.session_started.emit("s1", sessions.SessionState(
        session_id="s1", title="New conversation", cwd="/tmp", created_at=0.0
    ))
    qapp.processEvents()
    assert sessions.pool("claude-acp").all() != []

    widget.shutdown()

    assert sessions.pool("claude-acp").all() == [], (
        "a dead session id survived for the next tab on this same agent to trip over"
    )


def test_shutdown_is_idempotent(qapp):
    """Houdini may call onDestroyInterface twice; the second call must not
    crash. (What the FIRST call does to a sibling tab sharing the same
    agent is a separate claim, covered on its own by
    test_shutdown_does_not_stop_a_sibling_tabs_shared_client below — this
    test used to bundle both into one docstring with only the first one
    actually checked.)"""
    widget = _make_panel(qapp)
    widget.shutdown()
    widget.shutdown()


def test_shutdown_does_not_stop_a_sibling_tabs_shared_client(qapp):
    """`shutdown`'s own docstring (`ui/panel.py`): "The agent connection
    only goes down once the last tab closes: while any tab is still alive,
    the conversation must keep going." Two tabs on the same agent id share
    one `AcpClient` (`shared_client`, `test_two_panels_on_the_same_agent_
    share_one_client_and_one_pool`) — closing one of them must not stop
    it out from under the other, and the survivor must still be able to
    do real work afterward, not just report `is_running()` truthfully.
    """
    current = settings_mod.load()
    current.default_agent = "claude-acp"
    current.autostart_agent = False
    settings_mod.save(current)

    first = _make_panel(qapp)
    second = _make_panel(qapp)
    client = panel_mod.shared_client("claude-acp")
    assert panel_mod.shared_client(first._agent_id) is client
    assert panel_mod.shared_client(second._agent_id) is client
    client._running = True
    stop_calls: list[bool] = []
    orig_stop = client.stop
    client.stop = lambda: (stop_calls.append(True), orig_stop())[1]

    first.shutdown()

    assert not stop_calls, "closing one of two tabs must not stop their shared client"
    assert client.is_running() is True
    assert panel_mod.shared_client(second._agent_id) is client, (
        "the survivor's own client must not have been dropped/replaced either"
    )

    # The survivor is not just alive by report — a real session_started for
    # THIS agent still reaches it, proving its own signal wiring (torn down
    # in `shutdown`, per-tab, not per-client) is intact.
    client.session_started.emit(
        "s1",
        sessions.SessionState(session_id="s1", title="New conversation", cwd="/tmp", created_at=0.0),
    )
    qapp.processEvents()
    assert second._pool.get("s1") is not None, (
        "the surviving tab must still be able to receive live updates from the shared client"
    )

    second.shutdown()
    assert stop_calls, "the LAST tab closing must still stop the now-unshared client"


def test_shutdown_releases_the_settings_and_composers_own_workers(qapp, monkeypatch):
    """`AgentsView`'s install threads and `Composer`'s voice-upload thread
    are parented several widgets deep, not directly to `AgentPanel` — the
    exact same crash docs/facts/houdini.md §14 describes applies to them
    just as much (a `QThread` still running when ITS parent is destroyed
    is `qFatal()`, regardless of how many widgets deep that parent is),
    so `shutdown()` has to reach them too, not only its own four workers.
    """
    widget = _make_panel(qapp)
    calls: list[str] = []
    monkeypatch.setattr(widget._settings_view, "shutdown", lambda: calls.append("settings"))
    monkeypatch.setattr(widget._composer, "shutdown", lambda: calls.append("composer"))

    widget.shutdown()

    assert "settings" in calls
    assert "composer" in calls


def test_permission_answer_reaches_client_and_resolves_in_transcript(qapp, monkeypatch):
    widget = _make_panel(qapp)
    widget.resize(900, 700)
    widget.show()
    qapp.processEvents()
    client = panel_mod.shared_client(widget._agent_id)

    state = _session()
    client.session_started.emit(state.session_id, state)
    qapp.processEvents()

    answered: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        client, "answer_permission", lambda key, option: answered.append((key, option))
    )

    # The call object has no .title — the panel has to supply its own text
    # rather than show an empty line with buttons for who-knows-what.
    tool_call = object()

    # PermissionOption by shape: option_id / name / kind.
    class _Option:
        def __init__(self, option_id, name, kind):
            self.option_id = option_id
            self.name = name
            self.kind = kind

    options = [_Option("allow_once", "Allow once", "allow_once")]
    client.permission_requested.emit("req-1", state.session_id, tool_call, options)
    qapp.processEvents()

    entries = widget._model(state.session_id).entries()
    permission_entries = [entry for entry in entries if entry.kind == "permission"]
    assert len(permission_entries) == 1
    assert widget._permission_popover is not None
    anchor = widget._composer.popover_anchor_rect(widget)
    popover = widget._permission_popover
    assert abs(popover.geometry().center().x() - anchor.center().x()) <= 1
    assert popover.geometry().bottom() < anchor.top()
    assert "req-1" not in widget._transcript._rows

    widget._on_permission_answered("req-1", "allow_once")

    assert answered == [("req-1", "allow_once")]
    assert permission_entries[0].permission.answered == "allow_once"
    assert widget._permission_popover is None

    widget.shutdown()


def test_blocking_announcement_blocks_input_but_not_the_transcript(qapp):
    """A direct prohibition from design.md: the feed stays readable, the panel
    closable, Houdini working — only the input field is blocked."""
    from houdini_agent_panel.announcements import Announcement, Button

    widget = _make_panel(qapp)
    ann = Announcement(
        id="a1",
        severity="blocking",
        title="Important",
        body="Body",
        buttons=(Button(label="Got it", url=""),),
    )

    class _Result:
        announcements = [ann]
        updates: list = []

    widget._on_refresh_done(_Result())
    qapp.processEvents()

    assert widget._composer.is_input_blocked()
    assert widget._transcript.isEnabled()

    widget._on_blocking_action("a1", "")
    qapp.processEvents()

    assert not widget._composer.is_input_blocked()
    assert "a1" in widget._settings.seen_announcements

    widget.shutdown()


def test_chunk_for_background_session_does_not_touch_the_visible_transcript(qapp):
    """Streaming into an invisible session must not touch the widgets of the
    one the human is reading."""
    widget = _make_panel(qapp)
    client = panel_mod.shared_client(widget._agent_id)

    visible = _session("visible")
    background = _session("background")
    client.session_started.emit(visible.session_id, visible)
    client.session_started.emit(background.session_id, background)
    qapp.processEvents()
    widget._set_current_session(visible.session_id)

    refreshed: list = []
    widget._transcript.refresh = lambda entry_id=None: refreshed.append(entry_id)

    client.message_chunk.emit(background.session_id, "m1", "background")
    qapp.processEvents()

    assert refreshed == []
    assert widget._model(background.session_id).entries()

    widget.shutdown()


def test_registry_reaches_the_agents_section(qapp):
    """The "Agents" block in settings never hits the network itself.

    `AgentsView.refresh_from_registry` is synchronous, and calling it from the
    main thread would freeze Houdini for exactly the length of a network
    timeout. Entries have to arrive already fetched by the background pass.
    """
    from houdini_agent_panel.registry import AgentEntry, BinaryDistribution

    widget = _make_panel(qapp)
    entry = AgentEntry(
        id="opencode",
        name="OpenCode",
        version="1.18.11",
        binaries={"darwin-aarch64": BinaryDistribution(
            archive="https://example.test/a.zip", cmd="./opencode", args=[], sha256="0" * 64
        )},
    )

    shown = []
    widget._settings_view.set_agents = lambda entries, updates=None: shown.append((entries, updates))

    class _Result:
        announcements: list = []
        updates: list = []

    widget._on_refresh_done(_Result(), [entry])

    assert shown and shown[0][0] == [entry]
    widget.shutdown()


def test_installed_agent_reaches_the_header_chip_menu(qapp):
    """"Installed it, and it shows up in the chip menu with no panel restart"."""
    from houdini_agent_panel import settings as settings_mod

    widget = _make_panel(qapp)
    widget._boot()

    current = settings_mod.load()
    current.installed_agents["claude-acp"] = settings_mod.InstalledAgent(
        agent_id="claude-acp", version="1.0", kind="npx", installed_at="now"
    )
    current.installed_agents["codex-acp"] = settings_mod.InstalledAgent(
        agent_id="codex-acp", version="1.0", kind="binary", installed_at="now"
    )
    settings_mod.save(current)

    widget._settings_view._agents_view.installed_changed.emit()

    ids = [agent_id for agent_id, _label in widget._header._agent_items]
    assert ids == ["claude-acp", "codex-acp"]
    widget.shutdown()


def test_agent_installed_only_via_manifest_still_reaches_the_chip_menu(qapp):
    """A real bug, reported live: Claude worked fine, then vanished from this
    exact menu.

    `settings.installed_agents` used to be the chip menu's only source. An
    npx agent can run for hours on nothing but its own on-demand fetch,
    never touching the explicit Install/Update button that writes that
    settings record — only the manifest
    (`runtime.installed_version`/`_LaunchPrepWorker`, see panel.py) knows it
    is actually there. Without reading that too, the very next registry
    refresh dropped it from the menu with no way back to the agent the
    artist had just been talking to.
    """
    from houdini_agent_panel import runtime
    from houdini_agent_panel.registry import AgentEntry

    widget = _make_panel(qapp)
    widget._boot()

    entry = AgentEntry(id="claude-acp", name="Claude Agent", version="1.0.0")
    runtime._write_manifest(entry, kind="npx")
    assert "claude-acp" not in settings_mod.load().installed_agents  # never went through Install/Update

    widget._on_refresh_done(SimpleNamespace(announcements=[], updates=[]), [entry])

    ids = [agent_id for agent_id, _label in widget._header._agent_items]
    assert "claude-acp" in ids
    widget.shutdown()


def test_choosing_agent_from_chip_menu_switches_agent(qapp, monkeypatch):
    widget = _make_panel(qapp)
    widget._boot()

    started: list[str] = []
    monkeypatch.setattr(widget, "_start_agent", lambda agent_id: started.append(agent_id))

    widget._header.agent_selected.emit("codex-acp")

    from houdini_agent_panel import settings as settings_mod

    assert settings_mod.load().default_agent == "codex-acp"
    assert started == ["codex-acp"]
    widget.shutdown()


def test_restart_agent_stops_and_relaunches_the_same_agent(qapp, monkeypatch):
    """`_restart_agent` — wired to the Network section's "Restart agent"
    banner (issue #26) — stops the running client and brings the SAME
    agent id back, unlike `_on_agent_chosen` which changes identity."""
    current = settings_mod.load()
    current.default_agent = "claude-acp"
    current.autostart_agent = False
    settings_mod.save(current)

    widget = _make_panel(qapp)
    assert widget._agent_id == "claude-acp"

    client = panel_mod.shared_client("claude-acp")
    monkeypatch.setattr(client, "is_running", lambda: True)
    stopped = []
    monkeypatch.setattr(client, "stop", lambda: stopped.append(True))
    started: list[str] = []
    monkeypatch.setattr(widget, "_start_agent", lambda agent_id: started.append(agent_id))

    widget._restart_agent()

    assert stopped == [True]
    assert started == ["claude-acp"]
    assert widget._agent_id == "claude-acp"  # identity unchanged, unlike a switch
    assert settings_mod.load().default_agent == "claude-acp"  # never touched
    widget.shutdown()


def test_restart_agent_does_nothing_without_an_agent(qapp, monkeypatch):
    widget = _make_panel(qapp)
    started: list[str] = []
    monkeypatch.setattr(widget, "_start_agent", lambda agent_id: started.append(agent_id))

    widget._restart_agent()

    assert started == []
    widget.shutdown()


def test_restart_agent_keeps_the_conversation(qapp, monkeypatch):
    """The recipe borrowed from `_on_agent_chosen`
    (`AgentPanel._switch_agent_process`) persists the transcript to disk
    before tearing anything down, and refills the pool from disk once the
    old one is cleared — so a restart drops the live session id (it
    belonged to the process that's going away) but not the words."""
    current = settings_mod.load()
    current.default_agent = "claude-acp"
    current.autostart_agent = False
    settings_mod.save(current)

    widget = _make_panel(qapp)
    client = panel_mod.shared_client("claude-acp")
    client.session_started.emit("s1", _session("s1"))
    qapp.processEvents()
    widget._pool.get("s1").title = "About the shot"
    widget._model("s1").append_error("hello")

    monkeypatch.setattr(client, "is_running", lambda: False)
    monkeypatch.setattr(widget, "_start_agent", lambda agent_id: None)

    widget._restart_agent()

    assert widget._pool.get("s1") is None
    restored = [s for s in widget._pool.all() if s.session_id.startswith("restored:")]
    assert restored and restored[0].title == "About the shot"
    widget.shutdown()


def test_restart_agent_requested_from_settings_reaches_the_panel(qapp, monkeypatch):
    """The Network section's button is wired all the way through, not just
    present — `SettingsView.restart_agent_requested` must actually call
    `AgentPanel._restart_agent` (`_make_settings_view`)."""
    widget = _make_panel(qapp)
    calls = []
    monkeypatch.setattr(widget, "_restart_agent", lambda: calls.append(True))

    widget._settings_view.restart_agent_requested.emit()

    assert calls == [True]
    widget.shutdown()


def test_manage_agents_clicked_opens_settings_focused_on_agents(qapp):
    widget = _make_panel(qapp)
    widget._boot()
    widget._show_page(widget.PAGE_TRANSCRIPT)
    widget._settings_view._scroll.verticalScrollBar().setValue(50)

    widget._header.manage_agents_clicked.emit()

    assert widget._pages.currentIndex() == widget.PAGE_SETTINGS
    assert widget._settings_view._scroll.verticalScrollBar().value() == 0
    widget.shutdown()


def test_telemetry_consent_asked_once_and_remembered(qapp):
    """A refusal is remembered too.

    Otherwise someone who said "no thanks" would get the same question every
    time they opened the panel — that isn't a question any more, it's nagging.
    """
    from houdini_agent_panel import settings as settings_mod

    widget = _make_panel(qapp)
    widget._boot()
    assert widget._consent.isVisibleTo(widget)

    widget._on_telemetry_answer(False)

    saved = settings_mod.load()
    assert saved.telemetry_consent_asked is True
    assert saved.telemetry is False

    widget.shutdown()

    second = _make_panel(qapp)
    second._boot()
    assert not second._consent.isVisibleTo(second)
    second.shutdown()


def test_telemetry_consent_yes_turns_it_on(qapp):
    from houdini_agent_panel import settings as settings_mod

    widget = _make_panel(qapp)
    widget._on_telemetry_answer(True)

    saved = settings_mod.load()
    assert saved.telemetry is True
    assert saved.telemetry_consent_asked is True

    widget.shutdown()


def test_consent_strip_does_not_block_input(qapp):
    """The question about stats has no right to get in the way of working."""
    widget = _make_panel(qapp)
    widget._boot()

    assert not widget._composer.is_input_blocked()
    assert widget._transcript.isEnabled()

    widget.shutdown()


def test_turn_drives_activity_burst_tool_reset_and_completion(qapp, monkeypatch):
    widget = _make_panel(qapp)
    client = panel_mod.shared_client(widget._agent_id)
    state = _session()
    client.session_started.emit(state.session_id, state)
    monkeypatch.setattr(client, "prompt", lambda _session_id, _blocks: None)

    widget._on_submitted([{"type": "text", "text": "build some test geometry"}])
    activity_rows = [
        row for row in widget._transcript._rows.values() if hasattr(row, "indicator")
    ]
    assert len(activity_rows) == 1
    indicator = activity_rows[0].indicator
    first_verb = indicator._verb
    assert indicator.is_active()
    assert widget._composer._buddy._action_elapsed == 0

    call = SimpleNamespace(
        tool_call_id="tc1",
        title="Create geometry",
        kind="edit",
        status="pending",
        content=None,
        locations=None,
    )
    client.tool_call.emit(state.session_id, call)
    assert indicator.is_active()
    assert indicator._verb != first_verb

    client.turn_finished.emit(state.session_id, "end_turn")
    assert not indicator.is_active()
    assert indicator._status._text.startswith("Worked for ")

    widget.shutdown()


def test_a_tool_finishing_in_a_background_conversation_is_correct_once_shown(qapp):
    """Investigating a report where a collapsed group's header read
    "failed" while every one of its (31, in his case) tools read "done" —
    the summary contradicting its own contents. `_touch` only redraws the
    conversation currently on screen (`_is_current`); a status change in a
    conversation the artist switched away from via the drawer updates the
    MODEL right away but never touches the stale widget until the
    conversation is shown again, at which point `_show_session` rebuilds
    it from scratch. This is the one gap traced in the code that could
    leave a widget showing something older than the model actually has —
    confirms it heals the instant the conversation is shown again, so any
    reported mismatch actually seen ON SCREEN needs a different
    explanation (see the accompanying report for what was and wasn't
    established)."""
    widget = _make_panel(qapp)
    client = panel_mod.shared_client(widget._agent_id)
    client.session_started.emit("a", _session("a"))
    qapp.processEvents()
    client.session_started.emit("b", _session("b"))
    qapp.processEvents()
    widget._set_current_session("a")  # "b" is now in the background

    call = SimpleNamespace(
        tool_call_id="tc1", title="Run something", kind="execute",
        status="in_progress", content=None, locations=None,
    )
    client.tool_call.emit("b", call)
    update_failed = SimpleNamespace(
        tool_call_id="tc1", title=None, kind=None, status="failed", content=None, locations=None
    )
    client.tool_call_update.emit("b", update_failed)
    update_completed = SimpleNamespace(
        tool_call_id="tc1", title=None, kind=None, status="completed", content=None, locations=None
    )
    client.tool_call_update.emit("b", update_completed)
    qapp.processEvents()

    # The model already has the correct, final status — updating it never
    # depended on anything being on screen.
    entry = widget._model("b").entries()[-1]
    assert entry.tool.status == "completed"

    # Showing "b" rebuilds the transcript from the model as it stands now.
    widget._set_current_session("b")
    qapp.processEvents()
    row = widget._transcript._rows[entry.id]
    assert "done" in row._toggle.text()
    assert "failed" not in row._toggle.text()

    widget.shutdown()


def test_auth_buttons_follow_the_client_across_a_restart(qapp, monkeypatch):
    """The sign-in buttons must not talk to a corpse.

    Each agent id has its own client (`shared_client(agent_id)` — see
    `AgentPanel._agent_id`), so switching THIS tab's agent means talking to
    a genuinely different object, not a rebuilt worker inside the same one.
    Subscribing directly with
    `view.method_chosen.connect(shared_client(...).authenticate)` would
    permanently capture whichever instance existed when the widget was
    built.
    """
    widget = _make_panel(qapp)
    old = panel_mod.shared_client(widget._agent_id)

    monkeypatch.setattr(widget, "_start_agent", lambda agent_id: None)
    widget._on_agent_chosen("some-other-agent")
    assert widget._agent_id == "some-other-agent"
    fresh = panel_mod.shared_client(widget._agent_id)
    assert fresh is not old

    seen: list = []
    monkeypatch.setattr(fresh, "authenticate", lambda mid: seen.append(("auth", mid)))
    monkeypatch.setattr(fresh, "logout", lambda: seen.append(("logout",)))
    # `_on_logout_requested` now checks `is_running()` first (see
    # `test_sign_out_on_a_not_running_agent_reports_failure_not_silence`)
    # — stubbed True here since that guard isn't what this test is about.
    monkeypatch.setattr(fresh, "is_running", lambda: True)

    widget._auth_view.method_chosen.emit("oauth")
    widget._auth_view.logout_requested.emit()
    qapp.processEvents()

    assert seen == [("auth", "oauth"), ("logout",)]
    widget.shutdown()


def test_sign_out_on_a_not_running_agent_reports_failure_not_silence(qapp, monkeypatch):
    """`AcpClient._submit` (client.py) silently drops any call when there
    is no live worker — no `auth_required`, no `error`, nothing at all.
    Left unguarded, `_on_logout_requested` would set `_pending_logout_
    agent` and call `logout()` into that void: the row's "Sign out" click
    would just sit there forever with zero feedback, and no way to tell
    the click did nothing — the exact trap the owner's one-button model
    (`ui/agents.py::_AgentRow`) depends on NOT existing, one step further
    along. `_on_logout_requested` checks `is_running()` first and reports
    a failure the same way a real logout error would, instead of calling
    out into a client with nothing listening.
    """
    widget = _make_panel(qapp)
    client = panel_mod.shared_client(widget._agent_id)
    assert not client.is_running()

    called: list = []
    monkeypatch.setattr(client, "logout", lambda: called.append(True))
    notes: list[str] = []
    widget._note = lambda text, **_: notes.append(text)

    widget._on_logout_requested()
    qapp.processEvents()

    assert called == []
    assert widget._pending_logout_agent is None
    assert any("Sign out failed" in n for n in notes)
    widget.shutdown()


def test_open_drawer_never_moves_the_conversation(qapp):
    """An open drawer must not move the feed or the composer sideways, ever.

    This used to reserve the drawer's width as `_body_layout`'s left
    margin, which pushed the feed/composer aside — smoothly or not, that
    is still a horizontal jump the owner explicitly does not want.
    `_body_layout`'s margin is now a genuine constant: the drawer lives in
    the already-empty gutter beside the reading column instead
    (`ConversationDrawer.set_available_width`,
    `TranscriptView.current_gutter`), so there is nothing for `_body` to
    react to in the first place.
    """
    widget = _make_panel(qapp)
    widget.resize(1000, 700)
    widget.show()
    qapp.processEvents()

    body_x_before = widget._body.mapTo(widget, widget._body.rect().topLeft()).x()

    widget._conversations.open_drawer()
    qapp.processEvents()

    drawer = widget._conversations
    assert widget._body_layout.contentsMargins().left() == 0
    assert widget._body.mapTo(widget, widget._body.rect().topLeft()).x() == body_x_before
    # The header keeps its full width, so the toggle that closes the drawer
    # stays where it was — and the drawer starts below the header, so it
    # can never end up on top of that toggle either.
    assert widget._header.x() == 0
    assert drawer.y() >= widget._header.height()

    widget._conversations.close_drawer()
    qapp.processEvents()
    assert widget._body_layout.contentsMargins().left() == 0
    assert widget._body.mapTo(widget, widget._body.rect().topLeft()).x() == body_x_before

    widget.shutdown()


def test_bug_report_link_does_not_shift_when_the_drawer_opens(qapp):
    """The same "the drawer never moves anything" guarantee
    (`test_open_drawer_never_moves_the_conversation`), for the newest
    thing living in that footer band: the "Report a bug…" link sits below
    the composer's own input box, and the drawer draws inside `Transcript
    View`'s existing gutter without ever touching `_body`/`Composer`'s
    own geometry — this link was never wired to hear about the drawer at
    all, so nothing about it CAN shift; checked here at the panel level,
    not just the composer-only claim `test_ui_composer.py` already covers,
    since this is what the owner actually asked to be sure of.
    """
    widget = _make_panel(qapp)
    widget.resize(1000, 700)
    widget.show()
    qapp.processEvents()
    widget._show_page(widget.PAGE_TRANSCRIPT)
    qapp.processEvents()

    link = widget._composer._bug_report_link
    link_x_before = link.mapTo(widget, QtCore.QPoint(0, 0)).x()

    widget._conversations.open_drawer()
    qapp.processEvents()
    assert link.mapTo(widget, QtCore.QPoint(0, 0)).x() == link_x_before

    widget._conversations.close_drawer()
    qapp.processEvents()
    assert link.mapTo(widget, QtCore.QPoint(0, 0)).x() == link_x_before

    widget.shutdown()


def test_narrow_panel_also_never_moves_the_feed(qapp):
    widget = _make_panel(qapp)
    widget.resize(320, 700)
    widget.show()
    qapp.processEvents()

    widget._conversations.open_drawer()
    qapp.processEvents()

    assert widget._body_layout.contentsMargins().left() == 0
    widget.shutdown()


def test_drawer_width_tracks_the_transcripts_gutter_across_panel_widths(qapp):
    """The table the owner asked for, as an assertion: the composer's left
    edge is measured with the drawer closed and open at seven panel widths
    (1600 down to 320). It must be IDENTICAL at every single width — not
    just where the drawer comfortably fits — because `_body` never moves
    regardless of whether the drawer fits its ideal width, has to shrink,
    or hits its floor and ends up overlapping a little. What's expected to
    change across these widths is the drawer's OWN width: full 286px while
    there's room, shrinking with the gutter, floored at
    `_drawer_floor_width()` below it.
    """
    from houdini_agent_panel.ui.conversations import _DRAWER_IDEAL_WIDTH, _drawer_floor_width

    widget = _make_panel(qapp)
    widget._show_page(widget.PAGE_TRANSCRIPT)  # the gutter only tracks a VISIBLE transcript
    floor = _drawer_floor_width()
    rows = []

    for width in (1600, 1300, 1100, 900, 700, 500, 320):
        widget.resize(width, 700)
        widget.show()
        qapp.processEvents()

        composer_x_closed = widget._composer.mapTo(widget, QtCore.QPoint(0, 0)).x()

        widget._conversations.open_drawer()
        qapp.processEvents()
        composer_x_open = widget._composer.mapTo(widget, QtCore.QPoint(0, 0)).x()
        drawer_width = widget._conversations.width()
        gutter = widget._transcript.current_gutter()
        widget._conversations.close_drawer()
        qapp.processEvents()

        rows.append((width, gutter, drawer_width, composer_x_closed, composer_x_open))

        # The hard requirement: identical composer position, at every width.
        assert composer_x_open == composer_x_closed, (
            f"composer moved at panel width {width}: "
            f"{composer_x_closed} (closed) != {composer_x_open} (open)"
        )
        # The drawer's own width degrades exactly as designed: full ideal
        # width when the gutter has room for it, shrunk to the gutter while
        # the gutter is still above the floor, held at the floor below it.
        expected_drawer_width = min(_DRAWER_IDEAL_WIDTH, max(floor, gutter))
        assert drawer_width == expected_drawer_width, (
            f"at panel width {width}: gutter={gutter}, floor={floor}, "
            f"expected drawer width {expected_drawer_width}, got {drawer_width}"
        )

    widget.shutdown()

    header = f"{'panel':>6} {'gutter':>7} {'drawer':>7} {'closed x':>9} {'open x':>7} {'moved?':>7}"
    print("\n" + header)
    for width, gutter, drawer_width, x_closed, x_open in rows:
        moved = "yes" if x_closed != x_open else "no"
        print(f"{width:>6} {gutter:>7} {drawer_width:>7} {x_closed:>9} {x_open:>7} {moved:>7}")


def test_panel_can_be_docked_into_a_narrow_houdini_pane(qapp):
    """The centered 736px rails must not become the panel's minimum width.

    `setFixedWidth` on a child hands that width up as the parent's minimum,
    so the header, composer and settings rails between them pinned the whole
    panel at 736px — wider than a typical docked Houdini pane.
    """
    widget = _make_panel(qapp)
    widget.show()
    qapp.processEvents()

    assert widget.minimumSizeHint().width() <= 320

    widget.resize(360, 700)
    qapp.processEvents()
    assert widget.width() == 360

    widget.shutdown()


def test_config_options_from_the_agent_become_composer_chips(qapp):
    """The model picker. It exists in ACP only as session config options, and
    nothing was listening to them — so the chip never appeared at all."""
    from houdini_agent_panel.client import ConfigChoice, ConfigOption

    widget = _make_panel(qapp)
    client = panel_mod.shared_client(widget._agent_id)
    state = _session()
    client.session_started.emit(state.session_id, state)
    qapp.processEvents()

    option = ConfigOption(
        id="model",
        name="Model",
        current_value="opus",
        choices=(
            ConfigChoice(value="sonnet", name="Claude Sonnet 4.5"),
            ConfigChoice(value="opus", name="Claude Opus 4.1"),
        ),
    )
    client.config_options_changed.emit(state.session_id, [option])
    qapp.processEvents()

    chips = widget._composer._config_chips
    assert len(chips) == 1
    assert chips[0].currentData() == "opus"
    assert widget._pool.get(state.session_id).config_options == [option]

    chosen: list[tuple[str, str, str]] = []
    panel_mod.shared_client(widget._agent_id).set_config_option = lambda sid, cid, value: chosen.append(
        (sid, cid, value)
    )
    chips[0]._choose(0)

    assert chosen == [(state.session_id, "model", "sonnet")]
    widget.shutdown()


def test_agent_offering_no_config_options_gets_no_chips(qapp):
    widget = _make_panel(qapp)
    client = panel_mod.shared_client(widget._agent_id)
    state = _session()
    client.session_started.emit(state.session_id, state)
    qapp.processEvents()

    assert widget._composer._config_chips == []
    assert not widget._composer._config_bar.isVisibleTo(widget._composer)
    widget.shutdown()


def test_mode_selection_survives_switching_away_and_back(qapp, monkeypatch):
    """Picking "Plan" must not revert to the default mode after visiting
    another conversation — the agent's own `current_mode_update` echo is not
    guaranteed to arrive before the artist switches away."""
    from houdini_agent_panel.sessions import SessionMode

    widget = _make_panel(qapp)
    client = panel_mod.shared_client(widget._agent_id)
    sent = []
    monkeypatch.setattr(client, "set_mode", lambda sid, mode_id: sent.append((sid, mode_id)))

    first = _session("s1")
    client.session_started.emit(first.session_id, first)
    qapp.processEvents()
    modes = [SessionMode(id="manual", name="Manual"), SessionMode(id="plan", name="Plan")]
    client.modes_changed.emit(
        first.session_id, SimpleNamespace(current_mode_id="manual", available_modes=modes)
    )
    qapp.processEvents()

    widget._on_mode_selected("plan")
    assert widget._pool.get("s1").current_mode_id == "plan"
    assert sent == [("s1", "plan")]

    second = _session("s2")
    client.session_started.emit(second.session_id, second)
    qapp.processEvents()
    assert widget._current_session().session_id == "s2"

    widget._set_current_session("s1")
    qapp.processEvents()

    assert widget._pool.get("s1").current_mode_id == "plan"
    assert widget._composer.mode_chip._combo.currentData() == "plan"
    widget.shutdown()


def test_config_option_selection_survives_switching_away_and_back(qapp, monkeypatch):
    from houdini_agent_panel.client import ConfigChoice, ConfigOption

    widget = _make_panel(qapp)
    client = panel_mod.shared_client(widget._agent_id)
    monkeypatch.setattr(client, "set_config_option", lambda _sid, _cid, _value: None)

    first = _session("s1")
    client.session_started.emit(first.session_id, first)
    qapp.processEvents()
    option = ConfigOption(
        id="model",
        name="Model",
        current_value="sonnet",
        choices=(
            ConfigChoice(value="sonnet", name="Claude Sonnet"),
            ConfigChoice(value="opus", name="Claude Opus"),
        ),
    )
    client.config_options_changed.emit(first.session_id, [option])
    qapp.processEvents()

    widget._on_config_option_selected("model", "opus")
    assert widget._pool.get("s1").config_options[0].current_value == "opus"

    second = _session("s2")
    client.session_started.emit(second.session_id, second)
    qapp.processEvents()

    widget._set_current_session("s1")
    qapp.processEvents()

    assert widget._pool.get("s1").config_options[0].current_value == "opus"
    assert widget._composer._config_chips[0].currentData() == "opus"
    widget.shutdown()


def test_choosing_a_config_option_remembers_it_per_agent(qapp, monkeypatch):
    """The owner asked specifically because they expected the pick to
    survive a restart — `settings.config_options_by_agent` is where it's
    kept (`_reapply_remembered_config` is what puts it back)."""
    from houdini_agent_panel.client import ConfigChoice, ConfigOption

    widget = _make_panel(qapp)
    client = panel_mod.shared_client(widget._agent_id)
    monkeypatch.setattr(client, "set_config_option", lambda _sid, _cid, _value: None)

    state = _session("s1")
    client.session_started.emit(state.session_id, state)
    qapp.processEvents()
    option = ConfigOption(
        id="model",
        name="Model",
        current_value="sonnet",
        choices=(ConfigChoice(value="sonnet", name="Sonnet"), ConfigChoice(value="opus", name="Opus")),
    )
    client.config_options_changed.emit(state.session_id, [option])
    qapp.processEvents()

    widget._on_config_option_selected("model", "opus")

    remembered = settings_mod.load().config_options_by_agent
    assert remembered[widget._agent_id]["model"] == "opus"
    widget.shutdown()


def test_remembered_config_choice_is_reapplied_on_a_fresh_session(qapp):
    """A brand-new `session/new` starts the agent back on ITS OWN default
    (ACP has no persistence of its own) — the panel must put the artist's
    remembered pick back, not silently accept the agent's default."""
    from houdini_agent_panel.client import ConfigChoice, ConfigOption

    widget = _make_panel(qapp)
    current = settings_mod.load()
    current.config_options_by_agent[widget._agent_id] = {"model": "opus"}
    settings_mod.save(current)

    client = panel_mod.shared_client(widget._agent_id)
    applied: list[tuple[str, str, str]] = []
    client.set_config_option = lambda sid, cid, value: applied.append((sid, cid, value))

    state = _session("s1")
    client.session_started.emit(state.session_id, state)
    qapp.processEvents()
    option = ConfigOption(
        id="model",
        name="Model",
        current_value="sonnet",  # the agent's own default — NOT what was remembered
        choices=(ConfigChoice(value="sonnet", name="Sonnet"), ConfigChoice(value="opus", name="Opus")),
    )
    client.config_options_changed.emit(state.session_id, [option])
    qapp.processEvents()

    assert applied == [(state.session_id, "model", "opus")]
    widget.shutdown()


def test_reapply_does_not_repeat_on_a_later_config_option_update(qapp):
    """Only the FIRST `configOptions` a session ever reports gets
    reconciled against the remembered pick — a later `config_option_update`
    is a live change (the artist's own next click, or the agent's), and
    forcing the remembered value back onto it would fight that click."""
    from houdini_agent_panel.client import ConfigChoice, ConfigOption

    widget = _make_panel(qapp)
    current = settings_mod.load()
    current.config_options_by_agent[widget._agent_id] = {"model": "opus"}
    settings_mod.save(current)

    client = panel_mod.shared_client(widget._agent_id)
    applied: list[tuple[str, str, str]] = []
    client.set_config_option = lambda sid, cid, value: applied.append((sid, cid, value))

    state = _session("s1")
    client.session_started.emit(state.session_id, state)
    qapp.processEvents()
    choices = (ConfigChoice(value="sonnet", name="Sonnet"), ConfigChoice(value="opus", name="Opus"))
    client.config_options_changed.emit(
        state.session_id, [ConfigOption(id="model", name="Model", current_value="sonnet", choices=choices)]
    )
    qapp.processEvents()
    assert applied == [(state.session_id, "model", "opus")]

    # The agent (or a live artist pick) moves it to "sonnet" again — a
    # SECOND configOptions report for the SAME session.
    client.config_options_changed.emit(
        state.session_id, [ConfigOption(id="model", name="Model", current_value="sonnet", choices=choices)]
    )
    qapp.processEvents()

    assert applied == [(state.session_id, "model", "opus")]  # not called again
    widget.shutdown()


def test_stale_remembered_config_choice_is_silently_ignored(qapp):
    """A remembered model the agent no longer offers (retired, renamed after
    an update) must not be forced onto a choice list that doesn't contain
    it — the agent's own default is accepted, quietly."""
    from houdini_agent_panel.client import ConfigChoice, ConfigOption

    widget = _make_panel(qapp)
    current = settings_mod.load()
    current.config_options_by_agent[widget._agent_id] = {"model": "a-model-that-no-longer-exists"}
    settings_mod.save(current)

    client = panel_mod.shared_client(widget._agent_id)
    applied: list[tuple[str, str, str]] = []
    client.set_config_option = lambda sid, cid, value: applied.append((sid, cid, value))

    state = _session("s1")
    client.session_started.emit(state.session_id, state)
    qapp.processEvents()
    option = ConfigOption(
        id="model",
        name="Model",
        current_value="sonnet",
        choices=(ConfigChoice(value="sonnet", name="Sonnet"), ConfigChoice(value="opus", name="Opus")),
    )
    client.config_options_changed.emit(state.session_id, [option])
    qapp.processEvents()

    assert applied == []
    assert widget._composer._config_chips[0].currentData() == "sonnet"
    widget.shutdown()


def test_background_session_gets_unread_dot_current_session_does_not(qapp):
    widget = _make_panel(qapp)
    client = panel_mod.shared_client(widget._agent_id)

    first = _session("s1")
    client.session_started.emit(first.session_id, first)
    qapp.processEvents()
    second = _session("s2")
    client.session_started.emit(second.session_id, second)
    qapp.processEvents()
    assert widget._current_session().session_id == "s2"

    client.message_chunk.emit("s1", "m1", "an answer arrived while s1 was in the background")
    qapp.processEvents()

    assert widget._pool.get("s1").unread is True
    assert widget._pool.get("s2").unread is False

    widget._set_current_session("s1")
    qapp.processEvents()

    assert widget._pool.get("s1").unread is False
    widget.shutdown()


def test_text_typed_before_any_session_is_not_thrown_away(qapp, monkeypatch):
    """The composer clears itself on submit, so the panel has to keep what it
    was handed — otherwise the first message after opening the panel vanished
    without a trace."""
    widget = _make_panel(qapp)
    client = panel_mod.shared_client(widget._agent_id)

    started: list[bool] = []
    monkeypatch.setattr(widget, "_start_new_session", lambda: started.append(True))
    prompted: list[tuple[str, list]] = []
    monkeypatch.setattr(client, "prompt", lambda sid, blocks: prompted.append((sid, blocks)))
    notes: list[str] = []
    widget._note = notes.append

    widget._on_submitted([{"type": "text", "text": "make it rain"}])
    assert started == [True]
    assert prompted == []
    # Reported for real: routine narration here ("No conversation open
    # yet — starting one and sending this.") read as a problem report,
    # especially alongside an unrelated false "not signed in" note from
    # the same investigation (`test_signin_reachable.py`). The message
    # showing as sent is already visible without saying so again.
    assert notes == [], "starting the first session must not narrate itself"

    state = _session()
    client.session_started.emit(state.session_id, state)
    qapp.processEvents()

    assert prompted == [(state.session_id, [{"type": "text", "text": "make it rain"}])]
    widget.shutdown()


def test_permission_popover_does_not_float_over_the_settings_screen(qapp):
    widget = _make_panel(qapp)
    widget.resize(900, 700)
    widget.show()
    qapp.processEvents()
    client = panel_mod.shared_client(widget._agent_id)

    state = _session()
    client.session_started.emit(state.session_id, state)
    qapp.processEvents()

    class _Option:
        def __init__(self, option_id, name, kind):
            self.option_id = option_id
            self.name = name
            self.kind = kind

    client.permission_requested.emit(
        "req-1", state.session_id, object(), [_Option("allow_once", "Allow once", "allow_once")]
    )
    qapp.processEvents()
    assert widget._permission_popover is not None

    widget._show_page(widget.PAGE_SETTINGS)
    assert widget._permission_popover is None

    # Still pending — it comes back with the conversation.
    widget._show_page(widget.PAGE_TRANSCRIPT)
    assert widget._permission_popover is not None

    widget.shutdown()


# --- the notice strip's "Update" button (issue #20) ------------------------


def _zip_bytes(cmd_name: str = "myagent", content: bytes = b"echo hi\n") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(cmd_name, content)
    return buf.getvalue()


def _wait_until(qapp, condition, *, timeout_ms: int = 5000) -> None:
    from PySide6 import QtTest

    elapsed = 0
    step = 20
    while not condition() and elapsed < timeout_ms:
        qapp.processEvents()
        QtTest.QTest.qWait(step)
        elapsed += step
    assert condition(), "condition did not become true in time"


def _agent_update_setup(
    widget, qapp, monkeypatch, fetcher, *, agent_id="claude-acp", bad_checksum=False
):
    """An agent already installed at 1.0.0, a registry entry at 2.0.0, and
    the update banner showing it — the state every test below starts from.

    `agent_id` defaults to one of the actual `FEATURED_AGENT_IDS`
    (`registry.featured`) — the settings screen only ever draws rows for
    those, same as production, so an arbitrary id here would silently have
    no row for `trigger_agent_update` to find.
    """
    from houdini_agent_panel.registry import AgentEntry, BinaryDistribution
    from houdini_agent_panel.updates import Update

    monkeypatch.setattr("houdini_agent_panel.runtime.platform_key", lambda: "fake-platform")
    monkeypatch.setattr("houdini_agent_panel.registry.platform_key", lambda: "fake-platform")

    payload = _zip_bytes()
    digest = "0" * 64 if bad_checksum else hashlib.sha256(payload).hexdigest()
    fetcher.add_bytes("https://x/a.zip", payload)

    entry = AgentEntry(
        id=agent_id,
        name="Agent A",
        version="2.0.0",
        binaries={
            "fake-platform": BinaryDistribution(archive="https://x/a.zip", cmd="./myagent", sha256=digest)
        },
    )
    current = settings_mod.load()
    current.installed_agents[agent_id] = settings_mod.InstalledAgent(
        agent_id=agent_id, version="1.0.0", kind="binary", installed_at="now"
    )
    settings_mod.save(current)
    from houdini_agent_panel import paths
    import json as _json

    (paths.agent_dir(agent_id) / "manifest.json").write_text(
        _json.dumps({"agent_id": agent_id, "version": "1.0.0", "kind": "binary"}), "utf-8"
    )

    # Inject the fake fetcher the same way `test_ui_agents.py` does — there is
    # no production path that hands the panel's own `AgentsView` a fetcher,
    # so this is the one place a test can reach in and avoid a real request.
    widget._settings_view._agents_view._fetch = fetcher

    update = Update(kind="agent", target=agent_id, label="Agent A 2.0.0", current="1.0.0", latest="2.0.0")

    class _Result:
        announcements: list = []
        updates = [update]

    widget._on_refresh_done(_Result(), [entry])
    qapp.processEvents()
    return update


def test_clicking_update_on_the_banner_installs_it_in_the_background(qapp, monkeypatch, fetcher):
    """The bug as reported: the banner shows, the button does nothing."""
    widget = _make_panel(qapp)
    update = _agent_update_setup(widget, qapp, monkeypatch, fetcher)

    assert widget._notice.isHidden() is False
    assert widget._active_update is update

    # Exactly what clicking the strip's own button does.
    widget._notice.action_clicked.emit(update.target, "")

    # The install runs on a QThread — the main thread must stay responsive
    # (`qapp.processEvents()` inside `_wait_until` is what a frozen main
    # thread would fail to pump) while it finishes.
    _wait_until(qapp, lambda: not widget._settings_view._agents_view._threads)

    current = settings_mod.load()
    assert current.installed_agents[update.target].version == "2.0.0"
    # The banner is gone — it isn't offering an update that already happened.
    assert widget._notice.isHidden() is True
    assert widget._active_update is None

    widget.shutdown()


def test_update_stops_the_running_agent_first_and_restarts_it_after(qapp, monkeypatch, fetcher):
    """Swapping files under a live agent process is not something this panel
    does silently — it stops it, says so, and brings it back up once the
    update is actually on disk."""
    widget = _make_panel(qapp)
    update = _agent_update_setup(widget, qapp, monkeypatch, fetcher)
    widget._settings.default_agent = update.target
    settings_mod.save(widget._settings)
    # THIS tab has to actually be on the agent being updated for the
    # stop-then-restart tracking to apply to it (`_before_agent_install`
    # only restarts an agent this tab itself is attached to).
    widget._rejoin_agent(update.target)

    client = panel_mod.shared_client(update.target)
    monkeypatch.setattr(client, "is_running", lambda: True)
    stopped = []
    monkeypatch.setattr(client, "stop", lambda: stopped.append(True))
    started_agents = []
    monkeypatch.setattr(widget, "_start_agent", lambda agent_id: started_agents.append(agent_id))

    widget._notice.action_clicked.emit(update.target, "")

    assert stopped == [True]  # stopped BEFORE the install even started, not silently
    assert widget._restart_after_update == update.target

    _wait_until(qapp, lambda: not widget._settings_view._agents_view._threads)

    assert started_agents == [update.target]
    assert widget._restart_after_update is None

    widget.shutdown()


def test_removing_the_running_default_agent_stops_it_first_and_resets_the_header(qapp, monkeypatch):
    """The same hazard `_before_agent_install` already guards against for
    Update — swapping/deleting a live agent's files out from under it
    without saying so — applied to Remove. Unlike Update, this must never
    set `_restart_after_update`: the artist asked for it to be gone, not
    brought back. Found while diagnosing "remove and reinstall does
    nothing" — `_uninstall` never stopped the running agent at all, and
    left `default_agent`/the header naming an agent that was both stopped
    and no longer installed.
    """
    from houdini_agent_panel import runtime
    from houdini_agent_panel.registry import AgentEntry

    widget = _make_panel(qapp)
    agent_id = "claude-acp"  # a real FEATURED_AGENT_IDS member — registry.featured() needs one
    entry = AgentEntry(id=agent_id, name="Claude Agent", version="1.0.0")
    runtime._write_manifest(entry, kind="npx")
    current = settings_mod.load()
    current.installed_agents[agent_id] = settings_mod.InstalledAgent(
        agent_id=agent_id, version="1.0.0", kind="npx", installed_at="now"
    )
    current.default_agent = agent_id
    settings_mod.save(current)
    widget._settings.default_agent = agent_id
    widget._rejoin_agent(agent_id)  # this tab is the one showing the agent being removed
    widget._settings_view.set_agents([entry])
    widget._header.set_agent("Claude Agent", None)

    client = panel_mod.shared_client(agent_id)
    monkeypatch.setattr(client, "is_running", lambda: True)
    stopped = []
    monkeypatch.setattr(client, "stop", lambda: stopped.append(True))

    widget._settings_view._agents_view._uninstall(agent_id)

    assert stopped == [True], "the running agent was never stopped before its files were removed"
    assert widget._restart_after_update is None, "Remove must never schedule bringing it back"
    assert widget._header._agent_button.text() == ""
    assert runtime.installed_version(agent_id) is None
    assert not settings_mod.load().default_agent, (
        "default_agent must not keep pointing at an agent that no longer exists"
    )

    current_session = widget._current_session()
    session_id = current_session.session_id if current_session else "__idle__"
    feed_text = " ".join(e.text for e in widget._model(session_id).entries())
    assert "Stopping" in feed_text and "remove" in feed_text

    widget.shutdown()


def test_removing_an_agent_that_is_not_running_touches_nothing_live(qapp, monkeypatch):
    """Removing a background agent — installed, but never actually
    started — must not stop or disturb some OTHER, genuinely live
    connection at all. `other_agent`'s own client is a separate object
    from `live_agent`'s (one client per agent id, see `AgentPanel._agent_id`)
    and was never started, so a real (non-mocked) `is_running()` on it is
    already False — the assertion that matters is that `live_agent`'s own
    client, which THIS tab is actually attached to, is never touched by an
    unrelated Remove click.
    """
    from houdini_agent_panel import runtime
    from houdini_agent_panel.registry import AgentEntry

    widget = _make_panel(qapp)
    live_agent, other_agent = "claude-acp", "codex-acp"
    entry = AgentEntry(id=other_agent, name="Codex", version="1.0.0")
    runtime._write_manifest(entry, kind="npx")
    current = settings_mod.load()
    current.installed_agents[other_agent] = settings_mod.InstalledAgent(
        agent_id=other_agent, version="1.0.0", kind="npx", installed_at="now"
    )
    current.default_agent = live_agent
    settings_mod.save(current)
    widget._settings.default_agent = live_agent
    widget._rejoin_agent(live_agent)  # this tab is the one actually on live_agent
    widget._settings_view.set_agents([entry])

    live_client = panel_mod.shared_client(live_agent)
    live_stopped = []
    monkeypatch.setattr(live_client, "stop", lambda: live_stopped.append(True))

    widget._settings_view._agents_view._uninstall(other_agent)

    assert live_stopped == [], "removing a background agent must not stop the live one"
    assert settings_mod.load().default_agent == live_agent, "the real default_agent must be untouched"

    widget.shutdown()


def test_update_failure_is_reported_in_the_feed_not_silently(qapp, monkeypatch, fetcher):
    """A failed update must not look the same as a button that does nothing."""
    widget = _make_panel(qapp)
    # A wrong checksum in the registry entry — `download_and_verify` rejects
    # it, deterministically, off the main thread, the same way a bad
    # download or a tampered registry would in the field.
    update = _agent_update_setup(widget, qapp, monkeypatch, fetcher, bad_checksum=True)

    widget._notice.action_clicked.emit(update.target, "")

    _wait_until(qapp, lambda: not widget._settings_view._agents_view._threads)

    current_session = widget._current_session()
    session_id = current_session.session_id if current_session else "__idle__"
    feed_text = " ".join(e.text for e in widget._model(session_id).entries())
    assert "Could not update" in feed_text
    assert widget._restart_after_update is None

    widget.shutdown()


def test_panel_or_fx_update_runs_a_real_worker_not_just_instructions(qapp, monkeypatch):
    """Measured safe before this changed (`self_update.py`'s own docstring:
    both Houdini installs, macOS and Linux, a real hython with
    pydantic_core actually imported and used) — the panel now runs the
    update for real, in its own process, instead of only ever printing the
    command and hoping the artist runs it. `SelfUpdateWorker.work` is
    stubbed here (no real subprocess/network in a unit test); the signal
    wiring itself is the real thing, same as every other worker in this
    file — see the module docstring on preferring real signals to fakes.
    """
    from houdini_agent_panel.updates import Update
    from houdini_agent_panel.ui.self_update import SelfUpdateWorker

    started: list[str] = []
    monkeypatch.setattr(SelfUpdateWorker, "work", lambda self: started.append(self._target))

    widget = _make_panel(qapp)
    update = Update(
        kind="panel", target="houdini-agent-panel", label="houdini-agent-panel 1.2.0",
        current="1.1.0", latest="1.2.0",
    )

    class _Result:
        announcements: list = []
        updates = [update]

    widget._on_refresh_done(_Result(), [])
    qapp.processEvents()

    widget._notice.action_clicked.emit(update.target, "")

    assert widget._panel_update_worker is not None, "the notice's Update button must start a real worker"
    # The offer is replaced by the running update, not left stacked
    # alongside it — pressing Update again while one is already in flight
    # is a no-op (`_start_update`'s own guard), not a second update.
    assert widget._active_update is None
    assert "Updating" in widget._notice._label.text()

    widget._panel_update_worker.wait(3000)
    qapp.processEvents()
    assert started == ["houdini-agent-panel"]

    widget.shutdown()


def test_panel_update_passes_the_known_latest_version_not_a_bare_package_name(qapp, monkeypatch):
    """The version-pin bug: an owner on 0.7.1 pressed Update with 0.7.2
    available, and it reinstalled 0.7.1 over itself. `update.latest` is
    already on the `Update` record the notice is showing — `_start_update`
    must hand it to `SelfUpdateWorker` explicitly rather than leaving the
    version for uvx (or worse, a `PYTHONPATH`-shadowed import) to guess."""
    from houdini_agent_panel.updates import Update
    from houdini_agent_panel.ui.self_update import SelfUpdateWorker

    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(
        SelfUpdateWorker, "work", lambda self: seen.append((self._target, self._version))
    )

    widget = _make_panel(qapp)
    update = Update(
        kind="panel", target="houdini-agent-panel", label="houdini-agent-panel 1.2.0",
        current="1.1.0", latest="1.2.0",
    )
    widget._start_update(update)
    widget._panel_update_worker.wait(3000)
    qapp.processEvents()

    assert seen == [("houdini-agent-panel", "1.2.0")]

    widget.shutdown()


def test_panel_update_progress_updates_the_visible_notice(qapp):
    """The artist watching a strip that keeps changing is the difference
    between "it's working" and "did my click even land" — pip/uv's own
    output lines ARE the progress signal, there is no separate percentage
    computed anywhere."""
    from houdini_agent_panel.updates import Update

    widget = _make_panel(qapp)
    update = Update(
        kind="panel", target="houdini-agent-panel", label="houdini-agent-panel",
        current="1.1.0", latest="1.2.0",
    )

    widget._on_panel_update_progressed(update, "Downloading houdini_agent_panel-1.2.0-py3-none-any.whl")

    assert "Downloading houdini_agent_panel-1.2.0" in widget._notice._label.text()
    assert widget._notice.isHidden() is False

    widget.shutdown()


def test_panel_update_progress_never_shows_the_raw_pip_command(qapp):
    """`deps.py`'s own `f"Installing dependencies: {printable_argv(argv)}"`
    reaches this worker's `progressed` signal like any other output line —
    but a `--target "/Users/.../deps/py3.11" houdini-agent-panel==...` line
    wrapping across two lines, immediately followed by a long silent
    stretch while `hython` itself starts (`mcp_runtime.py`'s own 8.9-16.5s
    measurement), read as a hung update. The notice must never show it —
    the exact line still reaches the log either way
    (`SelfUpdateWorker.work`'s own `_log.info`, not tested here)."""
    from houdini_agent_panel.updates import Update

    widget = _make_panel(qapp)
    update = Update(
        kind="panel", target="houdini-agent-panel", label="houdini-agent-panel",
        current="1.1.0", latest="1.2.0",
    )
    widget._on_panel_update_progressed(update, "starting…")
    widget._on_panel_update_progressed(
        update,
        'Installing dependencies: hython -m pip install --upgrade --target '
        '"/Users/artist/Library/Application Support/HoudiniAgentPanel/deps/py3.11" '
        "houdini-agent-panel==1.2.0",
    )

    text = widget._notice._label.text()
    assert "Installing dependencies" not in text
    assert "--target" not in text
    assert "starting…" in text, "the last real line should stay on screen, not go blank"

    widget.shutdown()


def test_panel_update_notice_shows_elapsed_seconds_once_started(qapp):
    """"He thought it had hung, because nothing changed for a long time and
    the only visible text was a static command." An elapsed-seconds count
    is the honest thing to show while the child is silent — not a fake
    progress bar, just proof the panel is still watching."""
    import time as time_module

    from houdini_agent_panel.updates import Update

    widget = _make_panel(qapp)
    update = Update(
        kind="panel", target="houdini-agent-panel", label="houdini-agent-panel",
        current="1.1.0", latest="1.2.0",
    )

    assert "(" not in widget._notice._label.text() or widget._notice.isHidden()

    widget._panel_update_started_at = time_module.monotonic() - 5.0
    widget._panel_update_display_line = "Downloading…"
    widget._render_panel_update_notice(update)

    text = widget._notice._label.text()
    assert "Downloading…" in text
    import re

    match = re.search(r"\((\d+)s\)", text)
    assert match is not None, f"no elapsed-seconds count shown: {text!r}"
    assert int(match.group(1)) >= 4

    widget.shutdown()


def test_panel_update_tick_timer_starts_with_the_worker_and_stops_on_success(qapp, monkeypatch):
    """The ticker exists to keep the notice visibly alive while the update
    runs, and only then — it must not outlive the worker, ticking a
    finished update's own restart notice."""
    from houdini_agent_panel.updates import Update
    from houdini_agent_panel.ui.self_update import SelfUpdateWorker

    monkeypatch.setattr(SelfUpdateWorker, "work", lambda self: None)

    widget = _make_panel(qapp)
    update = Update(
        kind="panel", target="houdini-agent-panel", label="houdini-agent-panel",
        current="1.1.0", latest="1.2.0",
    )
    widget._start_update(update)

    assert widget._panel_update_tick_timer is not None
    assert widget._panel_update_started_at is not None

    widget._panel_update_worker.wait(3000)
    qapp.processEvents()

    widget._on_panel_update_succeeded(update)

    assert widget._panel_update_tick_timer is None
    assert widget._panel_update_started_at is None
    assert widget._panel_update_display_line == ""

    widget.shutdown()


def test_panel_update_success_shows_a_persistent_restart_notice(qapp):
    """"Say plainly that the new version is installed and takes effect
    after Houdini restarts, and keep saying it — a persistent state, not a
    line that scrolls away in the feed." Also names the lazy-import hazard
    `self_update.py` measured, rather than staying silent about it."""
    from houdini_agent_panel.updates import Update

    widget = _make_panel(qapp)
    update = Update(
        kind="panel", target="houdini-agent-panel", label="houdini-agent-panel",
        current="1.1.0", latest="1.2.0",
    )

    widget._on_panel_update_succeeded(update)

    assert widget._panel_update_worker is None
    assert widget._panel_update_restart_pending is update
    text = widget._notice._label.text()
    assert "1.2.0" in text
    assert "restart" in text.lower()
    assert "new agent" in text.lower() or "new" in text.lower()
    assert widget._notice.isHidden() is False

    widget.shutdown()


def test_panel_update_success_notice_survives_a_later_refresh_cycle(qapp):
    """The restart reminder must not be silently replaced by some OTHER
    agent's update banner or a fresh announcement arriving from the next
    periodic refresh — that reads as the reminder being resolved when it
    never was, and there is only one notice strip to show either in."""
    from houdini_agent_panel.announcements import Announcement
    from houdini_agent_panel.updates import Update

    widget = _make_panel(qapp)
    update = Update(
        kind="panel", target="houdini-agent-panel", label="houdini-agent-panel",
        current="1.1.0", latest="1.2.0",
    )
    widget._on_panel_update_succeeded(update)

    other_update = Update(kind="agent", target="kimi", label="Kimi CLI 2.0", current="1.0", latest="2.0")

    class _Result:
        announcements: list = [Announcement(id="a1", severity="info", title="unrelated announcement")]
        updates = [other_update]

    widget._on_refresh_done(_Result(), [])
    qapp.processEvents()

    assert widget._panel_update_restart_pending is update
    text = widget._notice._label.text()
    assert "1.2.0" in text and "restart" in text.lower()

    widget.shutdown()


def test_a_refresh_arriving_mid_update_does_not_clobber_the_progress_notice(qapp, monkeypatch):
    """A periodic update-check landing WHILE `SelfUpdateWorker` is still
    running is a real timing window, not a hypothetical — `_on_refresh_
    done`'s own early return used to trigger only once the update had
    already SUCCEEDED (`_panel_update_restart_pending`), leaving the
    IN-FLIGHT progress display unprotected: an announcement or another
    agent's update banner arriving during the run would erase the only
    indicator the artist has that anything is happening at all."""
    from houdini_agent_panel.announcements import Announcement
    from houdini_agent_panel.updates import Update
    from houdini_agent_panel.ui.self_update import SelfUpdateWorker

    # A worker that stays "running" for a moment — this test only needs
    # `_panel_update_worker is not None` to still be true when the refresh
    # lands, not a real result.
    monkeypatch.setattr(SelfUpdateWorker, "work", lambda self: QtCore.QThread.msleep(500))

    widget = _make_panel(qapp)
    update = Update(
        kind="panel", target="houdini-agent-panel", label="houdini-agent-panel",
        current="1.1.0", latest="1.2.0",
    )
    widget._start_update(update)
    widget._on_panel_update_progressed(update, "Downloading…")
    progress_text = widget._notice._label.text()

    other_update = Update(kind="agent", target="kimi", label="Kimi CLI 2.0", current="1.0", latest="2.0")

    class _Result:
        announcements: list = [Announcement(id="a1", severity="info", title="unrelated announcement")]
        updates = [other_update]

    widget._on_refresh_done(_Result(), [])
    qapp.processEvents()

    assert widget._notice._label.text() == progress_text

    widget._panel_update_worker.requestInterruption()
    widget._panel_update_worker.wait(3000)
    widget.shutdown()


def test_panel_update_failure_names_the_reason_and_restores_the_offer(qapp, monkeypatch):
    """On failure: the exact reason, and only THEN the manual command as a
    fallback — never the only route any more. The original offer comes
    back so retrying is one click, not a re-explanation."""
    from houdini_agent_panel.updates import Update

    widget = _make_panel(qapp)
    notes: list[str] = []
    monkeypatch.setattr(widget, "_note", lambda text, **_: notes.append(text))
    update = Update(
        kind="panel", target="houdini-agent-panel", label="houdini-agent-panel",
        current="1.1.0", latest="1.2.0",
    )
    widget._panel_update_worker = object()  # stands in for "one was running"

    widget._on_panel_update_failed(
        update,
        "Could not write the new files for houdini-agent-panel — something still has "
        "them open. Close Houdini and run the update again.",
    )

    assert widget._panel_update_worker is None
    assert any("close houdini" in n.lower() for n in notes)
    assert widget._active_update is update
    assert widget._notice.isHidden() is False

    widget.shutdown()


def test_dismissing_the_restart_notice_does_not_pollute_seen_announcements(qapp):
    """The restart-pending id is synthetic, not a real feed announcement —
    `_remember_seen` writing it to `settings.seen_announcements` would be
    silently wrong (it can never match anything a real feed sends) even
    though nothing outwardly breaks; the dismiss path has to know the
    difference, the same way it already knows an update offer's id isn't
    one either."""
    from houdini_agent_panel.updates import Update

    widget = _make_panel(qapp)
    update = Update(
        kind="panel", target="houdini-agent-panel", label="houdini-agent-panel",
        current="1.1.0", latest="1.2.0",
    )
    widget._on_panel_update_succeeded(update)
    notice_id = panel_mod._panel_update_notice_id(update)

    widget._on_notice_dismissed(notice_id)

    assert widget._panel_update_restart_pending is None
    saved = settings_mod.load()
    assert notice_id not in saved.seen_announcements

    widget.shutdown()


def test_announcement_button_still_opens_its_link_not_an_update_path(qapp):
    """`_on_notice_action` now branches on whether an update is active — a
    plain announcement's own button must keep working exactly as before."""
    from houdini_agent_panel.announcements import Announcement, Button

    widget = _make_panel(qapp)
    ann = Announcement(
        id="a1", severity="info", title="Heads up", buttons=(Button(label="Learn more", url="https://x/"),)
    )

    class _Result:
        announcements = [ann]
        updates: list = []

    widget._on_refresh_done(_Result())
    qapp.processEvents()
    assert widget._active_update is None

    opened = []
    widget._open_url = lambda url: opened.append(url)

    widget._notice.action_clicked.emit("a1", "https://x/")

    assert opened == ["https://x/"]
    assert "a1" in widget._settings.seen_announcements

    widget.shutdown()


def test_the_banner_does_not_offer_a_version_already_installed(qapp, monkeypatch):
    """Update results are cached for a day; the manifest changes the moment
    an agent is launched or installed. So the banner could go on offering
    0.64.2 to somebody already running 0.64.2 — and pressing it does nothing
    observable, because installing a version that is already there is a
    no-op. A button that cannot act is indistinguishable from a broken one,
    which is exactly how this arrived."""
    from houdini_agent_panel.ui import panel as panel_module

    class _Update:
        kind = "agent"
        target = "claude-acp"
        latest = "0.64.2"
        current = "0.64.1"
        label = "Claude Agent 0.64.2"

    monkeypatch.setattr(
        panel_module.runtime if hasattr(panel_module, "runtime") else panel_module,
        "installed_version",
        lambda _id: "0.64.2",
        raising=False,
    )
    from houdini_agent_panel import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "installed_version", lambda _id: "0.64.2")

    assert panel_module._update_is_stale(_Update()) is True

    monkeypatch.setattr(runtime_module, "installed_version", lambda _id: "0.64.1")
    assert panel_module._update_is_stale(_Update()) is False


def test_an_unconfigured_agent_is_told_apart_from_a_stuck_one(qapp, monkeypatch):
    """Measured on all six agents with an empty HOME: a never-configured
    agent connects, advertises NO auth methods, and then never answers
    `session/new`. The panel's sign-in screen is drawn FROM those auth
    methods, so it had nothing to show and said "it may be busy or stuck, try
    switching agents" — a loop with no exit, since every other agent behaves
    the same way on that machine.

    `claude-acp` specifically (not just any agent with no methods) — this
    used to route through a second, un-corrected copy of "type /login",
    the exact guess `_offer_login_command` was fixed elsewhere NOT to make
    for claude-acp (measured: an empty `availableCommands` list). There is
    no live session at the point `_report_stalled_new_session` runs
    (`session/new` is what stalled), so it can never confirm a real
    `/login` command either way — the honest answer is the SAME per-agent
    advice `_offer_login_command`/`_no_methods_advice` already give
    (`claude setup-token`), not a blind guess that a later measurement
    showed was wrong for this exact agent.
    """
    from houdini_agent_panel import client as client_mod
    from houdini_agent_panel.ui import panel as panel_mod

    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("claude-acp")
    notes: list[str] = []
    monkeypatch.setattr(widget, "_note", notes.append)
    monkeypatch.setattr(
        panel_mod.shared_client(widget._agent_id),
        "agent_info",
        lambda: client_mod.AgentInfo(
            name="claude", version="1.0", protocol_version=1,
            supports_image=False, supports_audio=False, supports_embedded_context=False,
            supports_load_session=False, supports_logout=False,
            auth_methods=(),  # the fresh-machine case
        ),
        raising=False,
    )

    widget._report_stalled_new_session(set())

    assert notes, "the artist was told nothing at all"
    assert "switching agents" not in notes[-1], "still sending them round the loop"
    assert "claude setup-token" in notes[-1], (
        f"claude-acp's own measured advice was not given: {notes[-1]!r}"
    )
    assert widget._composer._text_edit.toPlainText() == "", (
        "no live session exists yet to have confirmed a /login command — "
        "typing it in anyway is the exact guess this fix removes"
    )
    widget.shutdown()


def test_a_stalled_session_offers_login_for_an_agent_whose_no_methods_advice_is_generic(
    qapp, monkeypatch
):
    """The other side of the same fix: an agent NOT in `_NO_METHODS_ADVICE`
    still gets the shared, generic no-methods advice — not the panel's
    OWN blind "/login" guess and not silence."""
    from houdini_agent_panel import client as client_mod
    from houdini_agent_panel.ui import panel as panel_mod

    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("some-other-agent")
    notes: list[str] = []
    monkeypatch.setattr(widget, "_note", notes.append)
    monkeypatch.setattr(
        panel_mod.shared_client(widget._agent_id),
        "agent_info",
        lambda: client_mod.AgentInfo(
            name="Some Other Agent", version="1.0", protocol_version=1,
            supports_image=False, supports_audio=False, supports_embedded_context=False,
            supports_load_session=False, supports_logout=False,
            auth_methods=(),
        ),
        raising=False,
    )

    widget._report_stalled_new_session(set())

    assert notes, "the artist was told nothing at all"
    assert panel_mod.AgentPanel._GENERIC_NO_METHODS_ADVICE in notes[-1]
    widget.shutdown()


def test_starting_a_session_warns_when_the_mcp_interpreter_is_gone(qapp, monkeypatch):
    """Point 3 of the ephemeral-python fix: even a correctly-installed
    HAP_PYTHON can rot later (a pruned uv cache, a recreated venv). The
    panel used to stay completely silent about it — the agent just came up
    without any Houdini tools, and the artist only found out when it said
    so itself."""
    current = settings_mod.load()
    current.default_agent = "claude-acp"
    current.autostart_agent = False
    settings_mod.save(current)

    widget = _make_panel(qapp)
    client = panel_mod.shared_client("claude-acp")
    monkeypatch.setattr(client, "is_running", lambda: True)
    monkeypatch.setattr(client, "new_session", lambda **kwargs: None)
    monkeypatch.setattr(
        panel_mod.scene,
        "mcp_python_status",
        lambda: "The Houdini MCP server's interpreter is gone (HAP_PYTHON=/gone/python)",
    )

    widget._start_new_session()

    entries = widget._model("__idle__").entries()
    assert entries, "the artist was told nothing at all"
    assert entries[-1].kind == "error"
    assert "gone" in entries[-1].text
    widget.shutdown()


def test_starting_a_session_stays_quiet_when_the_mcp_interpreter_is_fine(qapp, monkeypatch):
    current = settings_mod.load()
    current.default_agent = "claude-acp"
    current.autostart_agent = False
    settings_mod.save(current)

    widget = _make_panel(qapp)
    client = panel_mod.shared_client("claude-acp")
    monkeypatch.setattr(client, "is_running", lambda: True)
    monkeypatch.setattr(client, "new_session", lambda **kwargs: None)
    monkeypatch.setattr(panel_mod.scene, "mcp_python_status", lambda: None)
    before = list(widget._model("__idle__").entries())

    widget._start_new_session()

    assert widget._model("__idle__").entries() == before, "a note was added despite no problem"
    widget.shutdown()


# --- waiting for a fx server that hasn't confirmed ready yet ---------------
#
# The race this answers: `fxhoudinimcp_server`'s own auto-start is
# asynchronous (`uiready.py` polls readiness on a worker thread), and
# `autostart_agent` can create a session before that poll finishes. A
# session opened at that moment pins no port and is toolless for its whole
# life (`scene.mcp_servers()`'s own comment). `scene.fx_pending()` is the
# signal that says "the poll is in flight, worth a bounded wait" — these
# tests are about `_start_new_session` actually acting on it.


def test_new_session_does_not_wait_when_fx_is_not_pending(qapp, monkeypatch):
    """The common case — either the port is already known, or there is
    nothing to wait FOR (no plugin). No poll timer, no wait note."""
    current = settings_mod.load()
    current.default_agent = "claude-acp"
    current.autostart_agent = False
    settings_mod.save(current)

    widget = _make_panel(qapp)
    client = panel_mod.shared_client("claude-acp")
    monkeypatch.setattr(client, "is_running", lambda: True)
    calls = []
    monkeypatch.setattr(client, "new_session", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(panel_mod.scene, "fx_pending", lambda: False)
    monkeypatch.setattr(panel_mod.scene, "mcp_python_status", lambda: None)

    widget._start_new_session()

    assert calls, "session/new was never sent"
    widget.shutdown()


def test_new_session_waits_for_a_pending_fx_server_then_opens_it(qapp, monkeypatch):
    """The fx server confirms ready midway through the wait — the session
    is opened with the by-then-current `mcp_servers()`, not the stale one
    from the moment `_start_new_session` was first called."""
    current = settings_mod.load()
    current.default_agent = "claude-acp"
    current.autostart_agent = False
    settings_mod.save(current)

    widget = _make_panel(qapp)
    client = panel_mod.shared_client("claude-acp")
    monkeypatch.setattr(client, "is_running", lambda: True)
    calls = []
    monkeypatch.setattr(client, "new_session", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(panel_mod.scene, "mcp_python_status", lambda: None)

    pending = [True]
    monkeypatch.setattr(panel_mod.scene, "fx_pending", lambda: pending[0])
    monkeypatch.setattr(
        panel_mod.scene,
        "mcp_servers",
        lambda: [{"name": "fxhoudini", "command": "python", "args": [], "env": [{"name": "HOUDINI_PORT", "value": "8100"}]}],
    )

    widget._start_new_session()
    assert not calls, "the session was opened before waiting for the fx server at all"

    pending[0] = False  # the readiness poll settled, port now known
    widget._poll_fx_wait(panel_mod._FX_WAIT_CEILING_MS)

    assert calls, "the session was never opened once the fx server became ready"
    assert calls[-1]["mcp_servers"][0]["env"] == [{"name": "HOUDINI_PORT", "value": "8100"}]
    widget.shutdown()


def test_new_session_gives_up_waiting_at_the_ceiling(qapp, monkeypatch):
    """The fx server never confirms ready within the wait's ceiling — the
    session opens anyway (never leaving the artist with a dead panel), and
    the feed says plainly why there are no Houdini tools this time."""
    current = settings_mod.load()
    current.default_agent = "claude-acp"
    current.autostart_agent = False
    settings_mod.save(current)

    widget = _make_panel(qapp)
    client = panel_mod.shared_client("claude-acp")
    monkeypatch.setattr(client, "is_running", lambda: True)
    calls = []
    monkeypatch.setattr(client, "new_session", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(panel_mod.scene, "mcp_python_status", lambda: None)
    monkeypatch.setattr(panel_mod.scene, "fx_pending", lambda: True)  # never resolves

    widget._start_new_session()
    assert not calls

    widget._poll_fx_wait(0)  # the ceiling has been reached

    assert calls, "the session was never opened once the ceiling was hit"
    entries = widget._model("__idle__").entries()
    assert entries, "the artist was told nothing about the missing Houdini tools"
    assert entries[-1].kind == "error"
    assert "Houdini" in entries[-1].text
    widget.shutdown()


def test_new_session_wait_is_announced_when_no_boot_strip_is_on_screen(qapp, monkeypatch):
    """A manual "+" on an already-running agent shows no boot strip
    (`BootStatus.set_phase`'s own guard) — the busy indicator covers the
    ordinary `session/new` wait, but nothing covers THIS extra one ahead of
    it, so it needs its own word in the feed."""
    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("codex-acp")
    monkeypatch.setattr(panel_mod.scene, "mcp_python_status", lambda: None)
    monkeypatch.setattr(panel_mod.scene, "fx_pending", lambda: True)
    client = panel_mod.shared_client(widget._agent_id)
    monkeypatch.setattr(client, "is_running", lambda: True)
    monkeypatch.setattr(client, "new_session", lambda **kwargs: None)
    assert widget._composer.boot_status().is_booting() is False

    widget._start_new_session()

    entries = widget._model("__idle__").entries()
    assert entries, "the artist was left with no explanation for the delay"
    assert "Houdini" in entries[-1].text
    widget.shutdown()


def test_new_session_wait_reuses_the_boot_strip_when_one_is_already_up(qapp, monkeypatch):
    """During an autostart boot the strip is already on screen — the wait
    speaks through its existing "Opening a conversation" step instead of
    also dropping a redundant note into the feed."""
    from houdini_agent_panel.ui import boot_status as boot_mod

    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("codex-acp")
    monkeypatch.setattr(panel_mod.scene, "mcp_python_status", lambda: None)
    monkeypatch.setattr(panel_mod.scene, "fx_pending", lambda: True)
    client = panel_mod.shared_client(widget._agent_id)
    monkeypatch.setattr(client, "is_running", lambda: True)
    monkeypatch.setattr(client, "new_session", lambda **kwargs: None)
    widget._composer.begin_boot("Codex")
    before = list(widget._model("__idle__").entries())

    widget._start_new_session()

    assert widget._model("__idle__").entries() == before, (
        "a redundant feed note was added while the boot strip already said it"
    )
    strip = widget._composer.boot_status()
    assert strip.phase() == boot_mod.PHASE_SESSION
    assert strip.text() != "Opening a conversation", "the wait must say more than the generic step"
    widget.shutdown()


def test_a_second_new_session_click_while_the_first_is_in_flight_is_ignored(qapp, monkeypatch):
    """Measured for real, from the owner's own log: `session/new` can take
    38s (a heavy MCP fleet, `claude_show_host_mcp_servers` on). Neither "+"
    disabled itself and nothing appeared on screen for up to 20s
    (`_report_stalled_new_session`'s own grace period), so a second click
    sent a second, independent `session/new` — two real sessions, not one
    request logged twice. This is the guard: a second call while the first
    hasn't resolved sends nothing."""
    current = settings_mod.load()
    current.default_agent = "claude-acp"
    current.autostart_agent = False
    settings_mod.save(current)

    widget = _make_panel(qapp)
    client = panel_mod.shared_client("claude-acp")
    monkeypatch.setattr(client, "is_running", lambda: True)
    calls = []
    monkeypatch.setattr(client, "new_session", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(panel_mod.scene, "fx_pending", lambda: False)
    monkeypatch.setattr(panel_mod.scene, "mcp_python_status", lambda: None)

    widget._start_new_session()
    widget._start_new_session()
    widget._start_new_session()

    assert len(calls) == 1, "a second click while the first request was still in flight sent another one"
    widget.shutdown()


def test_the_new_session_buttons_go_busy_the_moment_the_click_is_accepted(qapp, monkeypatch):
    """The visible half of the same guard — asked for explicitly: feedback
    has to appear the instant the click lands, not up to 20s later when
    `_report_stalled_new_session` finally has something to say. An artist
    watching a dead-looking button for 14 silent seconds is exactly what
    produced the second session in the first place."""
    current = settings_mod.load()
    current.default_agent = "claude-acp"
    current.autostart_agent = False
    settings_mod.save(current)

    widget = _make_panel(qapp)
    client = panel_mod.shared_client("claude-acp")
    monkeypatch.setattr(client, "is_running", lambda: True)
    monkeypatch.setattr(client, "new_session", lambda **kwargs: None)
    monkeypatch.setattr(panel_mod.scene, "fx_pending", lambda: False)
    monkeypatch.setattr(panel_mod.scene, "mcp_python_status", lambda: None)

    assert widget._header._new_conversation_button.isEnabled() is True
    assert widget._conversations._new_button.isEnabled() is True

    widget._start_new_session()

    assert widget._header._new_conversation_button.isEnabled() is False
    assert widget._conversations._new_button.isEnabled() is False
    widget.shutdown()


def test_the_new_session_buttons_re_enable_once_the_session_actually_starts(qapp, monkeypatch):
    current = settings_mod.load()
    current.default_agent = "claude-acp"
    current.autostart_agent = False
    settings_mod.save(current)

    widget = _make_panel(qapp)
    client = panel_mod.shared_client("claude-acp")
    monkeypatch.setattr(client, "is_running", lambda: True)
    monkeypatch.setattr(client, "new_session", lambda **kwargs: None)
    monkeypatch.setattr(panel_mod.scene, "fx_pending", lambda: False)
    monkeypatch.setattr(panel_mod.scene, "mcp_python_status", lambda: None)

    widget._start_new_session()
    assert widget._header._new_conversation_button.isEnabled() is False

    widget._on_session_started("s1", _session("s1"))

    assert widget._header._new_conversation_button.isEnabled() is True
    assert widget._conversations._new_button.isEnabled() is True

    # And a genuinely new click now sends a genuinely new request.
    calls = []
    monkeypatch.setattr(client, "new_session", lambda **kwargs: calls.append(kwargs))
    widget._start_new_session()
    assert calls
    widget.shutdown()


def test_the_new_session_buttons_re_enable_after_an_error(qapp, monkeypatch):
    """A `session/new` that fails outright must not leave the artist locked
    out of trying again — only a still-outstanding request blocks a second
    click."""
    current = settings_mod.load()
    current.default_agent = "claude-acp"
    current.autostart_agent = False
    settings_mod.save(current)

    widget = _make_panel(qapp)
    monkeypatch.setattr(widget, "_note", lambda *_a, **_k: None)
    client = panel_mod.shared_client("claude-acp")
    monkeypatch.setattr(client, "is_running", lambda: True)
    monkeypatch.setattr(client, "new_session", lambda **kwargs: None)
    monkeypatch.setattr(panel_mod.scene, "fx_pending", lambda: False)
    monkeypatch.setattr(panel_mod.scene, "mcp_python_status", lambda: None)

    widget._start_new_session()
    assert widget._header._new_conversation_button.isEnabled() is False

    widget._on_error("", "no connection to the agent")

    assert widget._header._new_conversation_button.isEnabled() is True
    assert widget._conversations._new_button.isEnabled() is True
    widget.shutdown()


def test_a_half_written_prompt_is_never_overwritten(qapp):
    """Offering a command is help; losing what someone was typing to make
    room for it is not."""
    from houdini_agent_panel.ui.composer import Composer

    composer = Composer()
    composer._text_edit.setPlainText("make the rotor emit dust when")
    composer.set_text("/login")
    assert composer._text_edit.toPlainText() == "make the rotor emit dust when"
    composer.deleteLater()


def test_an_empty_agent_list_says_why(qapp, monkeypatch):
    """Reported from a fresh Linux install: Settings → Agents was empty, with
    nothing to install and no explanation.

    `fetch_registry` falls back to a cache of any age, so an empty result
    means there is no cache either — a first run that could not reach the
    network. The panel simply skipped `set_agents` and said nothing, and an
    empty list on a fresh install is indistinguishable from a panel that
    does not work.
    """
    from houdini_agent_panel.ui import panel as panel_mod

    widget = panel_mod.AgentPanel()
    notes: list[str] = []
    monkeypatch.setattr(widget, "_note", lambda text, **_: notes.append(text))

    widget._on_refresh_done(None, [])

    assert notes, "an empty agent list was reported as nothing at all"
    assert "Network" in notes[-1], (
        f"a studio firewall is the likeliest cause and must be named: {notes[-1]!r}"
    )
    widget.shutdown()


def test_a_first_install_refreshes_the_menu_and_says_what_to_do(qapp, monkeypatch):
    """An npx agent installs in under a second — nothing downloads, npx
    fetches on first launch — so a row flipping to "installed" is the only
    sign anything happened. Worse, the chip menu is built from
    `settings.installed_agents` and was never rebuilt, so the agent just
    installed was missing from the one menu used to pick it."""
    from houdini_agent_panel.ui import panel as panel_mod

    widget = panel_mod.AgentPanel()
    refreshed: list[int] = []
    notes: list[str] = []
    monkeypatch.setattr(widget, "_refresh_agent_chip_menu", lambda: refreshed.append(1))
    monkeypatch.setattr(widget, "_note", notes.append)

    widget._on_agent_install_succeeded("codex-acp")

    assert refreshed, "the agent menu still lists what was there before the install"
    assert notes and "agent menu" in notes[-1], f"no next step was given: {notes}"
    widget.shutdown()


def test_the_panels_own_update_runs_immediately_and_clears_the_offer(qapp, monkeypatch):
    """Pressing Update on a panel update used to only ever print `uvx
    --refresh --from ... install` and leave the notice up so pressing it
    again repeated the same line (reported as three identical messages
    stacked). Now it runs that exact command for real
    (`SelfUpdateWorker.work` stubbed here — no real subprocess in a unit
    test) and clears the offer immediately, since a second click while one
    is already in flight must be a no-op, not a second update."""
    from houdini_agent_panel.ui import panel as panel_mod
    from houdini_agent_panel.ui.self_update import SelfUpdateWorker

    class _Update:
        kind = "panel"
        target = "houdini-agent-panel"
        latest = "0.1.6"
        current = "0.1.4"
        label = "houdini-agent-panel 0.1.6"

    started: list[str] = []
    monkeypatch.setattr(SelfUpdateWorker, "work", lambda self: started.append(self._target))

    widget = panel_mod.AgentPanel()
    widget._active_update = _Update()

    widget._start_update(_Update())

    assert widget._panel_update_worker is not None, "the manual command alone is no longer the whole story"
    assert widget._active_update is None, "the offer stays up and repeats on the next press"

    widget._panel_update_worker.wait(3000)
    qapp.processEvents()
    assert started == ["houdini-agent-panel"]
    widget.shutdown()


def test_a_panel_update_already_installed_is_not_offered(qapp, monkeypatch):
    """Reported from a machine running 0.1.7 while the banner offered 0.1.5,
    with the button leading nowhere because there was nothing left to do.

    An earlier guard skipped panel updates on the reasoning that their
    version comes from the running process and so cannot go stale. Backwards:
    update results are cached for a day and the panel updates more often than
    anything else, so its banner goes stale first.
    """
    from houdini_agent_panel import __version__
    from houdini_agent_panel.ui import panel as panel_mod

    class _Update:
        kind = "panel"
        target = "houdini-agent-panel"
        current = "0.1.4"
        label = "houdini-agent-panel"

        def __init__(self, latest):
            self.latest = latest

    assert panel_mod._update_is_stale(_Update(__version__)) is True, (
        "an update to the version already running was still offered"
    )
    assert panel_mod._update_is_stale(_Update("0.0.1")) is True
    assert panel_mod._update_is_stale(_Update("99.0.0")) is False, (
        "a genuinely newer version must still be offered"
    )


def test_a_half_downloaded_npx_package_is_named_as_the_cause(qapp, monkeypatch):
    """`sh: line 1: codex-acp: command not found` — a shell error naming a
    command the artist never typed, repeated on every launch forever.

    npx can leave its cache half-made: the directory exists, the package does
    not, and it then runs the missing binary and exits 0. Fixing the network
    afterwards changes nothing, because npx believes it already finished.
    Diagnosed on a real machine whose transfers were dying mid-stream.
    """
    from houdini_agent_panel.ui import panel as panel_mod

    widget = panel_mod.AgentPanel()
    notes: list[str] = []
    monkeypatch.setattr(widget, "_note", lambda text, **_: notes.append(text))

    widget._on_log_line("sh: line 1: codex-acp: command not found")

    assert notes, "the artist was left with a bare shell error"
    assert "_npx" in notes[-1], f"the fix must be spelled out: {notes[-1]!r}"
    widget.shutdown()


def test_the_boot_strip_follows_the_panel_through_a_real_start(qapp, monkeypatch):
    """Wiring test: the phases must come from the code paths that do the
    work, not from a timer. Reported as "while an agent is loading there's
    no indication at all that it's still loading" — two lines flashed past
    in the feed and then the chips appeared out of nowhere."""
    from houdini_agent_panel import client as client_mod
    from houdini_agent_panel.ui import boot_status as boot_mod
    from houdini_agent_panel.ui import panel as panel_mod

    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("codex-acp")
    monkeypatch.setattr(widget, "_note", lambda *_: None)
    strip = widget._composer.boot_status()

    widget._composer.begin_boot("Codex")
    assert strip.phase() == boot_mod.PHASE_PREPARING

    monkeypatch.setattr(panel_mod.shared_client(widget._agent_id), "start", lambda *a, **k: None)
    widget._on_launch_ready(object(), "Codex")
    assert strip.phase() == boot_mod.PHASE_LAUNCHING

    info = client_mod.AgentInfo(
        name="codex", version="1.1.9", protocol_version=1,
        supports_image=False, supports_audio=False, supports_embedded_context=False,
        supports_load_session=False, supports_logout=False, auth_methods=(),
    )
    monkeypatch.setattr(widget, "_start_new_session", lambda: None)
    widget._on_connected(info)
    assert strip.phase() == boot_mod.PHASE_CONNECTING

    widget._on_session_started("s1", sessions.SessionState("s1", "chat", "/tmp", 0.0))
    assert strip.phase() == boot_mod.PHASE_READY
    assert strip.is_booting() is False
    widget.shutdown()


def test_a_failed_start_leaves_no_progress_bar_behind(qapp, monkeypatch):
    """A bar frozen partway reads as "still coming"."""
    from houdini_agent_panel.ui import panel as panel_mod

    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("codex-acp")
    monkeypatch.setattr(widget, "_note", lambda *_, **__: None)
    monkeypatch.setattr(widget, "_open_agent_management", lambda: None)
    widget._composer.begin_boot("Codex")

    widget._on_launch_prep_failed("npx: command not found")

    assert widget._composer.boot_status().isHidden() is True
    assert widget._composer.boot_status().is_booting() is False
    assert widget._composer._boot_scrim.isHidden() is True, (
        "the input stayed covered after the agent failed to start"
    )
    widget.shutdown()


def test_the_input_is_covered_while_an_agent_starts_and_uncovered_after(qapp, monkeypatch):
    """Asked for: the input should be visually blocked during a boot, not
    left looking live. There is no agent to send anything to yet, and an
    inert control that looks ready invites the artist to type a paragraph
    into nothing."""
    from houdini_agent_panel import client as client_mod
    from houdini_agent_panel.ui import panel as panel_mod

    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("codex-acp")
    monkeypatch.setattr(widget, "_note", lambda *_: None)
    monkeypatch.setattr(widget, "_start_new_session", lambda: None)
    composer = widget._composer

    composer.begin_boot("Codex")
    assert composer._boot_scrim.isHidden() is False
    assert composer._text_edit.isReadOnly() is True
    assert composer._buddy.isHidden() is True, "the buddy sat over a dead input"

    info = client_mod.AgentInfo(
        name="codex", version="1.1.9", protocol_version=1,
        supports_image=False, supports_audio=False, supports_embedded_context=False,
        supports_load_session=False, supports_logout=False, auth_methods=(),
    )
    widget._on_connected(info)
    widget._on_session_started("s1", sessions.SessionState("s1", "chat", "/tmp", 0.0))

    assert composer._boot_scrim.isHidden() is True
    assert composer._text_edit.isReadOnly() is False
    widget.shutdown()


def test_a_new_chat_on_a_running_agent_does_not_cover_the_input(qapp, monkeypatch):
    """`session/new` fires there too. Covering the input for it would block
    typing every time somebody presses "+"."""
    from houdini_agent_panel.ui import panel as panel_mod

    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("codex-acp")
    monkeypatch.setattr(widget, "_note", lambda *_: None)
    composer = widget._composer

    widget._on_session_started("s1", sessions.SessionState("s1", "chat", "/tmp", 0.0))

    assert composer._boot_scrim.isHidden() is True
    assert composer._text_edit.isReadOnly() is False
    widget.shutdown()


def test_a_stalled_new_session_gives_the_input_back(qapp, monkeypatch):
    """Seen on the Linux machine: Codex connected, never answered
    `session/new`, and the panel said so in the feed — while the progress
    strip sat full at 4/4 and the input stayed blurred and unusable. The
    artist could not even type the `/login` the message was suggesting."""
    from houdini_agent_panel import client as client_mod
    from houdini_agent_panel.ui import panel as panel_mod

    widget = panel_mod.AgentPanel()
    widget._rejoin_agent("codex-acp")
    monkeypatch.setattr(widget, "_note", lambda *_: None)
    monkeypatch.setattr(
        panel_mod.shared_client(widget._agent_id),
        "agent_info",
        lambda: client_mod.AgentInfo(
            name="codex", version="1.1.9", protocol_version=1,
            supports_image=False, supports_audio=False, supports_embedded_context=False,
            supports_load_session=False, supports_logout=False, auth_methods=(),
        ),
        raising=False,
    )
    composer = widget._composer
    composer.begin_boot("Codex")
    assert composer._boot_scrim.isHidden() is False

    widget._report_stalled_new_session(set())

    assert composer._boot_scrim.isHidden() is True, "the input was left covered"
    assert composer._text_edit.isReadOnly() is False, "the input was left read-only"
    assert composer.boot_status().isHidden() is True, "the strip was left at 4/4 forever"
    widget.shutdown()


def test_switching_agent_from_settings_returns_to_the_conversation(qapp, monkeypatch):
    """The agent chip lives in the header, which stays put while Settings is
    open — so an agent can be switched from that screen, and the artist was
    then left looking at preferences while the thing they asked for happened
    out of sight."""
    from houdini_agent_panel.ui import panel as panel_mod

    widget = panel_mod.AgentPanel()
    monkeypatch.setattr(widget, "_note", lambda *_: None)
    monkeypatch.setattr(widget, "_start_agent", lambda *_: None)
    widget._show_page(widget.PAGE_SETTINGS)
    assert widget._pages.currentIndex() == widget.PAGE_SETTINGS

    widget._on_agent_chosen("codex-acp")

    assert widget._pages.currentIndex() == widget.PAGE_TRANSCRIPT
    widget.shutdown()

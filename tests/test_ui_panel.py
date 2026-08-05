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


def test_without_default_agent_panel_opens_on_agents_settings(qapp):
    """First open: no agent picked, so the human is shown what there is to
    pick from — which is now the "Agents" block in settings, not a separate
    screen."""
    widget = _make_panel(qapp)
    widget._boot()

    assert widget._pages.currentIndex() == panel_mod.AgentPanel.PAGE_SETTINGS
    assert widget._settings_view._scroll.verticalScrollBar().value() == 0
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
    """Houdini may call onDestroyInterface twice; the second call must
    neither crash nor take someone else's client down."""
    widget = _make_panel(qapp)
    widget.shutdown()
    widget.shutdown()


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

    widget._auth_view.method_chosen.emit("oauth")
    widget._auth_view.logout_requested.emit()
    qapp.processEvents()

    assert seen == [("auth", "oauth"), ("logout",)]
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
    monkeypatch.setattr(client, "set_mode", lambda _sid, _mode_id: None)

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

    widget._on_submitted([{"type": "text", "text": "make it rain"}])
    assert started == [True]
    assert prompted == []

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


def test_panel_or_fx_update_gets_instructions_not_a_silent_attempt(qapp, monkeypatch):
    """The panel can't safely replace the pip package it's currently running
    from — telling the artist how beats pretending an in-place update
    happened."""
    from houdini_agent_panel.updates import Update

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

    current_session = widget._current_session()
    session_id = current_session.session_id if current_session else "__idle__"
    feed_text = " ".join(e.text for e in widget._model(session_id).entries())
    # `uvx --refresh`, not pip: that is how the README installs it, and
    # without --refresh uvx would serve its cached copy of the old version.
    assert "uvx --refresh --from houdini-agent-panel" in feed_text
    # And the banner goes. It used to stay, on the reasoning that nothing had
    # been done about the update — but pressing Update again can only repeat
    # the same instruction, and it was reported as three identical messages
    # stacked in the feed.
    assert widget._notice.isHidden() is True

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
    assert "/login" in notes[-1], f"the way out was not named: {notes[-1]!r}"
    assert "switching agents" not in notes[-1], "still sending them round the loop"
    assert widget._composer._text_edit.toPlainText() == "/login", (
        "the command should be ready to send, not merely mentioned"
    )
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
    monkeypatch.setattr(widget, "_note", notes.append)

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


def test_the_panels_own_update_gives_a_command_that_works_and_stops_repeating(qapp, monkeypatch):
    """Pressing Update on a panel update printed `pip install --upgrade` —
    not how anyone installed this — and left the notice up, so pressing it
    again repeated the line. Reported as three identical messages stacked."""
    from houdini_agent_panel.ui import panel as panel_mod

    class _Update:
        kind = "panel"
        target = "houdini-agent-panel"
        latest = "0.1.6"
        current = "0.1.4"
        label = "houdini-agent-panel 0.1.6"

    widget = panel_mod.AgentPanel()
    notes: list[str] = []
    monkeypatch.setattr(widget, "_note", notes.append)
    widget._active_update = _Update()

    widget._start_update(_Update())

    assert "uvx" in notes[-1], f"the command must be the one that installs it: {notes[-1]!r}"
    assert "--refresh" in notes[-1], "without --refresh uvx serves its cached copy"
    assert widget._active_update is None, "the notice stays up and repeats on the next press"
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
    monkeypatch.setattr(widget, "_note", notes.append)

    widget._on_log_line("sh: line 1: codex-acp: command not found")

    assert notes, "the artist was left with a bare shell error"
    assert "_npx" in notes[-1], f"the fix must be spelled out: {notes[-1]!r}"
    widget.shutdown()


def test_the_boot_strip_follows_the_panel_through_a_real_start(qapp, monkeypatch):
    """Wiring test: the phases must come from the code paths that do the
    work, not from a timer. Reported as "во время загрузки агентов никак не
    оповещается, что агент ещё грузится" — two lines flashed past in the
    feed and then the chips appeared out of nowhere."""
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
    monkeypatch.setattr(widget, "_note", lambda *_: None)
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

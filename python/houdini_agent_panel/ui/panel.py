"""Root widget of the panel — where everything comes together.

Three decisions here shape everything else.

**One agent per Houdini process, many sessions.** The agent process and its
connection live in the module, not the widget: a second panel tab must see
the same conversation, not spin up a second process. Widgets come and go,
the connection outlives them.

**No network call and no long operation ever runs on the main thread.**
Houdini paints its UI on the same thread as the viewport; a second spent
here waiting on PyPI is a second of frozen Houdini.

**`hou` — only from here, and only synchronously.** Anything needed from the
scene is grabbed when the panel is built and in response to user actions,
which is to say, always on the main thread. The ACP client's worker thread
never touches `hou`.
"""

from __future__ import annotations

import contextlib
import time
import weakref
from dataclasses import replace
from typing import Any

from .. import client as acp_client
from .. import refresh, scene, sessions, settings as settings_mod
from ..transcript_model import PermissionView, TranscriptModel
from .announcement import BlockingNotice, ConsentStrip, NoticeStrip
from .chips import HeaderBar
from .composer import Composer
from .conversations import ConversationDrawer, summarize_title
from .permissions import PermissionRow
from .qt import QtCore, QtWidgets, Signal
from .transcript import TranscriptView

#: Connection to the agent for the whole Houdini process. Not a widget
#: attribute — otherwise closing one tab would take the conversation open in
#: another tab down with it.
_shared_client: acp_client.AcpClient | None = None

#: Live panels. Weak references: Qt deletes the widgets itself, and holding a
#: strong reference here would just stop them from ever dying.
_live_panels: "weakref.WeakSet[AgentPanel]" = weakref.WeakSet()


def shared_client() -> acp_client.AcpClient:
    global _shared_client
    if _shared_client is None:
        _shared_client = acp_client.AcpClient()
        # Everything the client reports goes to the on-disk log. Without this
        # the panel is undiagnosable on someone else's machine: the log file
        # existed but held only the startup header, never a word about the
        # agent itself.
        try:
            from .. import logbook

            logbook.setup()
            logbook.attach_client(_shared_client)
        except Exception:  # noqa: BLE001 - a log has no right to break the panel
            pass
    return _shared_client


def reset_shared_state_for_tests() -> None:
    """Reset process-wide singletons. Tests only."""
    global _shared_client
    if _shared_client is not None:
        _shared_client.stop()
    _shared_client = None
    _live_panels.clear()
    sessions.reset_pool_for_tests()


class _RefreshWorker(QtCore.QThread):
    """One network round trip for everything the panel needs, off the main thread.

    A dedicated thread, not a timer with a blocking call: even with no
    network, urllib honestly waits out the timeout, and on the main thread
    that looks exactly like a frozen Houdini.

    The registry is fetched here rather than separately by the agents
    section, for two reasons. The design promises one daily request that
    covers versions, announcements, and agents together. And without
    registry entries, `updates.check` literally cannot compare installed
    agent versions — the "update available" badge would only ever show up
    for the panel and fx themselves, never for what the artist is actually
    using.
    """

    done = Signal(object, object)  # RefreshResult | None, list[AgentEntry]

    def __init__(self, current: settings_mod.Settings, parent=None) -> None:
        super().__init__(parent)
        self._settings = current

    def run(self) -> None:  # pragma: no cover - covered via refresh.py
        entries: list = []
        try:
            from .. import registry

            entries = registry.fetch_registry()
        except Exception:  # noqa: BLE001 - the panel must work without a registry
            entries = []

        result = None
        try:
            result = refresh.daily_refresh(
                settings=self._settings,
                panel_version=settings_mod._panel_version(),
                entries=entries,
            )
        except Exception:  # noqa: BLE001 - the feed must never break the panel
            result = None

        self.done.emit(result, entries)


#: How long to wait for the agent to acknowledge a stop before releasing the
#: input ourselves. `session/cancel` is a notification — an agent is free to
#: never answer it, and the panel must not stay locked because of that.
#: Marks a pool entry restored from disk: a conversation the artist can
#: read, with no agent session behind it yet.
_RESTORED_PREFIX = "restored:"

_CANCEL_GRACE_MS = 4000

#: How long `session/new` may take before the panel says something. An agent
#: that spawns an MCP server per session can genuinely need a few seconds;
#: silence past this reads as a dead button, and a dead button is exactly
#: what an artist reports when nothing at all appears after a click.
_NEW_SESSION_GRACE_MS = 20_000

#: Below this the panel is too narrow to give the drawer its own column, so
#: the drawer overlays instead of pushing the conversation aside.
_MIN_BODY_WIDTH = 260

#: Names for the featured six, for when the registry hasn't arrived yet. Not
#: a source of truth — the registry always wins — this only keeps the chip
#: from showing a bare id for the first few seconds.
_FALLBACK_LABELS = {
    "claude-acp": "Claude Agent",
    "codex-acp": "Codex",
    "grok-build": "Grok Build",
    "opencode": "OpenCode",
    "gemini": "Gemini CLI",
    "kimi": "Kimi CLI",
}


class _LaunchPrepWorker(QtCore.QThread):
    """Prepares a LaunchSpec off the main thread.

    Everything slow that used to live in the old _launch_spec lives here: a
    registry round trip and ensure_node, which can mean downloading portable
    Node. None of that belongs on the main thread — GUI Houdini doesn't
    inherit the shell's PATH, so a homebrew node is invisible to it, and a
    download on first launch is close to guaranteed, not a rare case.
    """

    ready = Signal(object, str)   # LaunchSpec, human-readable name
    prep_failed = Signal(str)
    note = Signal(str)

    def __init__(self, agent_id: str, current: settings_mod.Settings, parent=None) -> None:
        super().__init__(parent)
        self._agent_id = agent_id
        self._settings = current

    def run(self) -> None:  # pragma: no cover - thin wrapper, logic lives in runtime
        from .. import registry, runtime

        agent_id = self._agent_id
        try:
            for custom in self._settings.custom_agents:
                if custom.id == agent_id:
                    self.ready.emit(runtime.custom_launch_spec(custom), custom.name or agent_id)
                    return
            entry = None
            for candidate in registry.fetch_registry():
                if candidate.id == agent_id:
                    entry = candidate
                    break
            if entry is None:
                self.prep_failed.emit(
                    f"Agent {agent_id} isn't in the registry or among custom agents."
                )
                return
            from .. import node as node_module

            if entry.needs_node and node_module.find_system_node() is None:
                self.note.emit(
                    f"{entry.name}: no system Node found, fetching the portable one — "
                    "first launch may take a minute…"
                )
            spec = runtime.launch_spec(entry)
            self.ready.emit(spec, entry.name)
        except Exception as exc:  # noqa: BLE001 - the reason goes to the feed
            self.prep_failed.emit(f"Could not prepare agent {agent_id}: {exc}")


class AgentPanel(QtWidgets.QWidget):
    """What ``onCreateInterface()`` returns."""

    PAGE_TRANSCRIPT = 0
    PAGE_SETTINGS = 1
    PAGE_AUTH = 2

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings_mod.load()
        self._pool = sessions.pool()
        self._models: dict[str, TranscriptModel] = {}
        self._pending_permissions: dict[str, str] = {}
        self._permission_views: dict[str, PermissionView] = {}
        self._permission_popover: PermissionRow | None = None
        self._refresh_worker: _RefreshWorker | None = None
        self._launch_worker: _LaunchPrepWorker | None = None
        self._registry_entries: list = []
        self._pending_agent_label: str = ""
        #: Blocks typed before any session existed, waiting for `session/new`.
        self._pending_prompt: list | None = None
        #: Our own conversation id per live agent session. The agent's id
        #: dies with its process; this one is what survives.
        self._conversation_ids: dict[str, str] = {}
        self._restored: list = []
        self._adopting_restored: str | None = None
        self._last_auth_method: str = ""
        #: The `Update` currently shown by the notice strip, if any — set
        #: only from `_on_refresh_done`. `NoticeStrip.action_clicked` fires
        #: for BOTH an announcement's button and this one's "Update" button
        #: (same signal, same slot); this is how `_on_notice_action` tells
        #: them apart.
        self._active_update: Any = None
        #: Set right before stopping the currently-running agent to update
        #: it out from under itself — the agent_id to bring back up once
        #: that update actually finishes (`_on_agent_install_succeeded`).
        self._restart_after_update: str | None = None
        self._closed = False

        self._build()
        self._wire_client()
        self._wire_pool()

        _live_panels.add(self)

        # Boot is deferred to the next event loop pass: Houdini waits for the
        # widget to return from onCreateInterface, and anything done before
        # that return delays the tab opening.
        QtCore.QTimer.singleShot(0, self._boot)

    # ------------------------------------------------------------------ UI

    def _build(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._header = HeaderBar(self)
        self._notice = NoticeStrip(self)
        self._consent = ConsentStrip(self)
        self._pages = QtWidgets.QStackedWidget(self)
        self._composer = Composer(self)
        self._composer.set_buddy(self._settings.buddy)
        self._blocking = BlockingNotice(self)

        self._transcript = TranscriptView(self)
        self._pages.insertWidget(self.PAGE_TRANSCRIPT, self._transcript)
        self._pages.insertWidget(self.PAGE_SETTINGS, self._make_settings_view())
        self._pages.insertWidget(self.PAGE_AUTH, self._make_auth_view())

        # Everything except the header lives in its own column, so the open
        # conversation drawer can push it aside instead of covering it. The
        # header stays full width on purpose: its sidebar toggle is the only
        # way to close the drawer again, and it must never end up underneath it.
        self._body = QtWidgets.QWidget(self)
        self._body_layout = QtWidgets.QVBoxLayout(self._body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(0)
        self._body_layout.addWidget(self._notice)
        self._body_layout.addWidget(self._consent)
        self._body_layout.addWidget(self._pages, 1)
        self._body_layout.addWidget(self._blocking)
        self._body_layout.addWidget(self._composer)

        layout.addWidget(self._header)
        layout.addWidget(self._body, 1)

        self._conversations = ConversationDrawer(self)
        self._conversations.open_state_changed.connect(self._on_drawer_state_changed)

        # The panel forwards focus to the composer: Houdini activates the pane
        # tab and grants focus to the panel widget, not to anything inside it.
        # Without this that focus lands nowhere and typing needs another click.
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setFocusProxy(self._composer)

        self._header.manage_agents_clicked.connect(self._open_agent_management)
        self._header.sign_in_clicked.connect(self._offer_sign_in)
        self._header.agent_selected.connect(self._on_agent_chosen)
        self._header.conversations_clicked.connect(self._toggle_conversations)
        self._header.new_session_clicked.connect(self._start_new_session)
        self._header.settings_clicked.connect(lambda: self._show_page(self.PAGE_SETTINGS))
        self._conversations.new_session_clicked.connect(self._start_new_session)
        self._conversations.session_selected.connect(self._pool.set_current)
        self._conversations.session_renamed.connect(self._on_session_renamed)
        self._conversations.session_removed.connect(self._on_session_removed)

        self._composer.submitted.connect(self._on_submitted)
        self._composer.cancelled.connect(self._on_cancelled)
        self._composer.mode_selected.connect(self._on_mode_selected)
        self._composer.config_option_selected.connect(self._on_config_option_selected)
        self._composer.attachment_rejected.connect(self._note)
        self._composer.buddy_selected.connect(self._on_buddy_selected)

        self._notice.action_clicked.connect(self._on_notice_action)
        self._notice.dismissed.connect(self._on_notice_dismissed)
        self._blocking.action_clicked.connect(self._on_blocking_action)
        self._consent.answered.connect(self._on_telemetry_answer)

    def _make_settings_view(self) -> QtWidgets.QWidget:
        from .settings_view import SettingsView

        view = SettingsView(self, before_install=self._before_agent_install)
        self._settings_view = view
        view.changed.connect(self._on_settings_changed)
        view.closed.connect(lambda: self._show_page(self.PAGE_TRANSCRIPT))
        view.install_succeeded.connect(self._on_agent_install_succeeded)
        view.install_failed.connect(self._on_agent_install_failed)
        return view

    def _make_auth_view(self) -> QtWidgets.QWidget:
        from .auth_view import AuthView

        view = AuthView(self)
        self._auth_view = view
        # Through its own methods, not straight into shared_client().authenticate:
        # a direct subscription would permanently capture whichever client
        # instance existed when the widget was built. The client gets
        # recreated on an agent switch, and the login buttons would silently
        # start talking to a corpse.
        view.method_chosen.connect(self._on_auth_method_chosen)
        view.logout_requested.connect(self._on_logout_requested)
        return view

    def _show_page(self, index: int) -> None:
        self._pages.setCurrentIndex(index)
        # Writing to the agent from the settings or auth screen is pointless:
        # the reply lands in a feed the human can't see right now.
        self._composer.setVisible(index == self.PAGE_TRANSCRIPT)
        # The permission popover is a free-floating child of the panel, not
        # part of the page stack — without this it kept hovering over the
        # settings form, anchored to a composer that isn't even on screen.
        self._sync_permission_popover()

    def _open_agent_management(self) -> None:
        """Send the human to the agents section of settings.

        Every path that used to land on the standalone "Agents" screen —
        first launch with no agent picked, a failed launch — lands here
        instead now. There is no PAGE_AGENTS any more; the agents block
        lives at the top of settings (`ui/settings_view.py`), so opening it
        is just switching pages plus scrolling to the top.
        """
        self._show_page(self.PAGE_SETTINGS)
        self._settings_view.focus_agents()

    # --------------------------------------------------------------- boot

    def _boot(self) -> None:
        self._restore_conversations()
        self._header.set_cwd(scene.hip_dir())
        self._refresh_agent_chip_menu()
        self._refresh_worker = _RefreshWorker(self._settings, self)
        self._refresh_worker.done.connect(self._on_refresh_done)
        self._refresh_worker.start()
        self._ask_telemetry_consent_once()

        client = shared_client()
        if client.is_running():
            # We're a second tab: the connection is already up, show what's there.
            self._adopt_running_client()
            return

        agent_id = self._settings.default_agent
        if not agent_id:
            self._open_agent_management()
            return
        if not self._settings.autostart_agent:
            self._note('No agent running. Press "+" to start a conversation.')
            return
        self._start_agent(agent_id)

    def _adopt_running_client(self) -> None:
        info = shared_client().agent_info()
        if info is not None:
            # The artist's name for it, not the npm package from `initialize`.
            # This path skips `_start_agent`, so nothing had set the pending
            # label and the chip fell back to "@agentclientprotocol/…".
            self._pending_agent_label = self._display_label(self._settings.default_agent or "")
            self._header.set_agent(
                self._pending_agent_label
                or self._display_label(self._settings.default_agent or "")
                or info.name,
                None,
            )
            self._header.set_can_sign_in(bool(info.auth_methods))
            self._composer.set_capabilities(info, self._settings.whisper_endpoint)
        self._refresh_sessions()
        current = self._pool.current()
        if current is None:
            self._start_new_session()
        elif current.session_id.startswith(_RESTORED_PREFIX):
            # Same reasoning as in `_on_connected`: a transcript off disk
            # has no session under it, so it has no modes and no model.
            self._adopting_restored = current.session_id
            self._start_new_session()
        else:
            self._show_session(current.session_id)
        self._show_page(self.PAGE_TRANSCRIPT)

    def _start_agent(self, agent_id: str) -> None:
        """Start an agent without freezing Houdini.

        Preparing the spec means a registry round trip and, if no system
        Node is found, downloading the portable one (44 MB). GUI Houdini on
        macOS doesn't inherit the shell's PATH, so it never sees a homebrew
        node, and a download on first launch is the typical case, not a rare
        one. All of this used to run on the main thread, and Houdini hung
        dead for the whole download — to an artist that's indistinguishable
        from "the panel doesn't work." Now the prep happens in the
        background, the panel stays alive and reports what's going on.
        """
        if self._launch_worker is not None and self._launch_worker.isRunning():
            return  # already preparing — a repeat click must not pile up downloads
        self._pending_agent_label = self._display_label(agent_id)
        # Update the chip immediately, not after `connected`: switching agents
        # is the moment the artist most wants confirmation that the click
        # landed, and a failed launch used to leave the chip naming the
        # previous agent — which reads as "nothing happened".
        self._header.set_agent(self._pending_agent_label, None)
        self._note(f"Preparing {self._pending_agent_label}…")
        worker = _LaunchPrepWorker(agent_id, self._settings, self)
        worker.note.connect(self._note)
        worker.ready.connect(self._on_launch_ready)
        worker.prep_failed.connect(self._on_launch_prep_failed)
        self._launch_worker = worker
        worker.start()

    def _on_launch_ready(self, spec: Any, label: str) -> None:
        self._launch_worker = None
        if label:
            self._pending_agent_label = label
        self._note(f"Launching {self._pending_agent_label}…")
        shared_client().start(spec, cwd=scene.hip_dir())

    def _on_launch_prep_failed(self, message: str) -> None:
        self._launch_worker = None
        self._note(message)
        self._open_agent_management()

    def _display_label(self, agent_id: str) -> str:
        """Human-readable agent name for the chip and the feed.

        The artist picks "Claude Agent", not "@agentclientprotocol/
        claude-agent-acp" — package identifiers stay in the logs. This never
        hits the network: the name is needed instantly, on the main thread,
        so the registry is only read from whatever has already arrived, and
        the featured six get static names for when the registry hasn't
        landed yet.
        """
        for custom in self._settings.custom_agents:
            if custom.id == agent_id:
                return custom.name or agent_id
        for entry in self._registry_entries:
            if entry.id == agent_id:
                return entry.name
        return _FALLBACK_LABELS.get(agent_id, agent_id)

    def _refresh_agent_chip_menu(self) -> None:
        """Feed the header chip whatever is installed right now.

        Order is install order — registry agents in `installed_agents` dict
        order, then custom agents — deterministic, and it matches how the
        agents section lists them.
        """
        ids = list(self._settings.installed_agents) + [c.id for c in self._settings.custom_agents]
        items = [(agent_id, self._display_label(agent_id)) for agent_id in ids]
        self._header.set_agent_menu(items, self._settings.default_agent)

    # ------------------------------------------------------------- client

    def _wire_client(self) -> None:
        """Subscribe to the shared client, remembering EVERY signal-slot pair.

        We remember pairs specifically because the client is shared across
        tabs: a bare ``signal.disconnect()`` when one tab closes would also
        disconnect its neighbor, which would stop getting the agent's
        replies while still looking alive.
        """
        client = shared_client()
        wiring = (
            (client.connected, self._on_connected),
            (client.disconnected, self._on_disconnected),
            (client.failed, self._on_failed),
            (client.auth_required, self._on_auth_required),
            (client.log_line, self._on_log_line),
            (client.authenticated, self._on_authenticated),
            (client.session_started, self._on_session_started),
            (client.modes_changed, self._on_modes_changed),
            (client.commands_changed, self._on_commands_changed),
            (client.config_options_changed, self._on_config_options_changed),
            (client.message_chunk, self._on_message_chunk),
            (client.thought_chunk, self._on_thought_chunk),
            (client.tool_call, self._on_tool_call),
            (client.tool_call_update, self._on_tool_call_update),
            (client.plan_changed, self._on_plan_changed),
            (client.usage_changed, self._on_usage_changed),
            (client.turn_finished, self._on_turn_finished),
            (client.error, self._on_error),
            (client.permission_requested, self._on_permission_requested),
        )
        for signal, slot in wiring:
            signal.connect(slot)
        self._client_wiring = wiring

    def _on_connected(self, info: Any) -> None:
        # The chip shows the name the artist picked, not the npm package
        # name from initialize ("@agentclientprotocol/claude-agent-acp").
        self._header.set_agent(self._pending_agent_label or info.name, None)
        self._header.set_can_sign_in(bool(info.auth_methods))
        self._composer.set_capabilities(info, self._settings.whisper_endpoint)
        self._show_page(self.PAGE_TRANSCRIPT)
        current = self._pool.current()
        if current is None:
            self._start_new_session()
        elif current.session_id.startswith(_RESTORED_PREFIX):
            # What's on screen was read back from disk and has no agent
            # behind it. Waiting for the artist's first message before
            # opening a session looked harmless, but modes, slash commands
            # and the model picker all arrive with `session/new` — so until
            # they typed, the panel showed a conversation with no controls
            # under it. Adopt the restored transcript into a live session
            # right away; `_on_session_started` carries the words over.
            self._adopting_restored = current.session_id
            self._start_new_session()

    def _on_disconnected(self, reason: str) -> None:
        self._pending_permissions.clear()
        self._permission_views.clear()
        self._hide_permission_popover()
        # Clear busy on EVERY session, not just the visible one. A turn that
        # was in flight when the agent went away can never finish, and a
        # session left marked busy comes back that way on the next switch —
        # with a send button stuck as a stop button that quietly does nothing
        # when pressed.
        for state in self._pool.all():
            state.busy = False
        self._composer.set_busy(False)
        current = self._pool.current()
        if current is not None:
            self._finish_activity(current.session_id)
        self._composer.set_capabilities(None, self._settings.whisper_endpoint)
        self._note(f"Agent disconnected: {reason}" if reason else "Agent stopped.")

    def _on_failed(self, message: str) -> None:
        self._note(f"Agent failed to start: {message}")
        self._open_agent_management()

    def _on_auth_required(self, methods: list) -> None:
        info = shared_client().agent_info()
        self._auth_view.set_methods(
            methods, can_logout=bool(info and info.supports_logout)
        )
        self._show_page(self.PAGE_AUTH)

    def _on_session_started(self, session_id: str, state: Any) -> None:
        adopted = self._adopting_restored
        self._adopting_restored = None
        if adopted is not None:
            # Move the restored conversation onto the session the agent just
            # opened: same words, same id on disk, a live transport at last.
            old_model = self._models.pop(adopted, None)
            if old_model is not None:
                self._models[session_id] = old_model
            conversation_id = self._conversation_ids.pop(adopted, None)
            if conversation_id is not None:
                self._conversation_ids[session_id] = conversation_id
            restored_state = self._pool.get(adopted)
            if restored_state is not None:
                state.title = restored_state.title
                self._pool.remove(adopted)
        import uuid as _uuid

        self._conversation_ids.setdefault(session_id, _uuid.uuid4().hex)
        # A brand new session cannot be mid-turn. Saying so explicitly keeps a
        # stale flag from a previous agent out of a fresh conversation.
        state.busy = False
        self._models.setdefault(session_id, TranscriptModel())
        self._pool.add(state)
        self._pool.set_current(session_id)
        self._show_session(session_id)
        self._show_page(self.PAGE_TRANSCRIPT)
        pending, self._pending_prompt = self._pending_prompt, None
        if pending:
            self._on_submitted(pending)

    def _on_modes_changed(self, session_id: str, mode_state: Any) -> None:
        state = self._pool.get(session_id)
        if state is not None:
            state.available_modes = list(getattr(mode_state, "available_modes", []) or [])
            state.current_mode_id = getattr(mode_state, "current_mode_id", None)
            self._pool.mark_changed(session_id)
        if self._is_current(session_id):
            self._composer.set_modes(state.available_modes, state.current_mode_id)

    def _on_commands_changed(self, session_id: str, commands: list) -> None:
        state = self._pool.get(session_id)
        if state is not None:
            state.available_commands = list(commands)
            self._pool.mark_changed(session_id)
        if self._is_current(session_id):
            self._composer.set_commands(list(commands))

    def _on_config_options_changed(self, session_id: str, options: list) -> None:
        """The model picker, and everything else the agent lets us change.

        ACP has no dedicated "model" concept: agents expose model, reasoning
        effort and fast mode as session config options (`configOptions` in
        the `session/new` reply, refreshed by `config_option_update`). The
        client has been reading them all along and nobody listened, so the
        chip stayed hidden and the panel looked like it had no model choice
        at all.

        Nothing here is invented: the options, their labels, their order and
        the current value are the agent's word. An agent that offers none
        gets no chips, per the panel's standing rule.
        """
        state = self._pool.get(session_id)
        if state is not None:
            state.config_options = list(options)
            self._pool.mark_changed(session_id)
        if self._is_current(session_id):
            self._composer.set_config_options(list(options))

    def _on_config_option_selected(self, config_id: str, value: str) -> None:
        """Record the pick right away — same reasoning as `_on_mode_selected`.

        `state.config_options` otherwise only moves when the agent sends its
        own `config_option_update`, which coming back to this session later
        would overwrite with whatever the value was before the pick.
        """
        current = self._pool.current()
        if current is not None:
            current.config_options = [
                replace(option, current_value=value) if option.id == config_id else option
                for option in current.config_options
            ]
            self._pool.mark_changed(current.session_id)
            shared_client().set_config_option(current.session_id, config_id, value)

    def _on_message_chunk(self, session_id: str, message_id: str, text: str) -> None:
        entry = self._model(session_id).apply_chunk(message_id, text)
        self._touch(session_id, entry.id)

    def _on_thought_chunk(self, session_id: str, message_id: str, text: str) -> None:
        entry = self._model(session_id).apply_chunk(message_id, text, thought=True)
        self._touch(session_id, entry.id)

    def _on_tool_call(self, session_id: str, call: Any) -> None:
        entry = self._model(session_id).apply_tool_call(call)
        self._touch(session_id, entry.id)
        if self._is_current(session_id):
            self._transcript.reset_thinking_after_tool()

    def _on_tool_call_update(self, session_id: str, update: Any) -> None:
        entry = self._model(session_id).apply_tool_update(update)
        if entry is not None:
            self._touch(session_id, entry.id)

    def _on_plan_changed(self, session_id: str, entries: list) -> None:
        entry = self._model(session_id).apply_plan(entries)
        self._touch(session_id, entry.id)

    def _on_usage_changed(self, session_id: str, usage: Any) -> None:
        state = self._pool.get(session_id)
        if state is not None:
            state.usage = usage
        if self._is_current(session_id):
            self._composer.set_usage(usage)

    def _on_turn_finished(self, session_id: str, stop_reason: str) -> None:
        state = self._pool.get(session_id)
        if state is not None:
            state.busy = False
        if self._is_current(session_id):
            self._composer.set_busy(False)
        self._finish_activity(session_id)
        if stop_reason and stop_reason not in ("end_turn", "cancelled"):
            entry = self._model(session_id).append_error(f"Agent stopped: {stop_reason}")
            self._touch(session_id, entry.id)

    def _on_error(self, session_id: str, message: str) -> None:
        # A failure while the artist is on the sign-in screen has to appear
        # THERE. Reporting it into a feed they cannot see is the same as not
        # reporting it: the screen just sits, which is indistinguishable from
        # a login that quietly did nothing.
        if self._pages.currentIndex() == self.PAGE_AUTH:
            self._auth_view.show_error(message, self._last_auth_method)
            return
        target = session_id or (self._pool.current().session_id if self._pool.current() else "")
        if not target:
            self._note(message)
            return
        entry = self._model(target).append_error(message)
        self._touch(target, entry.id)
        if self._is_current(target):
            self._composer.set_busy(False)
        self._finish_activity(target)

    def _on_permission_requested(
        self, request_key: str, session_id: str, tool_call: Any, options: list
    ) -> None:
        view = PermissionView(
            request_key=request_key,
            tool_title=getattr(tool_call, "title", "") or "Agent action",
            options=[
                (option.option_id, option.name, option.kind) for option in options
            ],
        )
        self._pending_permissions[request_key] = session_id
        self._permission_views[request_key] = view
        entry = self._model(session_id).apply_permission(view)
        self._touch(session_id, entry.id)
        if self._is_current(session_id):
            self._sync_permission_popover()

    def _on_permission_answered(self, request_key: str, option_id: str) -> None:
        session_id = self._pending_permissions.pop(request_key, "")
        self._permission_views.pop(request_key, None)
        self._hide_permission_popover()
        shared_client().answer_permission(request_key, option_id or None)
        if session_id:
            entry = self._model(session_id).resolve_permission(request_key, option_id or None)
            if entry is not None:
                self._touch(session_id, entry.id)
        self._sync_permission_popover()

    # ------------------------------------------------------------ sessions

    def _wire_pool(self) -> None:
        """Subscribe to the session pool, and REMEMBER the subscriptions.

        The pool is a process-wide singleton, so a connection into it
        outlives the tab that made it. These were fire-and-forget, and a
        lambda holding `self` kept every panel ever opened alive with its
        whole widget tree — closing a tab freed nothing. On a desktop where
        panels get opened and closed through the day that shows up as
        hundreds of stray empty windows, which is exactly how it was
        reported.
        """
        refresh = lambda _sid: self._refresh_sessions()  # noqa: E731 - one slot, three signals
        self._pool_wiring = (
            (self._pool.added, refresh),
            (self._pool.removed, refresh),
            (self._pool.changed, refresh),
            (self._pool.current_changed, self._show_session),
        )
        for signal, slot in self._pool_wiring:
            signal.connect(slot)

    def _refresh_sessions(self) -> None:
        current = self._pool.current()
        self._conversations.set_sessions(
            self._pool.all(), current.session_id if current else None
        )

    def _show_session(self, session_id: str) -> None:
        state = self._pool.get(session_id)
        self._transcript.set_model(self._model(session_id))
        self._transcript.refresh(None)
        if state is not None:
            self._composer.set_busy(state.busy)
            self._composer.set_usage(state.usage)
            self._composer.set_commands(list(state.available_commands))
            self._composer.set_modes(state.available_modes, state.current_mode_id)
            self._composer.set_config_options(list(state.config_options))
            # Being open IS being read — the sidebar's unread dot (design.md,
            # "the sidebar") only ever means "something arrived while this
            # wasn't the visible conversation."
            state.unread = False
        self._sync_permission_popover()
        self._refresh_sessions()

    def _sync_permission_popover(self) -> None:
        if self._pages.currentIndex() != self.PAGE_TRANSCRIPT:
            # It anchors to the composer, which is hidden on every other
            # page. The pending request isn't lost — it comes back the moment
            # the artist returns to the conversation.
            self._hide_permission_popover()
            return
        current = self._pool.current()
        session_id = current.session_id if current is not None else ""
        view = next(
            (
                candidate
                for key, candidate in self._permission_views.items()
                if self._pending_permissions.get(key) == session_id and candidate.answered is None
            ),
            None,
        )
        if view is None:
            self._hide_permission_popover()
            return
        if (
            self._permission_popover is not None
            and self._permission_popover.request_key() == view.request_key
        ):
            self._position_permission_popover()
            return
        self._hide_permission_popover()
        popover = PermissionRow(view, self)
        popover.answered.connect(self._on_permission_answered)
        self._permission_popover = popover
        popover.show()
        popover.raise_()
        self._position_permission_popover()

    def _hide_permission_popover(self) -> None:
        popover = self._permission_popover
        self._permission_popover = None
        if popover is not None:
            popover.hide()
            popover.deleteLater()

    def _position_permission_popover(self) -> None:
        popover = self._permission_popover
        if popover is None:
            return
        anchor = self._composer.popover_anchor_rect(self)
        # Widened together with `PermissionRow`'s own bounds (320–480): four
        # real options ("Allow once/always", "Reject once/always") need more
        # than the original 280–400 gave them.
        width = min(480, max(320, anchor.width() - 64))
        popover.setFixedWidth(width)
        popover.adjustSize()
        x = anchor.center().x() - width // 2
        x = max(8, min(x, self.width() - width - 8))
        y = max(self._header.height() + 8, anchor.top() - popover.height() - 10)
        popover.move(x, y)
        popover.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._sync_drawer_geometry()
        self._position_permission_popover()

    def _toggle_conversations(self) -> None:
        # Geometry first: the drawer's top inset is the header's height, and
        # the header only knows it after a layout pass — which may not have
        # happened by the time the button is first clicked.
        self._sync_drawer_geometry()
        self._conversations.toggle()

    def _sync_drawer_geometry(self) -> None:
        self._conversations.set_top_inset(self._header.height())
        self._conversations.sync_parent_geometry()
        self._apply_drawer_inset()
        if self._conversations.isVisible():
            self._conversations.raise_()

    def _on_drawer_state_changed(self, _open: bool) -> None:
        self._apply_drawer_inset()
        self._position_permission_popover()

    def _apply_drawer_inset(self) -> None:
        """Reserve the drawer's column so it never covers the conversation.

        Only while there is room left: below `_MIN_BODY_WIDTH` the panel is
        narrower than a drawer plus anything readable, so the drawer goes
        back to overlaying — the same thing every responsive sidebar does,
        and better than squeezing the feed into a hundred pixels.
        """
        drawer = self._conversations
        inset = 0
        if drawer.is_open() and self.width() - drawer.width() >= _MIN_BODY_WIDTH:
            inset = drawer.width()
        margins = self._body_layout.contentsMargins()
        if margins.left() != inset:
            self._body_layout.setContentsMargins(
                inset, margins.top(), margins.right(), margins.bottom()
            )

    def _start_new_session(self) -> None:
        """Ask the agent for another session — and never do it silently.

        `session/new` is a round trip to someone else's process, and for the
        agents we ship it also spawns an MCP server. When that takes a while,
        or never answers at all, the panel used to show absolutely nothing:
        the artist clicks "+", nothing appears, and the only available
        conclusion is that the button is broken. So the request is announced
        when it goes out and chased up if the answer never comes.
        """
        client = shared_client()
        if not client.is_running():
            agent_id = self._settings.default_agent
            if agent_id:
                self._start_agent(agent_id)
            else:
                self._open_agent_management()
            return
        before = {state.session_id for state in self._pool.all()}
        client.new_session(cwd=scene.hip_dir(), mcp_servers=scene.mcp_servers())
        QtCore.QTimer.singleShot(
            _NEW_SESSION_GRACE_MS, lambda: self._report_stalled_new_session(before)
        )

    def _report_stalled_new_session(self, before: set) -> None:
        if self._closed:
            return
        if {state.session_id for state in self._pool.all()} - before:
            return  # the agent answered, nothing to complain about
        self._note(
            "The agent hasn't opened a new conversation. It may be busy or "
            "stuck — try switching agents in the header, or restart it from "
            "settings."
        )

    def _on_session_renamed(self, session_id: str, title: str) -> None:
        state = self._pool.get(session_id)
        if state is not None:
            state.title = title
            self._pool.mark_changed(session_id)

    def _on_session_removed(self, session_id: str) -> None:
        # Deleting a conversation must hand its session back. With Claude a
        # session is a whole agent-SDK process running the user's entire MCP
        # fleet; leaving it behind meant a Houdini that had been open for an
        # afternoon carried a dozen of them, hundreds of megabytes, for
        # conversations the artist had already thrown away.
        self._release_session(session_id)
        self._pool.remove(session_id)
        if self._pool.current() is None:
            # Deleted the last conversation — an artist who just cleared the
            # drawer should land somewhere usable, not on an empty feed with
            # no session to prompt.
            self._start_new_session()

    def _release_session(self, session_id: str) -> None:
        """Give a session back to the agent, if it is a real one.

        Restored conversations carry OUR id, not the agent's — there is
        nothing on the far side to close, and asking would be a lie about
        what exists.
        """
        if not session_id or session_id.startswith(_RESTORED_PREFIX):
            return
        shared_client().close_session(session_id)

    def _model(self, session_id: str) -> TranscriptModel:
        return self._models.setdefault(session_id, TranscriptModel())

    def _is_current(self, session_id: str) -> bool:
        current = self._pool.current()
        return current is not None and current.session_id == session_id

    def _touch(self, session_id: str, entry_id: str) -> None:
        """Redraw a single entry — only if the human is looking at this session.

        Otherwise streaming into a background session would make Qt reflow a
        feed nobody is watching. What DOES happen for a background session is
        the sidebar's unread dot (design.md, "the sidebar"): flipped once, on
        the first touch after it goes quiet, not on every following chunk —
        one `changed` signal per conversation going stale beats rebuilding
        the drawer for every streamed token.
        """
        if self._is_current(session_id):
            self._transcript.refresh(entry_id)
            return
        state = self._pool.get(session_id)
        if state is not None and not state.unread:
            state.unread = True
            self._pool.mark_changed(session_id)

    # ------------------------------------------------------------- input

    def _on_submitted(self, blocks: list) -> None:
        current = self._pool.current()
        if current is not None and current.session_id.startswith(_RESTORED_PREFIX):
            # A conversation read back from disk has no agent behind it. Keep
            # the words, open a real session, and let `_on_session_started`
            # carry the transcript over — throwing the message away because
            # the transport is not up yet would be the artist's loss, not
            # ours.
            self._adopting_restored = current.session_id
            self._pending_prompt = list(blocks)
            self._start_new_session()
            return
        if current is None:
            # The composer has already cleared itself by now, so dropping the
            # blocks here would silently eat what the artist just typed — the
            # first message after opening the panel, most often. Hold it and
            # send it the moment a session exists.
            self._pending_prompt = list(blocks)
            self._note("No conversation open yet — starting one and sending this.")
            self._start_new_session()
            return
        text = " ".join(
            block.get("text", "") for block in blocks if block.get("type") == "text"
        ).strip()
        if text:
            entry = self._model(current.session_id).append_user(text)
            self._touch(current.session_id, entry.id)
            # `client.py.do_new_session` seeds every fresh session with the
            # English "New conversation" — this used to compare against the
            # old Russian default and never matched, so live conversations
            # never got a real name until this fix.
            if current.title in ("", "New conversation"):
                current.title = summarize_title(text)
                self._pool.mark_changed(current.session_id)
        current.busy = True
        self._composer.set_busy(True)
        activity = self._model(current.session_id).start_activity()
        self._touch(current.session_id, activity.id)
        self._composer.trigger_buddy()
        shared_client().prompt(current.session_id, blocks)

    def _finish_activity(self, session_id: str) -> None:
        activity = self._model(session_id).finish_activity()
        if activity is not None:
            self._touch(session_id, activity.id)

    def _on_cancelled(self) -> None:
        """Stop the current turn — and never leave the artist stuck.

        `session/cancel` is a notification: the agent may answer it with a
        `turn_finished`, or may ignore it entirely (a dead session, a wedged
        process). Waiting forever for an acknowledgement that may never come
        is how the panel ends up with a stop button that does nothing, so
        after a short grace period we release the input ourselves and say so.
        """
        current = self._pool.current()
        if current is None:
            self._composer.set_busy(False)
            return
        shared_client().cancel(current.session_id)
        session_id = current.session_id
        QtCore.QTimer.singleShot(
            _CANCEL_GRACE_MS, lambda: self._release_if_still_busy(session_id)
        )

    def _release_if_still_busy(self, session_id: str) -> None:
        state = self._pool.get(session_id)
        if state is None or not state.busy:
            return
        state.busy = False
        if self._is_current(session_id):
            self._composer.set_busy(False)
        entry = self._model(session_id).append_error(
            "The agent did not acknowledge the stop. Input is unlocked; "
            "start a new conversation if it stays unresponsive."
        )
        self._touch(session_id, entry.id)

    def _on_mode_selected(self, mode_id: str) -> None:
        """Record the pick right away, not only once the agent echoes it back.

        `session/set_mode` is a one-way call — `SessionState.current_mode_id`
        used to update ONLY from the agent's own `current_mode_update`
        (`_on_modes_changed`). An agent slow to send that update, or an
        artist switching to another conversation before it arrives, left the
        stored id stale; coming back to this session then had `_show_session`
        push the STALE value back into the mode chip, quietly overwriting
        "Plan" with whatever it was when the session was created. Setting it
        here as well means the pick survives even if the echo never comes.
        """
        current = self._pool.current()
        if current is not None:
            current.current_mode_id = mode_id
            self._pool.mark_changed(current.session_id)
            shared_client().set_mode(current.session_id, mode_id)

    def _on_buddy_selected(self, buddy: str) -> None:
        self._settings.buddy = buddy
        settings_mod.save(self._settings)

    # ------------------------------------------------- announcements and updates

    def _on_refresh_done(self, result: Any, entries: Any = ()) -> None:
        # The agents section doesn't hit the network itself: its
        # `refresh_from_registry` is synchronous, so calling it from the
        # main thread would freeze Houdini for the length of a network
        # timeout when there's no network. Entries arrive already fetched.
        if entries:
            self._registry_entries = list(entries)
        self._refresh_agent_chip_menu()
        settings_view = getattr(self, "_settings_view", None)
        if settings_view is not None and entries:
            from .. import registry

            # featured(), not the whole registry: that's pushing forty-odd
            # entries at the artist, drowning the choice they can't make
            # sense of. Anything else goes through "custom agent".
            settings_view.set_agents(
                registry.featured(entries),
                updates=list(getattr(result, "updates", []) or []),
            )

        for announcement in getattr(result, "announcements", []):
            if announcement.severity == "blocking":
                self._active_update = None
                self._blocking.show_notice(announcement)
                self._composer.block_input(announcement.title)
                return
            self._active_update = None
            self._notice.show_notice(announcement)
            return
        for update in getattr(result, "updates", []):
            self._active_update = update
            self._notice.show_update(update)
            return

    def _on_notice_action(self, identifier: str, url: str) -> None:
        # The strip's "Update" button fires this SAME signal (see
        # `NoticeStrip`'s own docstring) — `identifier` is `Update.target`
        # then, not an announcement id, and there's no `url` to open.
        update = self._active_update
        if update is not None and update.target == identifier:
            self._start_update(update)
            return
        self._open_url(url)
        self._remember_seen(identifier)

    def _on_notice_dismissed(self, identifier: str) -> None:
        if self._active_update is not None and self._active_update.target == identifier:
            self._active_update = None
            return  # an update dismissal isn't an announcement id — nothing to remember
        self._remember_seen(identifier)

    def _start_update(self, update: Any) -> None:
        """The notice strip's "Update" button, actually doing something.

        Only agents update through this panel: `update.kind` "panel"/"fx"
        names a package on PyPI, and this process can't safely replace the
        package it is currently running FROM out from under itself — that
        needs the artist to run pip and restart Houdini, the same as the
        first install (docs/design.md, "Installation"). Pretending to do it
        in place would be the more dangerous silence, not the honest one.
        """
        if update.kind != "agent":
            self._note(
                f"Update {update.target} yourself: pip install --upgrade {update.target}, "
                "then restart Houdini."
            )
            return
        self._show_page(self.PAGE_SETTINGS)
        self._settings_view.focus_agents()
        if not self._settings_view.trigger_agent_update(update.target):
            self._note(f"Could not find {update.label} to update — try Settings → Agents.")

    def _before_agent_install(self, agent_id: str) -> None:
        """About to overwrite `agent_id`'s files on disk (install OR update,
        from the settings row or from the notice banner — this fires either
        way, see `AgentsView.__init__`). If it's the agent currently
        running, its files are what the live process is reading from RIGHT
        NOW — swapping them under it is not something this panel does
        silently. Stopping it here, not just asking the artist to: they
        already said "update" once, and a second manual step for something
        the panel can safely do itself is the friction this project's UI
        rule exists to cut. `_on_agent_install_succeeded` brings it back up.
        """
        client = shared_client()
        if client.is_running() and self._settings.default_agent == agent_id:
            self._restart_after_update = agent_id
            self._note(
                f"Stopping {self._display_label(agent_id)} to update it — "
                "it restarts automatically once the update finishes."
            )
            client.stop()

    def _on_agent_install_succeeded(self, agent_id: str) -> None:
        if self._active_update is not None and self._active_update.target == agent_id:
            self._active_update = None
            self._notice.hide_notice()
        if self._restart_after_update == agent_id:
            self._restart_after_update = None
            self._note(f"{self._display_label(agent_id)} updated — restarting it…")
            self._start_agent(agent_id)

    def _on_agent_install_failed(self, agent_id: str, message: str) -> None:
        self._restart_after_update = None
        self._note(f"Could not update {self._display_label(agent_id)}: {message}")

    def _on_blocking_action(self, announcement_id: str, url: str) -> None:
        self._open_url(url)
        self._remember_seen(announcement_id)
        self._blocking.hide_notice()
        self._composer.unblock_input()

    def _open_url(self, url: str) -> None:
        if not url:
            return
        from .qt import QtGui

        QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))

    def _remember_seen(self, announcement_id: str) -> None:
        if not announcement_id:
            return
        # Same principle as in _on_agent_chosen: reload before writing, or
        # the panel's own snapshot would overwrite what other screens have
        # already saved.
        self._settings = settings_mod.load()
        if announcement_id not in self._settings.seen_announcements:
            self._settings.seen_announcements.append(announcement_id)
            settings_mod.save(self._settings)

    #: Agent stderr lines that the artist has to see. Agents are not obliged
    #: to report everything through the protocol — Grok, for one, creates a
    #: session happily and only writes "AuthorizationRequired" to stderr, so
    #: a panel that watched the protocol alone showed a working conversation
    #: that answered nothing.
    _FATAL_STDERR_MARKERS = ("authorizationrequired", "fatal", "error")

    def _on_log_line(self, line: str) -> None:
        lowered = line.lower()
        if not any(marker in lowered for marker in self._FATAL_STDERR_MARKERS):
            return
        if "authorizationrequired" in lowered.replace(" ", ""):
            self._note(
                "The agent says it is not signed in. Open the ⋯ menu and pick Sign in."
            )
            self._offer_sign_in()
            return
        # Trimmed: agents put timestamps and ANSI colour in stderr, and the
        # useful part is the tail.
        self._note(f"Agent: {line.strip()[-200:]}")

    def _offer_sign_in(self) -> None:
        """Show the sign-in screen using the methods `initialize` gave us.

        Needed because `auth_required` is not guaranteed: an agent may accept
        a session and fail authorization later, out of band. The methods are
        known from `initialize` either way, so signing in never has to depend
        on the agent asking first.
        """
        info = shared_client().agent_info()
        if info is None or not info.auth_methods:
            return
        if self._pages.currentIndex() == self.PAGE_AUTH:
            return
        self._auth_view.set_methods(
            list(info.auth_methods), can_logout=bool(info.supports_logout)
        )
        self._show_page(self.PAGE_AUTH)

    def _on_auth_method_chosen(self, method_id: str) -> None:
        self._last_auth_method = method_id
        self._note(f"Signing in with {method_id}… finish it in the browser if it opens.")
        shared_client().authenticate(method_id)

    def _on_authenticated(self, method_id: str) -> None:
        """Sign-in worked — get out of the way and open a conversation.

        Leaving the artist on the sign-in screen after a successful sign-in
        was the bug: they approved it in the browser and the panel gave no
        sign it had noticed, so it looked like the login had failed.
        """
        self._note("Signed in.")
        self._show_page(self.PAGE_TRANSCRIPT)
        if self._pool.current() is None:
            self._start_new_session()

    def _on_logout_requested(self) -> None:
        """Logging out sends the panel back where sign-in came from.

        After a successful logout, the client raises `auth_required` with
        the same methods that came from `initialize` — the sign-in screen
        shows up on its own, no separate branch needed here. If the agent
        couldn't log out, an `error` arrives instead and the human stays
        put: silently pretending the logout happened isn't an option.
        """
        shared_client().logout()

    def _ask_telemetry_consent_once(self) -> None:
        """Ask about telemetry exactly once, ever.

        The "asked" flag is written regardless of the answer — otherwise
        someone who said no once would get the same question every time
        they open the panel, and that's not a question any more, it's
        nagging.
        """
        if self._settings.telemetry_consent_asked:
            return
        self._consent.ask(
            "Allow sending anonymous usage stats? Only panel, agent, and OS "
            "versions, plus the fact something crashed. Never scenes, "
            "prompts, or paths."
        )

    def _on_telemetry_answer(self, allowed: bool) -> None:
        self._settings = settings_mod.load()
        self._settings.telemetry = bool(allowed)
        self._settings.telemetry_consent_asked = True
        settings_mod.save(self._settings)

    def _on_settings_changed(self) -> None:
        self._settings = settings_mod.load()
        info = shared_client().agent_info()
        self._composer.set_capabilities(info, self._settings.whisper_endpoint)
        self._refresh_agent_chip_menu()

    def _on_agent_chosen(self, agent_id: str) -> None:
        """Switch the running agent — called from the header chip's menu.

        Reload from disk BEFORE writing — mandatory. self._settings is a
        snapshot from when the panel opened, and the agents section writes a
        freshly-added custom agent straight to the file. Saving the stale
        snapshot on top used to erase that agent, and "add a custom agent →
        pick it right away" failed with "agent isn't in the registry or
        among custom agents" (found only by testing live, in both Houdini
        versions).
        """
        self._settings = settings_mod.load()
        self._settings.default_agent = agent_id
        settings_mod.save(self._settings)
        self._refresh_agent_chip_menu()
        client = shared_client()
        if client.is_running():
            client.stop()
        # A session id belongs to the process that issued it, so the binding
        # to the old agent must go. The CONVERSATION does not: it is what the
        # artist wrote and read, and wiping it on every agent switch was the
        # bug, not the feature. Transcripts are written to disk first, the
        # pool is emptied of dead ids, and the drawer keeps showing the same
        # list.
        #
        # Continuing with a different agent does not carry the model's memory
        # across — no protocol can do that. The transcript stays readable and
        # the new agent starts from what it is told.
        self._persist_conversations()
        for state in self._pool.all():
            self._release_session(state.session_id)
        self._pool.clear()
        self._pending_permissions.clear()
        # A message queued for the old agent has nowhere to land yet: the new
        # one has not opened a session. It is kept and sent once it does.
        self._start_agent(agent_id)

    # --- conversations that outlive the agent and Houdini -----------------

    def _persist_conversations(self) -> None:
        """Write every transcript we have to disk.

        Called whenever a conversation could be about to lose its live agent
        session: an agent switch, a panel closing. Failures are swallowed —
        losing history is bad, taking the panel down with it is worse.
        """
        try:
            from .. import conversations_store as store

            existing = {c.id: c for c in store.load()}
            for session_id, model in self._models.items():
                if session_id == "__idle__":
                    continue
                conversation_id = self._conversation_ids.get(session_id)
                if conversation_id is None:
                    continue
                records = model.to_records()
                if not records:
                    continue
                conversation = existing.get(conversation_id) or store.StoredConversation.new()
                conversation.id = conversation_id
                state = self._pool.get(session_id)
                if state is not None:
                    conversation.title = state.title
                    # The scene this conversation belongs to. Taken from the
                    # session rather than from `$HIP` right now, so saving
                    # after the artist opens a different scene doesn't drag
                    # the previous scene's conversations along with it.
                    conversation.cwd = state.cwd or scene.hip_dir()
                conversation.agent_id = self._settings.default_agent or ""
                conversation.entries = records
                conversation.updated_at = time.time()
                existing[conversation_id] = conversation
            current = self._pool.current()
            active_id = (
                self._conversation_ids.get(current.session_id) if current is not None else None
            )
            store.save(list(existing.values()), active_id=active_id)
        except Exception:  # noqa: BLE001 - history is never worth a crash
            pass

    def _restore_conversations(self) -> None:
        """Show what was written last time, before any agent is up.

        Read-only history: these transcripts have no live session behind
        them, and the first message the artist sends opens a fresh one.
        """
        try:
            from .. import conversations_store as store

            here = scene.hip_dir()
            stored = store.load(here)
            active_id = store.load_active_id()
            # Anything written before conversations were tied to a scene has
            # no scene to belong to, so it is not shown here. Saying so once
            # is the difference between "scoped" and "the panel ate my
            # history" — the file is untouched and still holds all of it.
            older = store.unscoped_count()
            if older:
                self._note(
                    f"{older} conversation(s) from before this version aren't tied to a "
                    f"scene and are hidden here. They are still in {store.store_path()}."
                )
        except Exception:  # noqa: BLE001
            return
        if not stored:
            return

        # Restored conversations enter the pool so the drawer can list them
        # and the artist can read them. They carry no live agent session —
        # the id below is ours, not any agent's — and the first message sent
        # into one opens a real session and takes the transcript with it.
        for conversation in stored:
            key = _RESTORED_PREFIX + conversation.id
            if self._pool.get(key) is not None:
                continue
            state = sessions.SessionState(
                session_id=key,
                title=conversation.title,
                cwd=scene.hip_dir(),
                created_at=conversation.created_at,
            )
            self._pool.add(state)
            self._conversation_ids[key] = conversation.id
            self._model(key).load_records(conversation.entries)

        ids = {c.id for c in stored}
        wanted = _RESTORED_PREFIX + (active_id if active_id in ids else stored[0].id)
        if self._pool.get(wanted) is not None:
            self._pool.set_current(wanted)
        self._restored = stored
        self._conversations.set_restored(stored) if hasattr(
            self._conversations, "set_restored"
        ) else None

    def _note(self, text: str) -> None:
        current = self._pool.current()
        session_id = current.session_id if current else "__idle__"
        entry = self._model(session_id).append_error(text)
        if current is None:
            self._transcript.set_model(self._model(session_id))
            self._transcript.refresh(None)
        else:
            self._touch(session_id, entry.id)

    # ---------------------------------------------------------- shutdown

    def shutdown(self) -> None:
        """Close THIS tab.

        The agent connection only goes down once the last tab closes: while
        any tab is still alive, the conversation must keep going. Otherwise
        an artist closing one of two panels would lose both.
        """
        if self._closed:
            return
        self._closed = True
        self._persist_conversations()
        _live_panels.discard(self)

        # Background threads are asked to stop, but a thread in the middle of
        # a network round trip does not stop on request — it stops when the
        # socket does. Whatever it does afterwards must not reach this panel:
        # its widgets are about to be deleted, and a signal arriving after
        # that is "RuntimeError: The SignalInstance object was already
        # deleted". Disconnecting first is what makes the wait a courtesy
        # rather than a race we have to win.
        worker = self._refresh_worker
        if worker is not None:
            with contextlib.suppress(RuntimeError, TypeError):
                worker.done.disconnect(self._on_refresh_done)
            worker.requestInterruption()
            worker.wait(2000)
            self._refresh_worker = None

        launch = self._launch_worker
        if launch is not None:
            for signal, slot in (
                (launch.ready, self._on_launch_ready),
                (launch.prep_failed, self._on_launch_prep_failed),
                (launch.note, self._note),
            ):
                with contextlib.suppress(RuntimeError, TypeError):
                    signal.disconnect(slot)
            launch.wait(3000)
            self._launch_worker = None

        for signal, slot in (
            *getattr(self, "_client_wiring", ()),
            *getattr(self, "_pool_wiring", ()),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._client_wiring = ()
        self._pool_wiring = ()

        if not _live_panels:
            global _shared_client
            if _shared_client is not None:
                _shared_client.stop()
                _shared_client = None

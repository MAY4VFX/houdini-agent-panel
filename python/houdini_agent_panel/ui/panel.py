"""Root widget of the panel — where everything comes together.

Three decisions here shape everything else.

**One agent process per agent id, many sessions.** The agent process and its
connection live in the module, not the widget: a second panel tab ON THE
SAME AGENT must see the same conversation, not spin up a second process.
Widgets come and go, the connection outlives them — but a tab on a
DIFFERENT agent gets its own process, not the same one repurposed (see
`AgentPanel._agent_id` — this used to be one shared connection for the
whole Houdini process, and switching one tab's agent silently pulled every
other tab's conversation and connection down with it).

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
from .boot_status import PHASE_CONNECTING, PHASE_LAUNCHING, PHASE_PREPARING, PHASE_SESSION
from .composer import Composer
from .conversations import ConversationDrawer, summarize_title
from .permissions import PermissionRow
from .qt import QtCore, QtWidgets, Signal
from .transcript import TranscriptView
from .worker import Worker

#: One connection per agent id, not one for the whole Houdini process. Two
#: tabs both talking to Claude share the same process; a tab that switches
#: to Gemini gets Gemini's own connection, and switching it again must not
#: disturb a sibling tab still using Claude — see `AgentPanel._agent_id`.
#: Not a widget attribute — otherwise closing one tab would take the
#: conversation open in another tab (using the same agent) down with it.
_shared_clients: dict[str, acp_client.AcpClient] = {}

#: Live panels, per agent id — weak references, since Qt deletes the widgets
#: itself and holding a strong reference here would just stop them from ever
#: dying. Which set a panel belongs to changes when it switches agents (see
#: `AgentPanel._rejoin_agent`); a panel closing only stops ITS agent's
#: client once no other tab using that SAME agent is left.
_live_panels_by_agent: dict[str, "weakref.WeakSet[AgentPanel]"] = {}


def _live_panels_for(agent_id: str) -> "weakref.WeakSet[AgentPanel]":
    return _live_panels_by_agent.setdefault(agent_id, weakref.WeakSet())


#: Whether some tab in THIS Houdini process has already swept
#: `orphans.py`'s leftover-agent file. `_boot()` runs once per TAB — two
#: panels opening together must not both read-modify-write the same JSON
#: file at once, and there is nothing left to find after the first sweep
#: anyway (see `_maybe_sweep_orphans`).
_orphans_swept = False


def shared_client(agent_id: str) -> acp_client.AcpClient:
    """The connection for this one agent id, process-wide."""
    client = _shared_clients.get(agent_id)
    if client is None:
        client = acp_client.AcpClient(agent_id=agent_id)
        _shared_clients[agent_id] = client
        # Everything the client reports goes to the on-disk log. Without this
        # the panel is undiagnosable on someone else's machine: the log file
        # existed but held only the startup header, never a word about the
        # agent itself.
        try:
            from .. import logbook

            logbook.setup()
            logbook.attach_client(client)
        except Exception:  # noqa: BLE001 - a log has no right to break the panel
            pass
    return client


def reset_shared_state_for_tests() -> None:
    """Reset process-wide singletons. Tests only."""
    global _shared_clients, _live_panels_by_agent, _orphans_swept
    for client in _shared_clients.values():
        client.stop()
    _shared_clients = {}
    _live_panels_by_agent = {}
    _orphans_swept = False
    sessions.reset_pool_for_tests()


def _apply_network_settings(current: settings_mod.Settings) -> None:
    """Point the panel's own requests at the studio's proxy and CA.

    `network.configure` is a primitive that nothing calls on its own — and a
    proxy feature that is fully implemented and never invoked is exactly the
    kind of thing that looks finished and does nothing. Called at startup and
    again whenever settings are saved, so a studio artist who fills these in
    does not have to restart Houdini to find out whether they got them right.

    Never fatal: the panel's downloads failing is a bad afternoon, the panel
    not opening is a worse one.
    """
    try:
        from .. import network, proxy

        network.configure(
            proxy=proxy.effective_proxy(current),
            ca_bundle=proxy.effective_ca_bundle(current),
        )
    except Exception:  # noqa: BLE001 - a misconfigured proxy must not stop the panel
        pass


def _update_is_stale(update: Any) -> bool:
    """Is this cached update already installed?

    Judged for the panel and fx too, which an earlier version of this
    deliberately skipped — on the reasoning that their versions come from the
    running process rather than a manifest, and so could not go stale. That
    was backwards. Update results are cached for a day and the panel is the
    thing that updates most often, so its banner is the FIRST to go stale:
    reported running 0.1.7 while being offered 0.1.5, with the button leading
    nowhere because there was nothing left to do.
    """
    latest = getattr(update, "latest", "")
    kind = getattr(update, "kind", "")
    try:
        from ..updates import is_newer

        if kind == "agent":
            from .. import runtime

            current = runtime.installed_version(getattr(update, "target", ""))
        elif kind == "panel":
            from .. import __version__ as current
        elif kind == "fx":
            from ..updates import _current_fx_version

            current = _current_fx_version()
        else:
            return False
    except Exception:  # noqa: BLE001 - a banner is never worth an exception
        return False
    return bool(current) and not is_newer(latest, current)


class _RefreshWorker(Worker):
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

    def work(self) -> None:  # pragma: no cover - covered via refresh.py
        entries: list = []
        registry_error = ""
        try:
            from .. import registry

            entries = registry.fetch_registry()
        except Exception as exc:  # noqa: BLE001 - the panel must work without a registry
            entries = []
            # Kept, not swallowed. Without the registry the Agents section is
            # simply empty, and an empty list of agents on a fresh install
            # looks exactly like a panel that does nothing — the artist has
            # no way to tell "couldn't reach the network" from "this is
            # broken". Reported first-hand from a fresh Linux install.
            registry_error = f"{type(exc).__name__}: {exc}"

        result = None
        try:
            result = refresh.daily_refresh(
                settings=self._settings,
                panel_version=settings_mod._panel_version(),
                entries=entries,
            )
        except Exception:  # noqa: BLE001 - the feed must never break the panel
            result = None

        self._registry_error = registry_error
        self.done.emit(result, entries)


class _OrphanSweepWorker(Worker):
    """Runs `orphans.sweep()` off the main thread, once per Houdini process.

    Reading the leftover-agent file and checking each candidate PID
    (`ps`/`lsof` on macOS, a couple of subprocess calls each) is cheap but
    not instant, and opening the panel must never wait on it — see
    `orphans.sweep`'s own docstring for what this is cleaning up and why
    it only happens here, at boot, rather than continuously.
    """

    done = Signal(list)  # list[orphans.SweptAgent]

    def work(self) -> None:
        from .. import orphans

        self.done.emit(orphans.sweep())


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


class _LaunchPrepWorker(Worker):
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

    def work(self) -> None:  # pragma: no cover - thin wrapper, logic lives in runtime
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
            # `install_agent`, not the cheaper-looking `launch_spec`: for an
            # already-installed agent (the overwhelming common case — the
            # chip only ever lists what `installed_agents` already has) it's
            # exactly `launch_spec` underneath, same cost — `install_agent`'s
            # very first line is `if is_installed(entry): return
            # launch_spec(entry)`. The difference only shows up for an agent
            # that can run without ever having gone through our own install
            # bookkeeping: an npx agent launches fine on nothing but npx's
            # own on-demand fetch, so `launch_spec` alone left the manifest
            # (and therefore the Settings screen and the update check, both
            # of which read it — see `_installed_record`/`check_updates`)
            # permanently believing it was never installed, no matter how
            # long it had actually been running. `install_agent` writes that
            # manifest as a side effect (for npx: `ensure_node` + one write,
            # no extra network — npx still does the real package fetch), so
            # the manifest stays the single source of truth for "is this
            # here" regardless of which door the agent came in through.
            if not runtime.is_installed(entry):
                self.note.emit(f"{entry.name}: installing…")
            spec = runtime.install_agent(entry)
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
        _apply_network_settings(self._settings)
        #: Which agent id THIS tab is attached to — its own connection
        #: (`shared_client(self._agent_id)`) and its own session list
        #: (`self._pool`). NOT the same fact as `self._settings.default_agent`,
        #: which is what a BRAND NEW tab opens with — this is what THIS one
        #: actually has right now, and the two can differ the moment this
        #: tab switches agents (this fixes that they used to be conflated:
        #: switching one tab's agent used to stop the ONE shared client and
        #: clear the ONE shared session pool, for every tab, regardless of
        #: which agent it was actually using).
        #:
        #: Switching agents in a tab DOES update `default_agent` too (see
        #: `_on_agent_chosen`) — deliberately: the last agent someone
        #: actually picked is the reasonable thing for the next new tab to
        #: open with, the same way switching a terminal's shell updates
        #: which one a new terminal in that same session starts with.
        #: `""` means no agent chosen yet (a fresh tab before `_boot()` has
        #: run, or one that landed on "Manage agents").
        self._agent_id: str = ""
        #: Which session THIS tab has on screen — the pool is shared (same
        #: session list, same agent process) among every tab on the SAME
        #: agent, but this is not: two tabs must be able to look at two
        #: different conversations at once (issue #21), including two tabs
        #: both on the same agent. See `_current_session`/
        #: `_set_current_session`.
        self._current_session_id: str | None = None
        #: Said once per tab, not once per agent switch — `_restore_conversations`
        #: runs on every rejoin now, and repeating the same notice each time an
        #: agent changes turns a useful sentence into noise.
        self._said_about_older_conversations = False
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
        #: Session ids already checked against `settings.config_options_by_
        #: agent` — see `_reapply_remembered_config`. Once per session: a
        #: later `config_option_update` reflects a live choice (the
        #: artist's own, or the agent's), not something to overwrite again.
        self._reapplied_config_sessions: set[str] = set()
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
        # Wired to the "" (no agent yet) client/pool for now — `_boot()`
        # calls `_rejoin_agent` to move onto the real one the moment it
        # knows which that is, before touching anything that depends on it.
        self._wire_client()
        self._wire_pool()

        _live_panels_for(self._agent_id).add(self)

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

        # Everything except the header lives in its own column below it, and
        # `_body_layout`'s margins never change — NOT while the drawer opens
        # or closes, not at any panel width. Two rejected designs got here:
        # first, reserving the drawer's width as this margin, which made the
        # feed and composer visibly jump sideways the instant the drawer
        # started (or finished) opening ("the panel jumps"); animating that
        # same margin instead of jumping it was the second attempt, and the
        # owner rejected that too — smooth or not, moving the content at all
        # wasn't wanted. What actually works: `TranscriptView` and
        # `Composer` already leave an empty gutter on either side of their
        # own 736px-wide content once the panel is wide enough
        # (`TranscriptView.current_gutter`), and the drawer draws INSIDE
        # that already-empty margin (`ConversationDrawer.set_available_
        # width`) instead of claiming new space. Too narrow for that and it
        # shrinks; narrower still and it overlaps a little rather than the
        # reading column ever getting permanently squeezed for it — see
        # `ConversationDrawer`'s own class docstring for the full reasoning.
        # The header stays full width regardless: its sidebar toggle is the
        # only way to close the drawer again, and the drawer starts below
        # the header (`set_top_inset`) so it can never end up underneath it.
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
        # The authoritative width sync: fires exactly when the transcript's
        # own gutter actually changes, so there's no ordering dependency on
        # whether the transcript's or the panel's resizeEvent runs first —
        # see `TranscriptView.gutter_changed`'s own docstring.
        self._transcript.gutter_changed.connect(self._conversations.set_available_width)

        # The panel forwards focus to the composer: Houdini activates the pane
        # tab and grants focus to the panel widget, not to anything inside it.
        # Without this that focus lands nowhere and typing needs another click.
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setFocusProxy(self._composer)

        self._header.manage_agents_clicked.connect(self._open_agent_management)
        self._header.agent_selected.connect(self._on_agent_chosen)
        self._header.conversations_clicked.connect(self._toggle_conversations)
        self._header.new_session_clicked.connect(self._start_new_session)
        self._header.settings_clicked.connect(lambda: self._show_page(self.PAGE_SETTINGS))
        self._conversations.new_session_clicked.connect(self._start_new_session)
        self._conversations.session_selected.connect(self._set_current_session)
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

        view = SettingsView(
            self, before_install=self._before_agent_install, before_uninstall=self._before_agent_uninstall
        )
        self._settings_view = view
        view.changed.connect(self._on_settings_changed)
        view.closed.connect(lambda: self._show_page(self.PAGE_TRANSCRIPT))
        view.install_succeeded.connect(self._on_agent_install_succeeded)
        view.install_failed.connect(self._on_agent_install_failed)
        view.sign_in_requested.connect(self._offer_sign_in)
        view.restart_agent_requested.connect(self._restart_agent)
        return view

    def _make_auth_view(self) -> QtWidgets.QWidget:
        from .auth_view import AuthView

        view = AuthView(self)
        self._auth_view = view
        # Through its own methods, not straight into shared_client(...).authenticate:
        # a direct subscription would permanently capture whichever client
        # instance existed when the widget was built. Switching THIS tab's
        # agent means talking to a different client object entirely (one
        # per agent id, not one for the whole process — see
        # `AgentPanel._agent_id`), and the login buttons would silently
        # start talking to a corpse.
        view.method_chosen.connect(self._on_auth_method_chosen)
        view.logout_requested.connect(self._on_logout_requested)
        return view

    def _show_page(self, index: int) -> None:
        self._pages.setCurrentIndex(index)
        # Writing to the agent from the settings or auth screen is pointless:
        # the reply lands in a feed the human can't see right now.
        self._composer.setVisible(index == self.PAGE_TRANSCRIPT)
        # And the conversation drawer belongs to the conversation. It
        # overlays rather than pushing content aside, which is right over a
        # transcript — that column is empty margin — and wrong over settings,
        # where it covered the agent names and the Back button with them,
        # leaving no way out except closing a drawer the artist might not
        # realise was open. It closes when the page changes; the toggle in
        # the header stays for when they come back.
        if index != self.PAGE_TRANSCRIPT and self._conversations.is_open():
            self._conversations.close_drawer()
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
        agent_id = self._settings.default_agent
        # Which agent THIS tab is attached to must be settled before
        # anything below touches `self._pool` or `shared_client(...)` —
        # `_restore_conversations` in particular needs a real `self._agent_id`
        # to add restored placeholders into the right pool, not whatever
        # the "" (no agent) one happened to hold.
        if agent_id:
            self._rejoin_agent(agent_id)

        self._restore_conversations()
        self._header.set_cwd(scene.hip_dir())
        self._refresh_agent_chip_menu()
        self._refresh_worker = _RefreshWorker(self._settings, self)
        self._refresh_worker.done.connect(self._on_refresh_done)
        self._refresh_worker.start()
        self._ask_telemetry_consent_once()
        self._maybe_sweep_orphans()

        if not agent_id:
            self._open_agent_management()
            return

        client = shared_client(agent_id)
        if client.is_running():
            # Another tab already has this SAME agent's connection up —
            # join it rather than starting a second process for it.
            self._adopt_running_client()
            return

        # The chip says which agent is chosen, and that is known from
        # settings before anything is launched. Only `_start_agent` used to
        # set it, so with autostart off the panel opened with a bare dot and
        # no name — while the menu behind it correctly showed that agent as
        # selected. Two controls, one fact, disagreeing.
        self._pending_agent_label = self._display_label(agent_id)
        self._header.set_agent(self._pending_agent_label, None)
        if not self._settings.autostart_agent:
            self._note('No agent running. Press "+" to start a conversation.')
            return
        self._start_agent(agent_id)

    def _adopt_running_client(self) -> None:
        info = shared_client(self._agent_id).agent_info()
        if info is not None:
            # The artist's name for it, not the npm package from `initialize`.
            # This path skips `_start_agent`, so nothing had set the pending
            # label and the chip fell back to "@agentclientprotocol/…".
            self._pending_agent_label = self._display_label(self._agent_id)
            self._header.set_agent(self._pending_agent_label or info.name, None)
            self._sync_agent_auth_row(info)
            self._composer.set_capabilities(info, self._settings.whisper_endpoint)
        self._refresh_sessions()
        current = self._current_session()
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
        self._composer.begin_boot(self._pending_agent_label)
        worker = _LaunchPrepWorker(agent_id, self._settings, self)
        worker.note.connect(self._note)
        # The prep worker knows things the phase name cannot: which package
        # is being fetched, how big it is. Shown in place of the generic
        # step name for as long as it has something to say.
        worker.note.connect(lambda text: self._composer.set_boot_phase(PHASE_PREPARING, text))
        worker.ready.connect(self._on_launch_ready)
        worker.prep_failed.connect(self._on_launch_prep_failed)
        self._launch_worker = worker
        worker.start()

    def _on_launch_ready(self, spec: Any, label: str) -> None:
        self._launch_worker = None
        if label:
            self._pending_agent_label = label
        self._composer.set_boot_phase(PHASE_LAUNCHING)
        shared_client(self._agent_id).start(spec, cwd=scene.hip_dir())

    def _on_launch_prep_failed(self, message: str) -> None:
        self._launch_worker = None
        self._composer.cancel_boot()
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

        `settings.installed_agents` alone used to miss anything installed
        only through an ordinary launch and never the explicit Install/Update
        button — an npx agent needs nothing else to run (npx fetches the
        package itself), so it could work for hours and then vanish from
        this exact menu the moment the registry refreshed, with no way back
        to the agent the artist had just been talking to. The manifest
        (`runtime.installed_version`) is what `_LaunchPrepWorker` actually
        writes on every launch now, so anything the registry knows about and
        the manifest says is here gets added too — settings.installed_agents
        stays the source for order and for whatever the registry hasn't
        loaded yet (nothing in `_registry_entries` right after boot).
        """
        from .. import runtime

        ids = list(self._settings.installed_agents)
        for entry in self._registry_entries:
            if entry.id not in ids and runtime.installed_version(entry.id) is not None:
                ids.append(entry.id)
        ids += [c.id for c in self._settings.custom_agents]
        items = [(agent_id, self._display_label(agent_id)) for agent_id in ids]
        # The checked entry is THIS tab's own agent, not the process-wide
        # default — a sibling tab on a different agent must not show up as
        # "selected" here just because it is what a NEW tab would open with.
        self._header.set_agent_menu(items, self._agent_id)

    # ------------------------------------------------------------- client

    def _wire_client(self) -> None:
        """Subscribe to `self._agent_id`'s client, remembering EVERY
        signal-slot pair.

        We remember pairs specifically because the client is shared across
        every tab using the SAME agent id: a bare ``signal.disconnect()``
        when one tab closes, or switches to a different agent, would also
        disconnect a sibling tab still on this one — which would stop
        getting the agent's replies while still looking alive. Call
        `_unwire_client()` first if this tab was already wired to a
        (possibly different) agent's client — `_rejoin_agent` does.
        """
        client = shared_client(self._agent_id)
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

    def _unwire_client(self) -> None:
        for signal, slot in getattr(self, "_client_wiring", ()):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._client_wiring = ()

    def _sync_agent_auth_row(self, info: Any) -> None:
        """Offer "Sign in" only when signing in is what's needed.

        `authMethods` from `initialize` says which methods EXIST, not whether
        the artist has used one — every agent lists them signed in or out. So
        keying the button on that offered sign-in to someone already working,
        with a session open and answers coming back. Reported exactly that
        way for Codex, and it is the same mistake fixed once in the agent
        switcher and then reintroduced when the button moved to settings.

        The honest signal is whether a session ever opened on this agent: the
        protocol has no "am I authenticated", but a `session/new` that
        succeeds is proof, and one that fails with auth_required is proof of
        the opposite. Signing out stays reachable from the sign-in screen
        itself, which is where someone who wants to switch accounts goes.
        """
        can_sign_in = bool(getattr(info, "auth_methods", ())) and not self._is_signed_in()
        self._settings_view.set_current_agent_auth(self._agent_id, can_sign_in)

    def _is_signed_in(self) -> bool:
        """Has this agent proved it is authenticated?

        The protocol offers no way to ask, and every capability flag answers
        a different question: `authMethods` lists what EXISTS,
        `supports_logout` says the method is implemented. Both are constant
        per agent, signed in or out.

        An open session looked like proof and is not. Measured on the Linux
        machine, where none of the agents had ever been configured:
        `claude-acp` advertises no methods and opens a session happily, then
        fails at the first prompt; `opencode` advertises one and also opens
        a session; only `codex-acp` refuses `session/new` with
        "Authentication required". So a session proves nothing on two agents
        out of three — which is precisely how a never-configured Claude came
        to be offered a "Sign out" button.

        A completed turn is what all three agree on: none of them will
        answer a prompt for an account that is not signed in. That is what
        gets recorded, and it is recorded persistently — otherwise the row
        returns on every Houdini restart until the artist types something.
        """
        return self._agent_id in self._settings.signed_in_agents

    def _remember_signed_in(self, signed_in: bool) -> None:
        """Record what a turn (or a sign-out) just proved about this agent."""
        if not self._agent_id:
            return
        known = list(self._settings.signed_in_agents)
        if signed_in and self._agent_id not in known:
            known.append(self._agent_id)
        elif not signed_in and self._agent_id in known:
            known.remove(self._agent_id)
        else:
            return
        self._settings.signed_in_agents = known
        settings_mod.save(self._settings)
        info = shared_client(self._agent_id).agent_info()
        if info is not None:
            self._sync_agent_auth_row(info)

    def _can_sign_out(self, info: Any) -> bool:
        """Reported on the Linux machine: the sign-in screen offered "Sign
        out" on an agent that had never been signed into. It was drawn from
        `supports_logout`, which Codex advertises either way — the same
        mistake as the Sign in row, one screen along."""
        if not getattr(info, "auth_methods", ()):
            # The panel only manages authentication it can see. An agent
            # that exposes no methods signs in and out through its own slash
            # commands, and a "Sign out" here would be a button that means
            # nothing — which is what a fresh Claude Agent showed.
            return False
        return bool(getattr(info, "supports_logout", False)) and self._is_signed_in()

    def _on_connected(self, info: Any) -> None:
        # Third phase. The process answered `initialize`; what remains is the
        # session, which is where the agent's MCP servers come up — measured
        # at 12-16s for the fx server alone under a Houdini interpreter, and
        # the longest silence of the whole boot.
        self._composer.set_boot_phase(PHASE_CONNECTING)
        # The chip shows the name the artist picked, not the npm package
        # name from initialize ("@agentclientprotocol/claude-agent-acp").
        self._header.set_agent(self._pending_agent_label or info.name, None)
        self._sync_agent_auth_row(info)
        self._composer.set_capabilities(info, self._settings.whisper_endpoint)
        # What a CLI prints when it starts, and the thing that was missing
        # from a five-second gap: "Preparing…", "Launching…", then silence
        # while the agent spawns, initialises and opens a session. Measured,
        # the panel's own share of that is under 10ms — the wait is the
        # agent's process, its handshake and its MCP servers, and none of it
        # is ours to shorten. Saying who answered, and from where, at least
        # tells the artist the silence ended.
        label = self._pending_agent_label or info.name
        version = f" {info.version}" if info.version else ""
        self._note(f"{label}{version} · {scene.hip_dir()}")
        self._show_page(self.PAGE_TRANSCRIPT)
        current = self._current_session()
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
        current = self._current_session()
        if current is not None:
            self._finish_activity(current.session_id)
        self._composer.set_capabilities(None, self._settings.whisper_endpoint)
        # No connected agent means no row should offer "Sign in…" either —
        # otherwise it keeps pointing at a process that no longer exists.
        self._settings_view.set_current_agent_auth(None, False)
        # A boot that ended in a dead agent is not progress. The reason goes
        # to the feed; a bar frozen partway would read as "still coming".
        self._composer.cancel_boot()
        self._note(f"Agent disconnected: {reason}" if reason else "Agent stopped.")

    def _on_failed(self, message: str) -> None:
        self._composer.cancel_boot()
        self._note(f"Agent failed to start: {message}")
        self._open_agent_management()

    def _on_auth_required(self, methods: list) -> None:
        # Whatever we thought, the agent has just said otherwise.
        self._remember_signed_in(False)
        info = shared_client(self._agent_id).agent_info()
        if not methods:
            self._offer_login_command(info)
            return
        self._auth_view.set_methods(methods, can_logout=self._can_sign_out(info))
        self._show_page(self.PAGE_AUTH)

    def _offer_login_command(self, info: Any) -> None:
        """The way in for an agent that lists no sign-in methods.

        Reported on the Linux machine: a fresh Claude Agent sent the artist
        to a screen headed "Sign in", reading "The agent offered no sign-in
        methods", with a Sign out button under it. Three untruths in one
        screen and no way forward from any of them.

        Agents that advertise no `authMethods` are not agents without a
        login — they are agents whose login is a slash command inside the
        session, which is what Zed's own documentation says to use. So say
        that, and put the command in the composer where it costs a keystroke.
        Same treatment `_report_stalled_new_session` gives, for the same
        reason: the screen the protocol suggests is a dead end here.
        """
        label = self._pending_agent_label or getattr(info, "name", "") or "This agent"
        self._note(
            f"{label} isn't signed in, and offers no sign-in method to the "
            f"panel — it uses its own /login command instead. It's ready in "
            f"the input box below."
        )
        self._composer.set_text("/login")
        self._show_page(self.PAGE_TRANSCRIPT)

    def _on_session_started(self, session_id: str, state: Any) -> None:
        # There is a session: the agent is up, its tools are loaded, and the
        # chips below are about to appear. This is the end of the boot and
        # the strip says so before removing itself.
        self._composer.finish_boot()
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
        # A session opening is the proof that this agent is signed in — the
        # protocol offers no other. Re-evaluate now, or the Sign in button
        # stays offered to somebody already talking to it.
        info = shared_client(self._agent_id).agent_info()
        if info is not None:
            self._sync_agent_auth_row(info)
        self._set_current_session(session_id)
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
        self._reapply_remembered_config(session_id, options)

    def _reapply_remembered_config(self, session_id: str, options: list) -> None:
        """Put back whatever the artist last picked for THIS agent.

        ACP scopes `configOptions` to a live session — there is no protocol
        concept of a saved preference, so a Houdini restart used to reset
        every session back to the agent's own defaults, silently undoing a
        choice the artist made on purpose. `settings.config_options_by_
        agent` is where `_on_config_option_selected` remembers it; this is
        where it gets reapplied, onto the FIRST `configOptions` a fresh
        `session/new` ever reports for this session id — not every later
        `config_option_update`, which reflects a live choice (the artist's
        own next click, or the agent's) that this must not fight.

        A remembered value that no longer exists among the agent's current
        choices (a model retired, an option renamed after an update) is
        left alone, silently: the agent's own default is a perfectly good
        answer, and logging a warning for a stale preference nobody asked
        about would just be noise.
        """
        if session_id in self._reapplied_config_sessions:
            return
        self._reapplied_config_sessions.add(session_id)
        remembered = settings_mod.load().config_options_by_agent.get(self._agent_id)
        if not remembered:
            return
        client = shared_client(self._agent_id)
        for option in options:
            value = remembered.get(option.id)
            if value is None or value == option.current_value:
                continue
            if value not in {choice.value for choice in option.choices}:
                continue
            client.set_config_option(session_id, option.id, value)

    def _on_config_option_selected(self, config_id: str, value: str) -> None:
        """Record the pick right away — same reasoning as `_on_mode_selected`.

        `state.config_options` otherwise only moves when the agent sends its
        own `config_option_update`, which coming back to this session later
        would overwrite with whatever the value was before the pick.

        Also remembered per-agent in `settings.json` (`config_options_by_
        agent`), so the same pick survives a restart — see
        `_reapply_remembered_config`.
        """
        current = self._current_session()
        if current is not None:
            current.config_options = [
                replace(option, current_value=value) if option.id == config_id else option
                for option in current.config_options
            ]
            self._pool.mark_changed(current.session_id)
            shared_client(self._agent_id).set_config_option(current.session_id, config_id, value)

            remembered = settings_mod.load()
            remembered.config_options_by_agent.setdefault(self._agent_id, {})[config_id] = value
            settings_mod.save(remembered)

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
        if stop_reason == "end_turn":
            # An answered prompt is the one thing every agent agrees means
            # "signed in" — see `_is_signed_in` for what does not.
            self._remember_signed_in(True)
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
        target = session_id or (self._current_session().session_id if self._current_session() else "")
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
        shared_client(self._agent_id).answer_permission(request_key, option_id or None)
        if session_id:
            entry = self._model(session_id).resolve_permission(request_key, option_id or None)
            if entry is not None:
                self._touch(session_id, entry.id)
        self._sync_permission_popover()

    # ------------------------------------------------------------ sessions

    def _wire_pool(self) -> None:
        """Subscribe to `self._agent_id`'s session pool, and REMEMBER the
        subscriptions.

        The pool is process-wide PER AGENT ID, so a connection into it
        outlives the tab that made it and is shared with every other tab on
        the SAME agent. These were fire-and-forget, and a lambda holding
        `self` kept every panel ever opened alive with its whole widget tree
        — closing a tab freed nothing. On a desktop where panels get opened
        and closed through the day that shows up as hundreds of stray empty
        windows, which is exactly how it was reported. Call
        `_unwire_pool()` first if this tab was already wired to a
        (possibly different) agent's pool — `_rejoin_agent` does.
        """
        refresh = lambda _sid: self._refresh_sessions()  # noqa: E731 - one slot, three signals
        self._pool_wiring = (
            (self._pool.added, refresh),
            (self._pool.removed, refresh),
            (self._pool.changed, refresh),
            (self._pool.removed, self._on_pool_session_removed),
        )
        for signal, slot in self._pool_wiring:
            signal.connect(slot)

    def _unwire_pool(self) -> None:
        for signal, slot in getattr(self, "_pool_wiring", ()):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._pool_wiring = ()

    def _rejoin_agent(self, agent_id: str) -> None:
        """Make `agent_id` the one THIS tab is attached to: its connection
        (`shared_client`), its session list (`self._pool`), and its place
        in the per-agent live-tab count that decides when that agent's
        client actually stops (see `shutdown`). Safe to call at boot (moving
        off the initial `""`) and on every later switch — unwires whatever
        this tab was attached to before, so nothing here ever double-fires
        on an agent this tab has already left.

        Does not itself start or stop anything: the caller decides that
        (`_boot`, `_on_agent_chosen`). This only moves which agent's
        objects THIS tab is listening to.
        """
        self._unwire_client()
        self._unwire_pool()
        _live_panels_for(self._agent_id).discard(self)
        self._agent_id = agent_id
        _live_panels_for(agent_id).add(self)
        self._wire_client()
        self._wire_pool()
        # And bring that agent's own history back onto its list. Leaving an
        # agent writes its conversations to disk and, once the last tab is
        # gone, empties its session pool — correctly, since those ids belong
        # to a process that has stopped. Nothing put them back: reading the
        # store happened only when a panel opened. So going Claude -> Codex
        # -> Claude showed an empty drawer, and the conversations looked
        # lost when they were sitting on disk the whole time. Idempotent, so
        # a tab joining an agent another tab is already using adds nothing.
        self._restore_conversations()

    @property
    def _pool(self) -> sessions.SessionPool:
        """THIS tab's own agent's session list — see `self._agent_id`."""
        return sessions.pool(self._agent_id)

    def _current_session(self) -> sessions.SessionState | None:
        """Whichever session THIS tab has open — see `_current_session_id`."""
        if self._current_session_id is None:
            return None
        return self._pool.get(self._current_session_id)

    def _set_current_session(self, session_id: str) -> None:
        """Make `session_id` the one on screen in THIS tab, and only this one.

        Replaces `SessionPool.set_current` (see its removal note): a click
        in one tab's drawer, or restoring history on boot, must never move
        a sibling tab's own conversation.
        """
        if session_id not in [s.session_id for s in self._pool.all()]:
            return
        if session_id == self._current_session_id:
            return
        self._current_session_id = session_id
        self._show_session(session_id)

    def _on_pool_session_removed(self, session_id: str) -> None:
        """A session left the shared pool — react only if THIS tab had it open.

        A sibling tab deleting some OTHER conversation must not move this
        one (issue #21). Mirrors what `SessionPool.remove` used to do to its
        own single shared `_current_id` — falls back to whatever's left, or
        to no current session at all. Does NOT start a new session on its
        own: this fires for every removal, including the internal "swap the
        restored placeholder for the real session id" in
        `_on_session_started`, where a replacement is already on its way a
        few lines later — starting one here too raced a second, spurious
        `session/new`. Only the artist's own "delete this conversation"
        (`_on_session_removed`) decides to open a fresh one when nothing is
        left.
        """
        if session_id != self._current_session_id:
            return
        remaining = self._pool.all()
        if remaining:
            self._set_current_session(remaining[-1].session_id)
        else:
            self._current_session_id = None

    def _refresh_sessions(self) -> None:
        current = self._current_session()
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
        current = self._current_session()
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
        # The drawer's WIDTH is kept in sync by `TranscriptView.gutter_
        # changed` (connected in `_build`), not pulled here — pulling
        # `current_gutter()` from inside this method, itself often called
        # from `AgentPanel`'s own `resizeEvent`, could read a value the
        # transcript's OWN resizeEvent hadn't updated yet for this same
        # resize, i.e. exactly the staleness `gutter_changed` exists to
        # avoid. `_body` itself never moves either way, at any width.
        self._conversations.sync_parent_geometry()
        if self._conversations.isVisible():
            self._conversations.raise_()

    def _on_drawer_state_changed(self, _open: bool) -> None:
        # `_body` never moves when the drawer opens or closes (see the
        # comment where it's built) — this only keeps floating chrome
        # ordered correctly. `_position_permission_popover`'s own
        # `.raise_()` is what keeps an active permission request visible on
        # TOP of a drawer that just opened over it, instead of hidden
        # underneath.
        self._position_permission_popover()

    def _start_new_session(self) -> None:
        """Ask the agent for another session — and never do it silently.

        `session/new` is a round trip to someone else's process, and for the
        agents we ship it also spawns an MCP server. When that takes a while,
        or never answers at all, the panel used to show absolutely nothing:
        the artist clicks "+", nothing appears, and the only available
        conclusion is that the button is broken. So the request is announced
        when it goes out and chased up if the answer never comes.
        """
        client = shared_client(self._agent_id)
        if not client.is_running():
            # THIS tab's own agent, not necessarily the process-wide
            # default — if this tab never had one (a fresh "" agent id),
            # there is nothing here for "+" to restart.
            if self._agent_id:
                self._start_agent(self._agent_id)
            else:
                self._open_agent_management()
            return
        before = {state.session_id for state in self._pool.all()}
        # Fourth phase, and the slow one — the agent starts its MCP servers
        # here. Only reported while a boot is on screen: pressing "+" in a
        # running agent is a different, much shorter wait that the busy
        # indicator already covers.
        self._composer.set_boot_phase(PHASE_SESSION)
        client.new_session(cwd=scene.hip_dir(), mcp_servers=scene.mcp_servers())
        QtCore.QTimer.singleShot(
            _NEW_SESSION_GRACE_MS, lambda: self._report_stalled_new_session(before)
        )

    def _report_stalled_new_session(self, before: set) -> None:
        """Say what is actually wrong, which is usually "it isn't signed in".

        Also ends the boot, whatever it says. The progress strip and the
        cover over the input belong to a start that is still happening; a
        `session/new` that never answered is not one. Seen for real on the
        Linux machine: the panel reported the stall in the feed while the
        strip sat full at 4/4 and the input stayed blurred and unusable, so
        the artist could not even type the `/login` the message suggested.

        On a machine where the agent has never been configured, it connects
        happily, advertises NO auth methods at all, and then never answers
        `session/new` — measured on all six of them with an empty HOME. The
        panel had nothing to show (its sign-in screen is drawn FROM those
        auth methods) and said "it may be busy or stuck, try switching
        agents", sending a new artist round a loop that cannot end: every
        other agent does the same thing.

        The agents all take the same way out, the one Zed's own docs give:
        run their `/login` inside the session. So offer that instead of a
        diagnosis we know is wrong — and put the command in the composer, so
        it costs a keystroke rather than knowing it exists.
        """
        self._composer.cancel_boot()
        if self._closed:
            return
        if {state.session_id for state in self._pool.all()} - before:
            return  # the agent answered, nothing to complain about

        client = shared_client(self._agent_id)
        info = client.agent_info()
        if info is not None and not info.auth_methods:
            label = self._pending_agent_label or info.name
            self._note(
                f"{label} connected but hasn't opened a conversation, and it "
                f"offers no sign-in method — which usually means it isn't set "
                f"up yet. Most agents are signed in with their own /login "
                f"command; it's ready in the input box below."
            )
            self._composer.set_text("/login")
            return
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
        # `_on_pool_session_removed` (wired to the pool's shared `removed`
        # signal, fired synchronously by the line above) already picked a
        # fallback conversation for THIS tab if the deleted one was the one
        # on screen here — but it deliberately never starts a new session on
        # its own (see its docstring), so that is still this method's job:
        # an artist who just cleared their own drawer should land somewhere
        # usable, not on an empty feed with no session to prompt.
        if self._current_session() is None:
            self._start_new_session()

    def _release_session(self, session_id: str, *, agent_id: str | None = None) -> None:
        """Give a session back to the agent, if it is a real one.

        Restored conversations carry OUR id, not the agent's — there is
        nothing on the far side to close, and asking would be a lie about
        what exists. `agent_id` defaults to THIS tab's own
        (`self._agent_id`); an explicit one is for releasing a session that
        belonged to an agent this tab just switched AWAY from — see
        `_on_agent_chosen`.
        """
        if not session_id or session_id.startswith(_RESTORED_PREFIX):
            return
        shared_client(agent_id if agent_id is not None else self._agent_id).close_session(session_id)

    def _model(self, session_id: str) -> TranscriptModel:
        return self._models.setdefault(session_id, TranscriptModel())

    def _is_current(self, session_id: str) -> bool:
        current = self._current_session()
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
        current = self._current_session()
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
            # placeholder title, and this is where the first thing the artist
            # says replaces it. The old placeholder is still listed because
            # conversations written before the rename are on disk with that
            # exact title — dropping it would leave them called "New
            # conversation" forever, no matter what was said in them.
            if current.title in ("", "New chat", "New conversation"):
                current.title = summarize_title(text)
                self._pool.mark_changed(current.session_id)
        current.busy = True
        self._composer.set_busy(True)
        activity = self._model(current.session_id).start_activity()
        self._touch(current.session_id, activity.id)
        self._composer.trigger_buddy()
        shared_client(self._agent_id).prompt(current.session_id, blocks)

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
        current = self._current_session()
        if current is None:
            self._composer.set_busy(False)
            return
        shared_client(self._agent_id).cancel(current.session_id)
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
        current = self._current_session()
        if current is not None:
            current.current_mode_id = mode_id
            self._pool.mark_changed(current.session_id)
            shared_client(self._agent_id).set_mode(current.session_id, mode_id)

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
        if not entries:
            # Say why the list is empty. `fetch_registry` falls back to a
            # cached copy of any age, so reaching this at all means there is
            # no cache either — a first run that could not reach the network.
            reason = getattr(self._refresh_worker, "_registry_error", "")
            self._note(
                "Couldn't fetch the agent list, so there is nothing to install "
                "yet. Check the network — or, behind a studio firewall, "
                "Settings → Network."
                + (f"\n{reason}" if reason else "")
            )
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
            # Checked against what is on disk right now, the same guard the
            # agent rows use. Update results are cached for a day and the
            # manifest changes the moment an agent is launched or installed,
            # so a banner could go on offering 0.64.2 to someone already
            # running 0.64.2. Pressing it then does nothing observable —
            # installing an existing version is a no-op — and a button that
            # cannot act is indistinguishable from a broken one, which is
            # exactly how this was reported.
            if _update_is_stale(update):
                continue
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
            # Say the command that actually works, and say it once. The
            # advice used to be `pip install --upgrade`, which is not how
            # anyone installed this — the README's one-liner is uvx, and pip
            # would miss the `--refresh` that stops uvx serving its cached
            # copy. And the notice stayed up afterwards, so pressing Update
            # again just repeated the same line; reported as three identical
            # messages stacked in the feed.
            self._note(
                f"{update.target} can't replace itself while Houdini is running it. Run:\n"
                f"    uvx --refresh --from {update.target} python -m houdini_agent_panel install\n"
                "then restart Houdini."
            )
            self._active_update = None
            self._notice.hide_notice()
            return
        self._show_page(self.PAGE_SETTINGS)
        self._settings_view.focus_agents()
        if not self._settings_view.trigger_agent_update(update.target):
            self._note(f"Could not find {update.label} to update — try Settings → Agents.")

    def _before_agent_install(self, agent_id: str) -> None:
        """About to overwrite `agent_id`'s files on disk (install OR update,
        from the settings row or from the notice banner — this fires either
        way, see `AgentsView.__init__`). If it's running, its files are what
        the live process is reading from RIGHT NOW — swapping them under it
        is not something this panel does silently. Stopping it here, not
        just asking the artist to: they already said "update" once, and a
        second manual step for something the panel can safely do itself is
        the friction this project's UI rule exists to cut.
        `_on_agent_install_succeeded` brings it back up.

        Only restarts it if `agent_id` is THIS tab's own agent
        (`self._agent_id`) — restarting an agent some OTHER, unrelated tab
        happens to be using isn't this tab's call to make; that tab is still
        wired to the same `shared_client(agent_id)` and will see it die,
        same as any other disconnect. A tab updating an agent it is not
        itself using at all still stops it (the files are about to change
        regardless of who notices), it just does not track bringing it back
        up — there is currently no owner for that restart in this case, a
        gap worth a dedicated look rather than a guess here.
        """
        client = shared_client(agent_id)
        if not client.is_running():
            return
        self._note(
            f"Stopping {self._display_label(agent_id)} to update it"
            + (" — it restarts automatically once the update finishes." if agent_id == self._agent_id else ".")
        )
        if agent_id == self._agent_id:
            self._restart_after_update = agent_id
        client.stop()

    def _before_agent_uninstall(self, agent_id: str) -> None:
        """About to delete `agent_id`'s files entirely (Remove). Same hazard
        as `_before_agent_install`, without the "brings it back up" half —
        Remove means the artist wants it gone, not restarted, so unlike an
        update this never sets `_restart_after_update`. The header is reset
        to blank only if THIS tab was the one showing `agent_id` — a sibling
        tab using some OTHER agent must not have its own header touched by
        a Remove click that has nothing to do with it.
        """
        client = shared_client(agent_id)
        if client.is_running():
            self._note(f"Stopping {self._display_label(agent_id)} to remove it.")
            client.stop()
            if agent_id == self._agent_id:
                self._header.set_agent("", None)

    def _on_agent_install_succeeded(self, agent_id: str) -> None:
        if self._active_update is not None and self._active_update.target == agent_id:
            self._active_update = None
            self._notice.hide_notice()
        if self._restart_after_update == agent_id:
            self._restart_after_update = None
            self._note(f"{self._display_label(agent_id)} updated — restarting it…")
            self._start_agent(agent_id)
            return

        # A first install used to end here, silently. The chip menu is built
        # from `settings.installed_agents`, which the install has just
        # changed — without this it goes on listing what was there before,
        # so the agent an artist just installed is missing from the one menu
        # they would use to pick it. Reported exactly that way.
        label = self._display_label(agent_id)
        self._refresh_agent_chip_menu()
        if not shared_client(agent_id).is_running():
            # And say what happens next. An npx agent installs in under a
            # second — nothing downloads, npx fetches the package on first
            # launch — so a row flipping to "installed" is the only sign
            # anything happened at all, and it reads like nothing did.
            self._note(f"{label} installed. Pick it in the agent menu to start.")

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
    _FATAL_STDERR_MARKERS = ("authorizationrequired", "fatal", "error", "command not found")

    def _on_log_line(self, line: str) -> None:
        lowered = line.lower()
        if not any(marker in lowered for marker in self._FATAL_STDERR_MARKERS):
            return
        if "command not found" in lowered:
            # npx can leave its cache half-made: the directory exists, the
            # package inside does not, and it then runs the missing binary
            # and exits 0. Every launch after that fails identically, and
            # the only visible sign is a shell error naming a command
            # nobody typed. Seen for real on a machine whose network was
            # dropping large transfers mid-stream when the agent was first
            # installed — the download died, the cache stayed, and a working
            # network later changed nothing because npx thought it was done.
            self._note(
                "The agent's package didn't finish downloading, and npx keeps "
                "reusing the incomplete copy. Clear its cache and start the "
                "agent again:\n    rm -rf ~/.npm/_npx"
            )
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
        info = shared_client(self._agent_id).agent_info()
        if info is None or not info.auth_methods:
            return
        if self._pages.currentIndex() == self.PAGE_AUTH:
            return
        self._auth_view.set_methods(
            list(info.auth_methods), can_logout=self._can_sign_out(info)
        )
        self._show_page(self.PAGE_AUTH)

    #: What each measured sign-in method actually does, so the panel can say
    #: it instead of leaving the artist watching a button. Keyed by the
    #: method id the agent advertises; anything unknown gets the generic
    #: line. Measured on a clean HOME (docs/facts/acp-sdk.md §12).
    _AUTH_ADVICE = {
        "chat-gpt": (
            "Opening ChatGPT in your browser — it can take a few seconds to "
            "appear. Sign in there and come back; the panel is waiting and "
            "will say when it's through."
        ),
        "api-key": (
            "Codex reads its API key from the environment: set CODEX_API_KEY "
            "(or OPENAI_API_KEY) in your shell profile, then restart Houdini. "
            "The panel picks up your login shell's variables at start."
        ),
    }

    def _on_auth_method_chosen(self, method_id: str) -> None:
        self._last_auth_method = method_id
        self._note(
            self._AUTH_ADVICE.get(
                method_id,
                f"Signing in with {method_id}… if a browser window opens, finish it there.",
            )
        )
        shared_client(self._agent_id).authenticate(method_id)

    def _on_authenticated(self, method_id: str) -> None:
        """Sign-in worked — get out of the way and open a conversation.

        Leaving the artist on the sign-in screen after a successful sign-in
        was the bug: they approved it in the browser and the panel gave no
        sign it had noticed, so it looked like the login had failed.
        """
        self._note("Signed in.")
        self._remember_signed_in(True)
        self._show_page(self.PAGE_TRANSCRIPT)
        if self._current_session() is None:
            self._start_new_session()

    def _on_logout_requested(self) -> None:
        """Logging out sends the panel back where sign-in came from.

        After a successful logout, the client raises `auth_required` with
        the same methods that came from `initialize` — the sign-in screen
        shows up on its own, no separate branch needed here. If the agent
        couldn't log out, an `error` arrives instead and the human stays
        put: silently pretending the logout happened isn't an option.
        """
        shared_client(self._agent_id).logout()

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

    def _maybe_sweep_orphans(self) -> None:
        """Once per Houdini process (`_orphans_swept`, module-wide — every
        tab calls this from its own `_boot`), look for agent processes a
        PAST session never got to stop and clean them up. See `orphans.py`
        for the whole story; this is only the wiring: off the main thread
        (`_OrphanSweepWorker`), so a machine with a few stale entries never
        delays this tab opening.
        """
        global _orphans_swept
        if _orphans_swept:
            return
        _orphans_swept = True
        worker = _OrphanSweepWorker(self)
        worker.done.connect(self._on_orphans_swept)
        self._orphan_sweep_worker = worker
        worker.start()

    def _on_orphans_swept(self, cleaned: list) -> None:
        """Say so, once, in the feed — a silent cleanup is indistinguishable
        from nothing having been wrong in the first place, and the artist
        should know a past crash left something running (may-hub task,
        2026-08-04)."""
        if not cleaned:
            return
        names = ", ".join(sorted({self._display_label(agent.agent_id) or agent.agent_id for agent in cleaned}))
        count = len(cleaned)
        noun = "process" if count == 1 else "processes"
        self._note(
            f"Cleaned up {count} leftover agent {noun} from a previous Houdini "
            f"session that didn't shut down cleanly: {names}."
        )

    def _on_settings_changed(self) -> None:
        self._settings = settings_mod.load()
        _apply_network_settings(self._settings)
        info = shared_client(self._agent_id).agent_info()
        self._composer.set_capabilities(info, self._settings.whisper_endpoint)
        self._refresh_agent_chip_menu()

    def _on_agent_chosen(self, agent_id: str) -> None:
        """Switch THIS tab's agent — called from the header chip's menu.

        Reload from disk BEFORE writing — mandatory. self._settings is a
        snapshot from when the panel opened, and the agents section writes a
        freshly-added custom agent straight to the file. Saving the stale
        snapshot on top used to erase that agent, and "add a custom agent →
        pick it right away" failed with "agent isn't in the registry or
        among custom agents" (found only by testing live, in both Houdini
        versions).

        Also updates `default_agent`: the last agent someone actually
        picked is the reasonable thing for the next NEW tab to open with —
        the same way switching a terminal's shell changes what a new
        terminal in that session starts with. It is not the same fact as
        `self._agent_id` (see where that is declared); this tab's own
        connection and session list follow `self._agent_id` only.

        A per-agent connection/pool now: only the agent THIS tab is
        leaving is affected, and only if no OTHER tab is still attached to
        it — a sibling tab on that same agent keeps working untouched, and
        one already on a third agent was never in scope at all. Old
        behaviour (single shared client/pool, stopped and cleared on every
        switch by every tab) is exactly the bug this replaced: switching
        one tab's agent used to silently drop a sibling tab's own
        conversation and connection.
        """
        if agent_id == self._agent_id:
            return  # already this one
        old_agent_id = self._agent_id

        self._settings = settings_mod.load()
        self._settings.default_agent = agent_id
        settings_mod.save(self._settings)

        self._switch_agent_process(old_agent_id, agent_id)

    def _switch_agent_process(
        self, old_agent_id: str, new_agent_id: str, *, rejoin: bool = True
    ) -> None:
        """Persist → detach from `old_agent_id` → attach to `new_agent_id`
        → launch it. The tail of `_on_agent_chosen`, factored out so
        `_restart_agent` doesn't duplicate it (issue #26) — a settings field
        an agent only reads at spawn (the Network proxy fields) needs this
        exact recipe with the SAME agent id on both sides.

        `rejoin=True` (a genuine identity switch, `_on_agent_chosen`) moves
        this tab's client/pool wiring and live-panel membership onto the
        new agent via `_rejoin_agent` — which also calls
        `_restore_conversations()` for it — and the old agent's process
        only stops if no OTHER tab is still using it.

        `rejoin=False` (`_restart_agent`): same identity, so there is
        nothing to rejoin — this tab never left. The process stops
        unconditionally instead: it is the one shared by every tab on this
        agent (`shared_client`), so a restart is not "am I still using
        it," it is "does everyone using it get the new environment." The
        pool is cleared just the same, so `_restore_conversations()` is
        called directly to refill it from disk before relaunching.
        """
        # The CONVERSATION is not the session id: it is what the artist
        # wrote and read, and wiping it on every switch (or restart) was
        # the bug, not the feature. Written to disk before anything about
        # the old process is touched.
        self._persist_conversations()
        self._current_session_id = None
        self._pending_permissions.clear()

        if rejoin:
            # Detach from the old agent's live-panel count BEFORE deciding
            # whether it was the last tab there — `_rejoin_agent` does the
            # detaching, this reads the result right after.
            self._rejoin_agent(new_agent_id)
            stop_old = not _live_panels_for(old_agent_id)
        else:
            stop_old = True

        if stop_old:
            # Nobody else needs the old process (switch), or restarting it
            # is the whole point (restart) — either way, a session id
            # belongs to the process that issued it, so the binding goes
            # with it. A sibling tab still using this same agent during a
            # SWITCH would have kept both; a restart always tears it down.
            old_client = shared_client(old_agent_id)
            old_pool = sessions.pool(old_agent_id)
            for state in old_pool.all():
                self._release_session(state.session_id, agent_id=old_agent_id)
            if old_client.is_running():
                old_client.stop()
            old_pool.clear()

        if not rejoin:
            # `_rejoin_agent` would normally have refilled the pool from
            # disk as part of moving onto the new agent — skipped above
            # since there is no new agent to move onto, so it happens here
            # instead, now that `old_pool.clear()` emptied it.
            self._restore_conversations()

        self._refresh_agent_chip_menu()
        # A message queued for the old agent has nowhere to land yet: the new
        # one has not opened a session. It is kept and sent once it does.
        self._start_agent(new_agent_id)

    def _restart_agent(self) -> None:
        """Restart THIS tab's own agent process, in place — no identity
        change, no `default_agent` write.

        An agent reads its environment once, at spawn
        (docs/2026-08-03-proxy-support.md), so a Network setting change
        (proxy/no-proxy/CA bundle) needs a fresh process to take effect,
        not a Houdini restart. Wired to `SettingsView.restart_agent_
        requested` (the Network section's own banner button); the
        settings screen decides WHEN to offer this, this method only does
        it.
        """
        if not self._agent_id:
            return
        self._switch_agent_process(self._agent_id, self._agent_id, rejoin=False)

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
                # The scene this conversation belongs to. Preferably from the
                # session, so saving after the artist opens a different scene
                # doesn't drag the previous scene's conversations along — but
                # NEVER left empty, which is what happened when the session
                # was already gone from the pool. A conversation with no
                # scene matches no scene: twenty-five of the reporter's
                # Claude conversations were saved that way and became
                # invisible for good, which read as "the panel lost them".
                # Leaving the pool is exactly when persisting matters most.
                conversation.cwd = (
                    (state.cwd if state is not None else "")
                    or conversation.cwd
                    or scene.hip_dir()
                )
                # THIS tab's own agent at the moment of persisting, not
                # `self._settings.default_agent`: `_on_agent_chosen` already
                # updates that to the NEW agent before persisting the OLD
                # agent's conversation — tagging it with `default_agent`
                # there would mislabel it as belonging to an agent it was
                # never actually had with, and `store.load`'s new per-agent
                # filter (see `conversations_store.py`) would then never
                # find it again under the agent it really happened with.
                conversation.agent_id = self._agent_id or ""
                conversation.entries = records
                conversation.updated_at = time.time()
                existing[conversation_id] = conversation
            current = self._current_session()
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
            stored = store.load(here, self._agent_id)
            active_id = store.load_active_id()
            # Anything written before conversations were tied to a scene
            # and/or an agent has none to belong to, so it is not shown
            # here. Saying so once is the difference between "scoped" and
            # "the panel ate my history" — the file is untouched and still
            # holds all of it. One combined note for both fields
            # (`unscoped_count`), not two near-identical ones: an artist who
            # already read "N conversations aren't tied to a scene" does not
            # need the same sentence again about agents right next to it.
            older = store.unscoped_count()
            if older and not self._said_about_older_conversations:
                self._said_about_older_conversations = True
                self._note(
                    f"{older} conversation(s) from before this version tied conversations "
                    f"to a scene and an agent aren't shown here. They are still in "
                    f"{store.store_path()}."
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
        # Restored conversations go into the drawer and NOT onto the screen.
        # Opening the last one looked helpful and was misleading: it was
        # usually had with a different agent, and reopening it under today's
        # agent shows a conversation the model has no memory of, presented
        # as if it were continuous. An artist opening the panel gets a new
        # chat with the agent they chose; the old ones are one click away in
        # the drawer, which is what "conversations survive a restart" was
        # ever supposed to mean.
        del ids, active_id
        # Nothing else reads this, and the drawer is refreshed from the pool
        # like every other change. There used to be a
        # `self._conversations.set_restored(...) if hasattr(...) else None`
        # here — a guarded call to a method `ConversationDrawer` has never
        # had, so it was a no-op from the day it was written, waiting to
        # look like it did something.
        self._restored = stored

    def _note(self, text: str) -> None:
        current = self._current_session()
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
        _live_panels_for(self._agent_id).discard(self)

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

        # THIS tab's agent's connection only, and only if no OTHER tab is
        # still attached to it — closing one of two tabs sharing the same
        # agent must not take the other one's conversation down with it.
        # A sibling tab on a DIFFERENT agent was never affected either way.
        if not _live_panels_for(self._agent_id):
            client = _shared_clients.pop(self._agent_id, None)
            if client is not None:
                client.stop()
            # Same reasoning as `_on_agent_chosen`'s own cleanup: a session
            # id belongs to the process that just died, and leaving it in
            # the pool would greet whichever tab opens this SAME agent
            # next with session ids from a process that no longer exists.
            sessions.pool(self._agent_id).clear()

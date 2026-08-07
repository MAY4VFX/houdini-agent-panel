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
import os
import shutil
import time
import weakref
from dataclasses import replace
from typing import Any

from .. import client as acp_client
from .. import refresh, scene, sessions, settings as settings_mod
from .. import shellenv, signin_evidence, updates as updates_mod
from ..announcements import Announcement, Button
from ..logbook import logger as _logbook_logger
from ..transcript_model import PermissionView, TranscriptModel
from .announcement import BlockingNotice, ConsentStrip, NoticeStrip
from .chips import HeaderBar
from .boot_status import PHASE_CONNECTING, PHASE_LAUNCHING, PHASE_PREPARING, PHASE_SESSION
from .composer import Composer
from .conversations import ConversationDrawer, empty_scope_text, summarize_title
from .permissions import PermissionRow
from .qt import QShortcut, QtCore, QtGui, QtWidgets, Signal
from .transcript import TranscriptView
from . import worker as worker_module
from .worker import Worker

_log = _logbook_logger("houdini_agent_panel.ui.panel")

#: One connection per agent id, not one for the whole Houdini process. Two
#: tabs both talking to Claude share the same process; a tab that switches
#: to Gemini gets Gemini's own connection, and switching it again must not
#: disturb a sibling tab still using Claude — see `AgentPanel._agent_id`.
#: Not a widget attribute — otherwise closing one tab would take the
#: conversation open in another tab (using the same agent) down with it.
_shared_clients: dict[str, acp_client.AcpClient] = {}

#: Coalescing window for `_persist_conversations_soon` — see its docstring.
_PERSIST_COOLDOWN_MS = 500

#: Namespaces the sign-in-offer notice's id apart from a real `Announcement.id`
#: (the feed's ids are opaque strings from `feed/announcements.json`, so
#: nothing stops one from colliding with an agent id in theory) — see
#: `_maybe_offer_sign_in`/`_on_notice_action`/`_on_notice_dismissed`.
_SIGNIN_OFFER_PREFIX = "sign-in-offer:"

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


def _panel_update_notice_id(update: Any) -> str:
    """The synthetic `Announcement.id` a self-update's persistent "restart
    Houdini" notice uses — distinct from `update.target` itself (the id
    `NoticeStrip.show_update`/`_on_notice_action` already use for the
    OFFER), so a leftover `_active_update` for some other package can
    never be mistaken for this one, and so `_on_notice_dismissed` can tell
    "dismissed the restart reminder" from "dismissed a real announcement"
    without guessing from shape alone.
    """
    return f"panel-update-restart-pending:{update.target}"


#: Lines `install.py`/`deps.py` print that restate their own argv rather
#: than report real progress — currently just `deps.py`'s own
#: `f"Installing dependencies: {printable_argv(argv)}"`. The line, argv
#: and all, already reaches the log (`SelfUpdateWorker.work`'s own
#: `_log.info`); showing it in the notice too meant a `--target
#: /Users/.../deps/py3.11` path wrapping across two lines, immediately
#: followed by a long silent stretch while `hython` itself starts
#: (measured 8.9-16.5s, `mcp_runtime.py`'s own numbers) — reported for
#: real as looking like the update had hung.
_PANEL_UPDATE_ADMIN_PREFIXES = ("Installing dependencies:",)


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

    def __init__(
        self, current: settings_mod.Settings, parent=None, *, fresh_start: bool = True
    ) -> None:
        super().__init__(parent)
        self._settings = current
        #: Passed straight through to `updates.check` — see its own
        #: docstring. `True` (the default) is a panel that just opened;
        #: `False` is a periodic re-check from `AgentPanel`'s own recurring
        #: timer (`_on_session_refresh_due`), for a panel that has already
        #: been running a while.
        self._fresh_start = fresh_start

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
                fresh_start=self._fresh_start,
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

#: A live failure on the owner's own Linux box (docs/facts/acp-sdk.md §18):
#: a browser tab reached "you're all set up," the spawned `claude setup-
#: token` was still running minutes later, and the panel never moved past
#: `_on_terminal_login_slow`'s own "still working" note — which fires once,
#: says nothing further, and has no way out besides the Cancel button that
#: was already there but never pointed at. This is the second, LONGER
#: timer: if neither a real prompt (`input_requested`) nor the process
#: ending has happened by now, the artist gets an explicit next step
#: instead of an indefinitely stale "still working." Long enough that a
#: genuinely slow but working connection doesn't get told to give up
#: (`_on_terminal_login_slow` above already covers the first 15s; this is
#: what happens if THAT wasn't the end of it either).
_TERMINAL_LOGIN_STUCK_MS = 75_000

#: How often a panel that stays open re-checks for updates on its own,
#: without a restart — reuses `_RefreshWorker`/`_on_refresh_done` exactly
#: as the boot check does, `fresh_start=False` the only difference
#: (`updates.py::_SESSION_MAX_AGE`, same duration, same reasoning: several
#: releases in an hour on a busy day, so a panel left open all day must
#: not need a restart to ever find out). Derived from that constant
#: rather than a second number written here, so the two can never drift
#: apart from each other by accident.
_SESSION_REFRESH_INTERVAL_MS = int(updates_mod._SESSION_MAX_AGE.total_seconds() * 1000)

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
    PAGE_BUGREPORT = 3

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
        #: Agent ids whose sign-in offer (`_maybe_offer_sign_in`) the artist
        #: dismissed in THIS tab's lifetime — never written to `settings`,
        #: per the owner's own ask: dismissing it means "not now," not
        #: "never tell me again." A fresh tab, or a Houdini restart, offers
        #: it again if it's still warranted by then.
        self._dismissed_signin_offers: set[str] = set()
        self._models: dict[str, TranscriptModel] = {}
        self._pending_permissions: dict[str, str] = {}
        self._permission_views: dict[str, PermissionView] = {}
        self._permission_popover: PermissionRow | None = None
        self._refresh_worker: _RefreshWorker | None = None
        #: Re-fires `_RefreshWorker` on its own, roughly every
        #: `_SESSION_REFRESH_INTERVAL_MS`, so a panel left open for a day
        #: still notices a new release without the artist ever restarting
        #: Houdini — see that constant's own comment. Armed once, in
        #: `_boot()`; stopped in `shutdown()`.
        self._session_refresh_timer: Any = None
        #: The handle `scene.watch_hip_dir_changes` returned, so `shutdown()`
        #: can remove it — `None` before `_boot()` registers it, and once
        #: more after. See `_on_hip_dir_changed` for what it's for.
        self._hip_watch_handle: Any = None
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
        #: Set by `_on_agent_row_sign_in`/`_on_agent_row_sign_out` when the
        #: artist clicked Sign in/out on a Settings row for an agent that
        #: ISN'T this tab's own — there is no way to hold a second live
        #: connection open per tab (`_agent_id`), so the only route is to
        #: switch onto it first and open the sign-in screen the moment it
        #: connects. Consulted (and cleared) by `_complete_pending_auth_
        #: switch`, called from both places a connect can end up (a brand
        #: new session via `_on_session_started`, or reattaching to one
        #: already live via `_on_connected`'s own tail).
        self._pending_auth_target: str | None = None
        #: Set for the duration of a `logout()` call so its outcome —
        #: `auth_required` (success) or `error` (failure) on the SAME
        #: signals a plain sign-in failure uses — can be told apart and
        #: recorded as a sign-out attempt rather than a sign-in one
        #: (`_record_auth_attempt`, issue #33's "last attempt" text).
        self._pending_logout_agent: str | None = None
        #: True from `_on_auth_method_chosen` until the pending
        #: `authenticate()` resolves (or the artist cancels the wait) —
        #: lets `_on_log_line` know an agent's stderr right now is likely
        #: about the sign-in in progress (gemini's `oauth-personal`: the
        #: ONLY thing it ever says, docs/facts/acp-sdk.md §13) rather than
        #: unrelated noise from a running conversation.
        self._auth_pending: bool = False
        #: The worker currently running a spawned terminal-auth process
        #: (Kimi's `kimi login`, §13-14), if any — `None` the rest of the
        #: time. Kept so `_on_auth_cancel_pending`/`_show_page`/`shutdown`
        #: can stop it: it polls indefinitely on its own, so leaving it
        #: running after the artist has moved on is a real leak, the same
        #: hazard `orphans.py` exists for on the agent process itself.
        self._terminal_login_worker: Any = None
        #: Set alongside `_terminal_login_worker` — see `_on_terminal_login_
        #: exited`/`_terminal_login_fallback_message` for what they're for.
        self._terminal_login_url_shown: bool = False
        self._terminal_login_command: str = ""
        #: Which agent id `_terminal_login_worker` was started for — see
        #: `_start_terminal_login`'s own comment for the residual race
        #: this guards even `_stop_terminal_login`'s signal-disconnect
        #: doesn't fully close (a Qt cross-thread signal already queued at
        #: the moment of disconnect still gets delivered).
        self._terminal_login_agent_id: str = ""
        #: Whether the spawned process has printed ANYTHING yet — tells
        #: apart a fetch/start that never got off the ground (no output at
        #: all — on a machine that needs a proxy, the npx fetch happens
        #: BEFORE the CLI prints a byte, docs/facts/acp-sdk.md §14) from a
        #: real authentication failure (it printed something, then ended).
        #: Reported as the single most confusing failure mode the panel
        #: has: the two used to read identically.
        self._terminal_login_got_output: bool = False
        #: Single-shot: if nothing has printed yet after a while, say so
        #: instead of leaving the artist watching an unchanged message —
        #: informational only, this NEVER kills the process (unlike
        #: `authenticate()`, this is ours to poll, but not to give up on).
        self._terminal_login_slow_timer: Any = None
        #: The longer, second timer — see `_TERMINAL_LOGIN_STUCK_MS`'s own
        #: comment. Also never kills anything; it only makes sure silence
        #: this long stops looking identical to "still working normally."
        self._terminal_login_stuck_timer: Any = None
        #: Whether the child has reached an actual input prompt this
        #: attempt — the one truly conclusive event short of exiting.
        #: `_on_terminal_login_stuck` checks this before saying anything:
        #: once the artist has a field to type into, they are not stuck.
        self._terminal_login_input_requested_seen: bool = False
        #: The `Update` currently shown by the notice strip, if any — set
        #: only from `_on_refresh_done`. `NoticeStrip.action_clicked` fires
        #: for BOTH an announcement's button and this one's "Update" button
        #: (same signal, same slot); this is how `_on_notice_action` tells
        #: them apart.
        self._active_update: Any = None
        #: The in-flight `SelfUpdateWorker`, if the notice strip's "Update"
        #: button is currently running a panel/fxhoudinimcp update — see
        #: `_start_update`. `None` the rest of the time, including right
        #: after it finishes (`_on_panel_update_succeeded`/`_failed` clear
        #: it themselves, same shape as every other worker field here).
        self._panel_update_worker: Any = None
        #: The text `_on_panel_update_progressed` is currently showing —
        #: not necessarily the LAST line the child printed, see its own
        #: comment: an administrative line (the raw pip command echo) is
        #: logged but never shown, so this stays on the last line that
        #: actually meant something to an artist reading it.
        self._panel_update_display_line: str = ""
        #: `time.monotonic()` when the current self-update started, or
        #: `None` — `_panel_update_tick_timer` uses it to show elapsed
        #: seconds even while nothing new has printed. Reported for real:
        #: the artist thought the panel had hung, because the only text on
        #: screen during `hython`'s own multi-second startup (measured
        #: 8.9-16.5s, `mcp_runtime.py`'s own numbers) was a static line
        #: that never changed.
        self._panel_update_started_at: float | None = None
        #: Repeats roughly once a second for as long as a self-update is
        #: running — `None` the rest of the time. Its only job is to
        #: re-render the SAME notice with a fresh elapsed-time count, so
        #: something on screen keeps moving even during a stretch where
        #: the child prints nothing at all.
        self._panel_update_tick_timer: Any = None
        #: The `Update` a self-update just finished installing, set by
        #: `_on_panel_update_succeeded` and never cleared by anything
        #: except this panel actually closing — a fact true for the rest
        #: of THIS Houdini session belongs on screen the whole time, not
        #: as a line the feed scrolls away (`_on_refresh_done` defers to
        #: it instead of replacing it with some other banner).
        self._panel_update_restart_pending: Any = None
        #: Set right before stopping the currently-running agent to update
        #: it out from under itself — the agent_id to bring back up once
        #: that update actually finishes (`_on_agent_install_succeeded`).
        self._restart_after_update: str | None = None
        self._closed = False
        #: `_persist_conversations_soon`'s coalescing state — see its
        #: docstring. `_cooldown_active` gates further calls into the
        #: dirty-flag path instead of a fresh write each; `_dirty` is what
        #: the trailing write at the end of the window checks.
        self._persist_cooldown_active = False
        self._persist_dirty = False

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
        # `_boot()` is intentionally deferred until Houdini has accepted
        # the pane widget. That leaves one real first frame before the timer
        # fires; on H21 it was visible as a bare accent dot and looked like
        # the installed agents had disappeared. The choice is already in
        # the settings we loaded synchronously, so name it immediately.
        if self._settings.default_agent:
            self._pending_agent_label = self._display_label(self._settings.default_agent)
            self._header.set_agent(self._pending_agent_label, None)
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
        self._pages.insertWidget(self.PAGE_BUGREPORT, self._make_bug_report_view())

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
        self._transcript.queue_remove_requested.connect(self._on_queue_remove_requested)

        # The panel forwards focus to the composer: Houdini activates the pane
        # tab and grants focus to the panel widget, not to anything inside it.
        # Without this that focus lands nowhere and typing needs another click.
        self.setFocusPolicy(QtCore.Qt.StrongFocus)
        self.setFocusProxy(self._composer)

        self._header.manage_agents_clicked.connect(self._open_agent_management)
        self._header.agent_selected.connect(self._on_agent_chosen)
        self._header.conversations_clicked.connect(self._toggle_conversations)
        self._header.new_session_clicked.connect(self._start_new_session)
        self._header.settings_clicked.connect(self._toggle_settings)
        self._conversations.new_session_clicked.connect(self._start_new_session)
        self._conversations.session_selected.connect(self._set_current_session)
        self._conversations.session_renamed.connect(self._on_session_renamed)
        self._conversations.session_removed.connect(self._on_session_removed)

        self._composer.submitted.connect(self._on_submitted)
        self._composer.enqueue_requested.connect(self._on_enqueue_requested)
        self._composer.cancelled.connect(self._on_cancelled)
        self._composer.mode_selected.connect(self._on_mode_selected)
        self._composer.config_option_selected.connect(self._on_config_option_selected)
        self._composer.attachment_rejected.connect(self._note)
        self._composer.buddy_selected.connect(self._on_buddy_selected)
        self._composer.bug_report_link_clicked.connect(self._open_bug_report)

        self._notice.action_clicked.connect(self._on_notice_action)
        self._notice.dismissed.connect(self._on_notice_dismissed)
        self._blocking.action_clicked.connect(self._on_blocking_action)
        self._consent.answered.connect(self._on_telemetry_answer)

        # Settings lost its own back button (owner's call — it's an overlay
        # now, not a page you navigate away from): Escape is one of the
        # remaining ways out, alongside the "…" button toggling it closed
        # again and whatever already returned to the transcript on its own
        # (an agent switch does, deliberately). `WidgetWithChildrenShortcut`
        # so it fires no matter which child inside Settings currently has
        # focus — a plain `keyPressEvent` override on `self` would miss
        # every keystroke a focused child widget consumes first. Scoped to
        # ONLY close Settings, never anything else: Auth/BugReport are out
        # of scope for this change and keep whatever behaviour they already
        # had (a real `keyPressEvent` on the composer's own popup is a
        # separate, already-working mechanism this doesn't touch).
        escape = QShortcut(QtGui.QKeySequence(QtCore.Qt.Key_Escape), self)
        escape.setContext(QtCore.Qt.WidgetWithChildrenShortcut)
        escape.activated.connect(self._on_settings_escape)

    def _toggle_settings(self) -> None:
        """The "…" button's one job now (bug reporting moved to its own
        link — see `HeaderBar.settings_clicked`'s own docstring): open
        Settings if it isn't showing, close it (back to the transcript) if
        it is. Same click, opposite outcome depending on current state —
        exactly the owner's own description of it.
        """
        if self._pages.currentIndex() == self.PAGE_SETTINGS:
            self._show_page(self.PAGE_TRANSCRIPT)
        else:
            self._show_page(self.PAGE_SETTINGS)

    def _on_settings_escape(self) -> None:
        if self._pages.currentIndex() == self.PAGE_SETTINGS:
            self._show_page(self.PAGE_TRANSCRIPT)

    def _make_settings_view(self) -> QtWidgets.QWidget:
        from .settings_view import SettingsView

        view = SettingsView(
            self, before_install=self._before_agent_install, before_uninstall=self._before_agent_uninstall
        )
        self._settings_view = view
        view.changed.connect(self._on_settings_changed)
        view.install_succeeded.connect(self._on_agent_install_succeeded)
        view.install_failed.connect(self._on_agent_install_failed)
        view.sign_in_requested.connect(self._on_agent_row_sign_in)
        view.sign_out_requested.connect(self._on_agent_row_sign_out)
        view.restart_agent_requested.connect(self._restart_agent)
        view.bug_report_requested.connect(self._open_bug_report)
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
        view.cancel_pending.connect(self._on_auth_cancel_pending)
        view.terminal_login_input_submitted.connect(self._on_terminal_login_input_submitted)
        return view

    def _make_bug_report_view(self) -> QtWidgets.QWidget:
        from .bugreport_view import BugReportView

        view = BugReportView(self)
        self._bug_report_view = view
        view.closed.connect(lambda: self._show_page(self.PAGE_TRANSCRIPT))
        view.attachments_changed.connect(self._on_bugreport_attachments_changed)
        return view

    def _show_page(self, index: int) -> None:
        if (
            self._pages.currentIndex() == self.PAGE_AUTH
            and index != self.PAGE_AUTH
            and self._terminal_login_worker is not None
        ):
            # Leaving the sign-in screen with a spawned terminal-auth
            # process (Kimi) still running — it polls indefinitely on its
            # own (docs/facts/acp-sdk.md §14), so walking away without
            # stopping it is a real leak, not a background task that will
            # tidy itself up.
            self._stop_terminal_login()
        self._pages.setCurrentIndex(index)
        # Writing to the agent from the settings or auth screen is pointless:
        # the reply lands in a feed the human can't see right now.
        self._composer.setVisible(index == self.PAGE_TRANSCRIPT)
        # The "…" button's own pressed look tracks whichever route actually
        # opened or closed Settings — its own click, Escape, or an agent
        # switch landing back on the transcript — not just its own click,
        # since every one of those goes through this single funnel.
        self._header.set_settings_open(index == self.PAGE_SETTINGS)
        # And the conversation drawer belongs to the conversation. It
        # overlays rather than pushing content aside, which is right over a
        # transcript — that column is empty margin — and wrong over settings,
        # where it covered the agent names, leaving no way out except
        # closing a drawer the artist might not realise was open. It closes
        # when the page changes; the toggle in the header stays for when
        # they come back.
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

    def _open_bug_report(self) -> None:
        """Gathers everything the report screen shows and opens it — the
        owner's ask: a button he can press, type a comment into, and send.

        Gathered here, on the main thread, not in `BugReportView` itself
        or the worker: `bugreport.gather_system_fields` can fall back to
        `import hou` (`hou` is never touched off the main thread, this
        project's own rule), and the conversation tail needs THIS tab's
        own `TranscriptModel`, which this widget is the only thing that
        knows how to find (`self._model`, `self._current_session`).
        """
        from .. import bugreport, logbook

        current = settings_mod.load()
        system_fields = bugreport.gather_system_fields(self._agent_id)
        log_tail, log_redacted = bugreport.read_log_tail(logbook.log_path())

        session = self._current_session()
        entries = self._model(session.session_id).entries() if session is not None else []
        conversation_tail, conversation_redacted = bugreport.conversation_tail_text(entries)

        self._bug_report_view.open_for(
            system_fields=system_fields,
            log_tail=log_tail,
            log_redacted=log_redacted,
            conversation_tail=conversation_tail,
            conversation_redacted=conversation_redacted,
            attachment_prefs=current.bugreport_attachments,
            endpoint=current.bugreport_endpoint or bugreport.DEFAULT_ENDPOINT,
        )
        self._show_page(self.PAGE_BUGREPORT)

    def _on_bugreport_attachments_changed(self, prefs: dict) -> None:
        """Remembered right away, on every toggle — not only when the
        report is actually sent — so leaving this screen without sending
        still keeps the choice for next time (the NDA case the feature
        exists to answer)."""
        current = settings_mod.load()
        current.bugreport_attachments = dict(prefs)
        settings_mod.save(current)
        self._settings = current

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
        # `_restore_conversations` and the header's cwd label are both
        # scoped to `scene.hip_dir()` AT THIS MOMENT, and nothing above
        # ever asks again. If the artist opens a real scene into a
        # Houdini session this tab started against a different one (most
        # often a fresh, unsaved file — `hip_dir()`'s own `$HOME`
        # fallback), everything above stays wrong for the rest of the
        # tab's life: not just the label, but which on-disk conversations
        # ever get offered back. `_on_hip_dir_changed` is what re-runs
        # both the moment the scene actually moves; see its own comment.
        # Guarded like `_maybe_sweep_orphans` and every other optional
        # extra in this method: a panel that cannot register this watcher
        # still has an agent worth booting — it only keeps the older,
        # boot-time-only scoping, not nothing.
        try:
            self._hip_watch_handle = scene.watch_hip_dir_changes(self._on_hip_dir_changed)
        except Exception:  # noqa: BLE001
            _log.warning("could not watch for scene changes", exc_info=True)
        self._refresh_agent_chip_menu()
        self._refresh_worker = _RefreshWorker(self._settings, self)
        self._refresh_worker.done.connect(self._on_refresh_done)
        self._refresh_worker.start()
        self._ask_telemetry_consent_once()
        self._maybe_sweep_orphans()

        # See `_SESSION_REFRESH_INTERVAL_MS`'s own comment. Repeating, not
        # single-shot — this IS the panel's own recurring schedule for
        # checking again; nothing else re-arms it.
        session_refresh_timer = QtCore.QTimer(self)
        session_refresh_timer.timeout.connect(self._on_session_refresh_due)
        session_refresh_timer.start(_SESSION_REFRESH_INTERVAL_MS)
        self._session_refresh_timer = session_refresh_timer

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

    def _on_hip_dir_changed(self) -> None:
        """The scene underneath this tab may have just moved — File > Open,
        File > New, a merge, a load — see `scene.watch_hip_dir_changes`'s
        own docstring for the bug this closes. Re-does exactly what
        `_boot()` did once, using `scene.hip_dir()`'s CURRENT answer:
        `_restore_conversations` already reads it fresh and is already
        idempotent (skips anything already in `self._pool`), so nothing
        about what it does needed to change, only how often it runs.
        """
        self._header.set_cwd(scene.hip_dir())
        self._restore_conversations()

    def _on_session_refresh_due(self) -> None:
        """The recurring half of `_SESSION_REFRESH_INTERVAL_MS` — a panel
        that has been open this long checks again on its own, the same way
        `_boot()`'s own one-time check already does, just `fresh_start=
        False` (`updates.py`'s own longer cache window applies, so this
        doesn't hit PyPI on every single tick if a previous one — boot's,
        or an earlier tick's — already answered recently enough).

        Skips a tick outright if the LAST `_RefreshWorker` (whichever
        triggered it) is somehow still running — `_SESSION_REFRESH_
        INTERVAL_MS` is hours, a real check is seconds, so this is a
        defensive backstop, not the expected path. Never force-stops
        anything mid-flight; the next tick, hours later, tries again.
        """
        worker = self._refresh_worker
        if worker is not None and worker.isRunning():
            return
        self._refresh_worker = _RefreshWorker(self._settings, self, fresh_start=False)
        self._refresh_worker.done.connect(self._on_refresh_done)
        self._refresh_worker.start()

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
        self._note(message, error=True)
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
        """Cache what THIS connection's `initialize` said about signing in,
        and let every Settings row redraw from it.

        Used to gate the Settings row's "Sign in" on `not self._is_signed_
        in()` — reported for Codex: the row stayed offered while the agent
        was already answering questions. That fix traded one wrong belief
        for another: `_is_signed_in()` is itself a guess (the protocol has
        no "am I authenticated" query — see its own docstring), and issue
        #33 is the other side of the same coin — an artist stuck on a
        broken Gemini/Vertex login with the panel silently convinced they
        were already signed in, and no way back to the screen that would
        have let them retry. `authMethods`/`supports_logout` cost nothing to
        get wrong (they're just "does a button do anything"), while gating
        reachability on a guess about account state can strand someone
        entirely. So this only caches capability now, unconditionally.
        """
        self._remember_agent_auth_capability(self._agent_id, info)
        self._refresh_agent_auth_rows()

    def _remember_agent_auth_capability(self, agent_id: str, info: Any) -> None:
        """Persist what `agent_id`'s `initialize` just reported about
        signing in — `authMethods`/`supports_logout` are constants of the
        BUILD, not the account (docs/facts/acp-sdk.md §11), so this stays
        valid long after the artist switches away from `agent_id`, which is
        exactly what lets a Settings row for a DIFFERENT, not-currently-
        connected agent still offer Sign in/Sign out (issue #33).
        """
        if not agent_id:
            return
        methods = [
            settings_mod.AgentAuthMethod(id=m.id, name=m.name, description=m.description)
            for m in getattr(info, "auth_methods", ()) or ()
        ]
        supports_logout = bool(getattr(info, "supports_logout", False))
        current = settings_mod.load()
        existing = current.agent_auth_info.get(agent_id)
        if methods:
            record = settings_mod.AgentAuthInfo(methods=methods, supports_logout=supports_logout)
            if existing == record:
                return
            current.agent_auth_info[agent_id] = record
        elif existing is not None:
            # This build offers nothing the panel can manage any more (e.g.
            # after an update) — drop the stale cache rather than keep
            # offering a button for a method that no longer exists.
            del current.agent_auth_info[agent_id]
        else:
            return
        settings_mod.save(current)
        self._settings = current

    def _refresh_agent_auth_rows(self) -> None:
        self._settings_view.refresh_agent_auth()

    def _record_auth_attempt(
        self, agent_id: str, *, action: str, ok: bool, message: str, method_id: str = ""
    ) -> None:
        """Persist what a sign-in/out attempt just did, so the Settings row
        that started it can say so beside the button — even after Houdini
        restarts, and even for an agent that isn't the one connected right
        now (issue #33: "a failure is visible where the retry button is
        rather than in the transcript").
        """
        if not agent_id:
            return
        current = settings_mod.load()
        current.auth_attempts[agent_id] = settings_mod.AuthAttempt(
            action=action,
            method_id=method_id or self._last_auth_method,
            ok=ok,
            message=message,
            at=settings_mod.AuthAttempt.now(),
        )
        settings_mod.save(current)
        self._settings = current
        self._refresh_agent_auth_rows()

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
        """Whether Sign out is worth drawing at all: the agent has to
        actually implement logout, and there has to be at least one method
        to return to afterwards.

        Used to also require `self._is_signed_in()` — reported on the Linux
        machine: a fresh Codex showed Sign out before anyone had signed in.
        That guard traded one wrong guess for another: `_is_signed_in()` is
        itself only a guess (see its own docstring), and gating reachability
        on it is exactly issue #33's report. `supports_logout`/`auth_
        methods` are both constants of the BUILD, not the account
        (docs/facts/acp-sdk.md §11), so this now reflects only what the
        agent can do — never a guess about whether it's needed right now.
        """
        if not getattr(info, "auth_methods", ()):
            # The panel only manages authentication it can see. An agent
            # that exposes no methods signs in and out through its own slash
            # commands, and a "Sign out" here would be a button that means
            # nothing — which is what a fresh Claude Agent showed.
            return False
        return bool(getattr(info, "supports_logout", False))

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
        self._maybe_offer_sign_in(info)
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
        else:
            # A session was already live (a reattach, not a fresh connect) —
            # `_on_session_started` isn't coming to do this instead, so if
            # switching here was in aid of signing in (`_on_agent_row_sign_
            # in`/`_sign_out`), THIS is the last point that can honor it.
            self._complete_pending_auth_switch()

    def _maybe_offer_sign_in(self, info: Any) -> None:
        """Offer sign-in the moment the agent connects — before a turn is
        wasted finding out the hard way.

        The report this answers: Claude Agent chosen, "hi" typed, a 1m41s
        wait, then five lines explaining it needs to sign in — while
        already signed in to the desktop app. `claude-acp` advertises no
        auth methods and opens a session happily either way (docs/facts/
        acp-sdk.md §11); it only fails at the first prompt. So "connected"
        alone tells us nothing, and neither does a session existing.

        Two things this must never do: nag an artist who genuinely is
        signed in (`signin_evidence.has_credential_evidence` — checked
        BEFORE our own incomplete record, `_is_signed_in`, ever gets a
        vote either way), and repeat itself once dismissed, for the rest
        of THIS tab's life (`_dismissed_signin_offers` — never persisted;
        "not now" is not "never").
        """
        if self._is_signed_in():
            return
        if self._agent_id in self._dismissed_signin_offers:
            return
        # The SAME composed environment the agent process itself gets
        # (`client.py::do_start`, `ui/terminal_login.py::TerminalLoginWorker
        # .build_env`) — `os.environ` alone is what Houdini saw, missing
        # whatever only the artist's shell profile sets (shellenv.py's own
        # module docstring). `shellenv.capture()`'s one-time subprocess
        # cost is already paid by now: `do_start` calls the same cached
        # function, synchronously, before the process it just connected TO
        # was even spawned — this can only ever be a cache hit here.
        env = shellenv.merged(dict(os.environ))
        if signin_evidence.has_credential_evidence(self._agent_id, env=env):
            return
        label = self._pending_agent_label or getattr(info, "name", "") or "This agent"
        self._notice.show_notice(
            Announcement(
                id=_SIGNIN_OFFER_PREFIX + self._agent_id,
                severity="info",
                title=f"{label} may not be signed in yet.",
                buttons=(Button("Sign in", ""),),
            )
        )

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
        # Nothing to un-cache here: `agent_auth_info` survives a disconnect
        # deliberately (it's a build constant, not a live fact — see
        # `_remember_agent_auth_capability`), and every OTHER agent's row
        # was never touched by this one going away.
        # A boot that ended in a dead agent is not progress. The reason goes
        # to the feed; a bar frozen partway would read as "still coming".
        self._composer.cancel_boot()
        # `reason` is only ever non-empty for an ABNORMAL exit (`client.py`'s
        # own `disconnected.emit` sites: `""` for a normal stop, a real
        # message only for "agent process exited unexpectedly") — worth the
        # split rather than one line covering both severities.
        if reason:
            self._note(f"Agent disconnected: {reason}", error=True)
        else:
            self._note("Agent stopped.")
        # A switch that was in aid of signing in has nowhere left to land —
        # the agent it was headed for just went away.
        self._pending_auth_target = None
        self._pending_logout_agent = None

    def _on_failed(self, message: str) -> None:
        self._composer.cancel_boot()
        self._note(f"Agent failed to start: {message}", error=True)
        self._open_agent_management()
        self._pending_auth_target = None
        self._pending_logout_agent = None

    def _on_auth_required(self, methods: list) -> None:
        # Whatever we thought, the agent has just said otherwise.
        self._remember_signed_in(False)
        # A fresh `auth_required` moots any wait already in progress —
        # including a spawned terminal-auth process (Kimi), which has no
        # reason to still be running once the agent has said this.
        self._stop_terminal_login()
        self._auth_pending = False
        if self._pending_logout_agent == self._agent_id:
            # `do_logout` reports success this way: the agent has nothing
            # of its own to signal "logged out" with, so it just answers
            # with the same auth_required it would after any other loss of
            # credentials (`client.py::do_logout`'s own docstring).
            self._pending_logout_agent = None
            self._record_auth_attempt(
                self._agent_id, action="sign_out", ok=True, message="Signed out."
            )
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
        self._show_page(self.PAGE_TRANSCRIPT)
        if self._has_login_command():
            self._note(
                f"{label} isn't signed in, and offers no sign-in method to "
                f"the panel — it has its own /login command instead. It's "
                f"ready in the input box below."
            )
            self._composer.set_text("/login")
            return
        # No sign-in method AND no /login. Telling them to type it anyway is
        # what this used to do, and the agent answered "/login isn't
        # available in this environment" — which was measured beforehand and
        # ignored: `claude-acp` reports an EMPTY command list.
        self._note(f"{label} isn't signed in. {self._no_methods_advice()}")

    #: Static, per-agent advice for when `initialize` reports NO sign-in
    #: methods at all — the agent still has a real way in, the panel just
    #: can't drive it (docs/facts/acp-sdk.md §9/§11). Keyed by agent id;
    #: `_GENERIC_NO_METHODS_ADVICE` covers anything not listed here.
    _NO_METHODS_ADVICE = {
        "claude-acp": (
            "No auth method, no /login command (measured: claude-acp "
            "reports an empty command list) — it reads credentials the "
            "machine already has. In a terminal:\n    claude setup-token\n"
            "which writes ~/.claude/.credentials.json (the macOS Keychain "
            "on a Mac) — the adapter picks that up. An ANTHROPIC_API_KEY "
            "exported in your shell profile works too; the panel passes "
            "your login shell's environment to the agent. Then restart it "
            "from Settings."
        ),
    }
    _GENERIC_NO_METHODS_ADVICE = (
        "No sign-in method the panel can act on, and no /login command "
        "either. It likely reads credentials from its own configuration "
        "or environment — check its own documentation for how to sign "
        "in, then restart it from Settings."
    )

    def _no_methods_advice(self) -> str:
        return self._NO_METHODS_ADVICE.get(self._agent_id, self._GENERIC_NO_METHODS_ADVICE)

    def _has_login_command(self) -> bool:
        """Does the open session actually offer a login command?

        Asked of the session, never assumed. `availableCommands` is the only
        place the answer exists, and it differs per agent: measured on a
        clean machine, `claude-acp` returns an empty list.
        """
        current = self._current_session()
        commands = (getattr(current, "available_commands", None) or []) if current else []
        return any(getattr(c, "name", "") in ("login", "auth") for c in commands)

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
                # Anything still queued when this conversation was written
                # to disk (`_restore_conversations` rebuilds it from the
                # `queued`-kind entries) rides along the same way the
                # transcript and the title just did — a queue is part of
                # the conversation, not something a restart gets to erase.
                state.queued = restored_state.queued
                self._pool.remove(adopted)
        import uuid as _uuid

        self._conversation_ids.setdefault(session_id, _uuid.uuid4().hex)
        # A brand new session cannot be mid-turn. Saying so explicitly keeps a
        # stale flag from a previous agent out of a fresh conversation.
        state.busy = False
        self._models.setdefault(session_id, TranscriptModel())
        self._pool.add(state)
        # Re-cache this agent's auth capability now that we have a live
        # `agent_info()` again — cheap, and keeps the Settings row current
        # even though nothing about signing in actually depends on a
        # session existing any more (see `_sync_agent_auth_row`).
        info = shared_client(self._agent_id).agent_info()
        if info is not None:
            self._sync_agent_auth_row(info)
        self._set_current_session(session_id)
        self._show_session(session_id)
        self._show_page(self.PAGE_TRANSCRIPT)
        # If this tab switched agents in aid of signing in
        # (`_on_agent_row_sign_in`/`_sign_out`), THIS is the point that
        # honors it — after the page above, so it isn't immediately undone.
        self._complete_pending_auth_switch()
        pending, self._pending_prompt = self._pending_prompt, None
        if pending:
            self._on_submitted(pending)
        # A restored queue with nothing pending ahead of it: nothing else
        # is going to call `_drain_queue` for a session that just started
        # with no turn of its own yet. `_dispatch_prompt`, inside it, marks
        # the session busy on the way out — if `pending` above just did
        # exactly that, this is a no-op (`_drain_queue` checks `busy`
        # first) and the restored queue waits its turn like any other.
        self._drain_queue(session_id)

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
        # The agent's side of the exchange, on disk the moment it lands —
        # otherwise a hang on the artist's NEXT prompt would cost this
        # whole answer too, not just the one that never came back. See
        # `_persist_conversations_soon`.
        self._persist_conversations_soon()
        # This turn's own drain point: if something was typed while it was
        # running, its turn has come. After the persist above, not before —
        # the turn that just finished gets written to disk as itself before
        # anything about the NEXT one starts.
        self._drain_queue(session_id)

    def _on_error(self, session_id: str, message: str) -> None:
        if self._pending_logout_agent == self._agent_id:
            # A `logout()` requested from a Settings row (rather than the
            # sign-in screen's own button) has no screen guaranteed to be
            # open when its answer arrives — the artist may already be back
            # in Settings. Record it either way, and only ALSO show it on
            # the auth screen if that's genuinely where it's being watched.
            self._pending_logout_agent = None
            self._record_auth_attempt(self._agent_id, action="sign_out", ok=False, message=message)
            if self._pages.currentIndex() == self.PAGE_AUTH:
                self._auth_view.show_error(message, self._last_auth_method)
            else:
                self._note(f"Sign out failed: {message}", error=True)
            return
        # A failure while the artist is on the sign-in screen has to appear
        # THERE. Reporting it into a feed they cannot see is the same as not
        # reporting it: the screen just sits, which is indistinguishable from
        # a login that quietly did nothing.
        if self._pages.currentIndex() == self.PAGE_AUTH:
            self._auth_pending = False
            self._auth_view.show_error(message, self._last_auth_method)
            self._record_auth_attempt(
                self._agent_id, action="sign_in", ok=False, message=message,
                method_id=self._last_auth_method,
            )
            return
        target = session_id or (self._current_session().session_id if self._current_session() else "")
        if not target:
            self._note(message, error=True)
            return
        entry = self._model(target).append_error(message)
        self._touch(target, entry.id)
        state = self._pool.get(target)
        if state is not None:
            # An error ends this turn as surely as `turn_finished` does —
            # no further completion is coming for it. Left set, this stuck
            # `busy` forever blocked `_drain_queue` for a queue behind a
            # turn that failed instead of finishing cleanly (found while
            # wiring the queue through every way a turn can end, not just
            # the tidy one).
            state.busy = False
        if self._is_current(target):
            self._composer.set_busy(False)
        self._finish_activity(target)
        self._persist_conversations_soon()
        self._drain_queue(target)

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
        pool_sessions = self._pool.all()
        if not pool_sessions:
            # Only computed for the rare case that matters — see
            # `_compute_empty_scope_text`'s own docstring for the cost accounting
            # this gate exists for.
            self._conversations.set_empty_scope_text(self._compute_empty_scope_text())
        self._conversations.set_sessions(
            pool_sessions, current.session_id if current else None
        )

    def _compute_empty_scope_text(self) -> str:
        """What the drawer should say with nothing to show, for THIS tab's
        current agent and folder — reported for real, twice: the owner
        opened a scene, saw one empty drawer, and read a CORRECT absence
        as data loss both times (dumping the store: 41 conversations in
        that folder belonged to a different agent; the 2 that belonged to
        THIS one lived in a different folder entirely). Naming both
        filters is the fix `conversations.empty_scope_text` does; this is
        only the part that gathers what it needs to do that.

        One combined, unfiltered `conversations_store.load()` — not two
        scoped ones — measured ~25-30ms at the store's own worst case (50
        conversations x 400 entries): cheap for the one call `_refresh_
        sessions` makes it from (only when the list is ALREADY empty, not
        on every refresh), wasteful to pay twice for filters that can both
        be checked in memory off the one read.
        """
        agent_label = self._display_label(self._agent_id) if self._agent_id else ""
        if not agent_label:
            return empty_scope_text("")
        here = scene.hip_dir()
        try:
            from .. import conversations_store as store

            all_conversations = store.load()
        except Exception:  # noqa: BLE001 - a missing hint is not worth breaking the drawer
            return empty_scope_text(agent_label)
        other_agents_here = sum(
            1
            for c in all_conversations
            if c.cwd == here and c.agent_id and c.agent_id != self._agent_id
        )
        this_agent_elsewhere = sum(
            1
            for c in all_conversations
            if c.agent_id == self._agent_id and c.cwd and c.cwd != here
        )
        return empty_scope_text(
            agent_label,
            other_agents_here=other_agents_here,
            this_agent_elsewhere=this_agent_elsewhere,
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
        diagnosis we know is wrong — routed through `_offer_login_command`,
        not a second copy of its advice: this used to say "type /login"
        unconditionally, the exact assumption that method itself was fixed
        NOT to make (`claude-acp` measured reporting an empty command
        list — see its own docstring). There is no live session yet at
        this point (`session/new` is what stalled), so `_has_login_
        command()` can never confirm one either way here — which is
        exactly why deferring to the shared, measured `_no_methods_advice`
        fallback is the honest answer, not a regression: a blanket "type
        /login" was never more than a guess in this branch to begin with.
        """
        self._composer.cancel_boot()
        if self._closed:
            return
        if {state.session_id for state in self._pool.all()} - before:
            return  # the agent answered, nothing to complain about

        client = shared_client(self._agent_id)
        info = client.agent_info()
        if info is not None and not info.auth_methods:
            self._offer_login_command(info)
            return
        self._note(
            "The agent hasn't opened a new conversation. It may be busy or "
            "stuck — try switching agents in the header, or restart it from "
            "settings.",
            error=True,
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
            # On disk before it is sent anywhere: this is the artist's own
            # typed words, the one part of a conversation nothing can
            # reconstruct if the turn that follows never comes back (a hang,
            # a wedged agent, a crash). See `_persist_conversations_soon`.
            self._persist_conversations_soon()
        self._dispatch_prompt(current.session_id, blocks)

    def _dispatch_prompt(self, session_id: str, blocks: list[dict]) -> None:
        """Actually send blocks to the agent and mark the session busy.

        The shared tail of two very different moments: "type and press
        send" (`_on_submitted`, always the CURRENT session) and "the turn
        ahead of this queued message just finished, so its own turn has
        come" (`_drain_queue`, which may be draining a session that isn't
        even the one on screen right now — the artist could have switched
        tabs while it was waiting). `_is_current` is what tells the two
        apart: the composer only ever reflects the ONE session on screen.
        """
        state = self._pool.get(session_id)
        if state is not None:
            state.busy = True
        if self._is_current(session_id):
            self._composer.set_busy(True)
            self._composer.trigger_buddy()
        activity = self._model(session_id).start_activity()
        self._touch(session_id, activity.id)
        shared_client(self._agent_id).prompt(session_id, blocks)

    # --- the queue: typed while busy, sent one turn at a time --------------

    def _on_enqueue_requested(self, blocks: list) -> None:
        """The composer decided this should wait, not go now — busy (not
        blocked) at the moment send was pressed. Queued on the conversation
        itself (`sessions.SessionState.queued`), never anywhere keyed by
        panel/tab: switching to a different conversation while this one is
        still working must not carry these typed words along, or leave
        them showing up in the wrong one.
        """
        current = self._current_session()
        if current is None or current.session_id.startswith(_RESTORED_PREFIX):
            # Nothing live and busy to queue behind — this is the same
            # situation `_on_submitted` already knows how to open a session
            # for, so let it, rather than queuing behind nothing.
            self._on_submitted(blocks)
            return
        text = " ".join(
            block.get("text", "") for block in blocks if block.get("type") == "text"
        ).strip()
        import uuid as _uuid

        entry_id = str(_uuid.uuid4())
        if text:
            # Attachment-only messages get no transcript entry here, same
            # as a direct send (`_on_submitted` above) — the blocks still
            # queue and will still be sent, they just have nothing to show
            # in a feed that has never rendered a textless user message.
            entry = self._model(current.session_id).queue_message(entry_id, text)
            self._touch(current.session_id, entry.id)
        current.queued.append(sessions.QueuedMessage(id=entry_id, blocks=list(blocks)))
        self._pool.mark_changed(current.session_id)
        # The artist's own words, on disk the instant they exist — a queued
        # message lost to a hang before its turn ever comes is exactly the
        # bug `_persist_conversations_soon` was written for.
        self._persist_conversations_soon()

    def _drain_queue(self, session_id: str) -> None:
        """The next queued message's turn has come.

        One at a time, oldest first — never the whole backlog at once: each
        queued message is its own separate turn, the same as if the artist
        had waited for each answer and typed the next one by hand. Does
        nothing if the session is busy (another turn is already running —
        it will call back here when IT finishes) or the queue is empty.
        """
        state = self._pool.get(session_id)
        if state is None or state.busy or not state.queued:
            return
        queued = state.queued.pop(0)
        entry = self._model(session_id).promote_queued(queued.id)
        if entry is not None:
            self._touch(session_id, entry.id)
        self._pool.mark_changed(session_id)
        self._persist_conversations_soon()
        self._dispatch_prompt(session_id, queued.blocks)

    def _on_queue_remove_requested(self, entry_id: str) -> None:
        """The artist pulled a still-waiting message back out — the one
        thing about a queue that has to work, per the owner's own ask.
        Only ever reachable for the CURRENT session: the remove button
        lives on a transcript row, and only the session on screen has any
        rows drawn at all."""
        current = self._current_session()
        if current is None:
            return
        current.queued = [q for q in current.queued if q.id != entry_id]
        if self._model(current.session_id).remove_entry(entry_id):
            self._touch(current.session_id, entry_id)
        self._pool.mark_changed(current.session_id)
        self._persist_conversations_soon()

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
        if current.queued:
            # Visible at the moment of the decision, not discovered later
            # as a surprise once the next queued message quietly goes out
            # on its own: the queue is kept, not silently dropped, and
            # cancelling this turn is what lets the FIRST of them start.
            self._note(
                f"Stopping — {len(current.queued)} queued message(s) will "
                "still be sent, one at a time, once this turn ends."
            )
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
        queue_note = (
            f" {len(state.queued)} queued message(s) will still be sent."
            if state.queued
            else ""
        )
        entry = self._model(session_id).append_error(
            "The agent did not acknowledge the stop. Input is unlocked; "
            "start a new conversation if it stays unresponsive." + queue_note
        )
        self._touch(session_id, entry.id)
        self._drain_queue(session_id)

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
        # A self-update is running, or one just finished and Houdini hasn't
        # restarted since — either way the notice strip is already saying
        # something this panel needs the artist to keep seeing. An agent-
        # update banner or an announcement arriving from a periodic refresh
        # that happens to land in the middle must not silently replace it:
        # mid-update that would erase the ONLY progress indicator there is;
        # afterwards it reads as the restart reminder being resolved when
        # it never was. There is only one notice strip to show either in.
        # Registry entries still update underneath it (the settings
        # screen's own agent rows need them regardless), only the STRIP is
        # held.
        if self._panel_update_worker is not None or self._panel_update_restart_pending is not None:
            if entries:
                self._registry_entries = list(entries)
            self._refresh_agent_chip_menu()
            return
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
                + (f"\n{reason}" if reason else ""),
                error=True,
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
        # `_maybe_offer_sign_in`'s own notice, on the same strip and the
        # same signal — its id is namespaced (`_SIGNIN_OFFER_PREFIX`) so it
        # can never collide with a real `Announcement.id` or an update
        # target, and it goes through `_offer_sign_in` (the existing,
        # already-working entry point Settings itself uses), never
        # `_open_url`/`_remember_seen` — there is no URL, and this isn't a
        # feed announcement to remember having seen.
        if identifier.startswith(_SIGNIN_OFFER_PREFIX):
            self._notice.hide_notice()
            self._offer_sign_in()
            return
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
        if identifier.startswith(_SIGNIN_OFFER_PREFIX):
            # "Not now," not "never" — kept in-memory, per tab, per the
            # owner's own ask (`_dismissed_signin_offers`'s own docstring).
            # Never `_remember_seen`: that persists to `settings.seen_
            # announcements`, forever, which is exactly the wrong shape here.
            self._dismissed_signin_offers.add(identifier[len(_SIGNIN_OFFER_PREFIX):])
            return
        if self._active_update is not None and self._active_update.target == identifier:
            self._active_update = None
            return  # an update dismissal isn't an announcement id — nothing to remember
        pending = self._panel_update_restart_pending
        if pending is not None and _panel_update_notice_id(pending) == identifier:
            # Dismissed on purpose, not resolved — closing the ✕ doesn't
            # mean Houdini got restarted. The artist saw it once and chose
            # to move on; that's their call, same as any other notice. It
            # will not come back on its own (`_on_refresh_done` only
            # RE-shows it, never re-fires it after a dismissal) — matching
            # every other announcement's own dismiss-is-final shape.
            self._panel_update_restart_pending = None
            return  # not a real announcement id either — nothing to remember
        self._remember_seen(identifier)

    def _start_update(self, update: Any) -> None:
        """The notice strip's "Update" button, actually doing something.

        Agents update through `AgentsView`/`runtime.install_agent` already
        (a subprocess of its own, on `_InstallWorker`'s thread) — this
        branch is `update.kind` "panel"/"fx", a package this SAME process
        is running from. It used to just tell the artist the command to
        type by hand, reasoning that a process cannot safely rewrite the
        tree it imported itself from. That half is true; the conclusion
        was too cautious.

        Measured before this changed (both Houdini installs, macOS and
        Linux, a real `hython` with `pydantic_core` actually imported and
        a model actually validated, not just constructed): `pip install
        --upgrade --target <tree>` against a tree the running process has
        ALREADY loaded from succeeds, and the process survives — POSIX
        lets you unlink and rewrite a file that's open or mapped, and an
        already-imported module stays exactly as it was in memory. So the
        update itself is safe to run automatically; running it IN this
        process is what would not be (`self_update.py`'s own docstring has
        the rest, including what does NOT survive: a module this process
        had not imported yet at the moment the tree changes).

        So `_start_update` now runs it for real, in a separate process
        (`SelfUpdateWorker`, off this thread) — the exact command that used
        to be the manual advice, `uvx --refresh --from <target>==<version>
        python -m houdini_agent_panel install`, just run by the panel
        instead of typed by the artist. The manual command is still what's
        offered on failure (`_on_panel_update_failed`), now a fallback
        rather than the only route. `update.latest` is what fills in
        `<version>` — see `self_update.py`'s own docstring for why that
        pin is load-bearing, not just tidiness.
        """
        if update.kind != "agent":
            if self._panel_update_worker is not None:
                return  # already running — a second click while it's in flight is a no-op, not a second update
            from .self_update import SelfUpdateWorker

            self._panel_update_worker = SelfUpdateWorker(update.target, update.latest, parent=self)
            self._panel_update_worker.progressed.connect(
                lambda line, u=update: self._on_panel_update_progressed(u, line)
            )
            self._panel_update_worker.succeeded.connect(
                lambda u=update: self._on_panel_update_succeeded(u)
            )
            self._panel_update_worker.failed.connect(
                lambda message, u=update: self._on_panel_update_failed(u, message)
            )
            self._active_update = None
            self._panel_update_started_at = time.monotonic()
            self._panel_update_display_line = "starting…"
            tick_timer = QtCore.QTimer(self)
            # Roughly once a second — fast enough that a stalled stretch
            # (`hython` itself starting, no output of its own for as long
            # as 16s) never sits still for more than a moment, slow enough
            # that it never competes with a real line arriving.
            tick_timer.timeout.connect(lambda u=update: self._render_panel_update_notice(u))
            tick_timer.start(1000)
            self._panel_update_tick_timer = tick_timer
            self._render_panel_update_notice(update)
            self._panel_update_worker.start()
            return
        self._show_page(self.PAGE_SETTINGS)
        self._settings_view.focus_agents()
        if not self._settings_view.trigger_agent_update(update.target):
            self._note(f"Could not find {update.label} to update — try Settings → Agents.", error=True)

    def _on_panel_update_progressed(self, update: Any, line: str) -> None:
        # `SelfUpdateWorker` already logs every line it receives, argv
        # included (see its own docstring) — this only decides what's
        # worth putting in front of the artist. An administrative line
        # (`_PANEL_UPDATE_ADMIN_PREFIXES`) is dropped from the DISPLAY,
        # not the log: it doesn't read as progress, and it used to sit on
        # screen unchanged for as long as the next real line took to
        # arrive. `_panel_update_tick_timer` is what keeps the strip
        # visibly alive through a stretch like that either way.
        if not line.startswith(_PANEL_UPDATE_ADMIN_PREFIXES):
            self._panel_update_display_line = line
        self._render_panel_update_notice(update)

    def _render_panel_update_notice(self, update: Any) -> None:
        elapsed = ""
        if self._panel_update_started_at is not None:
            elapsed = f" ({int(time.monotonic() - self._panel_update_started_at)}s)"
        self._notice.show_notice(
            Announcement(
                id=f"panel-update-progress:{update.target}",
                severity="info",
                title=f"Updating {update.label}… {self._panel_update_display_line}{elapsed}",
            )
        )

    def _stop_panel_update_tick(self) -> None:
        if self._panel_update_tick_timer is not None:
            self._panel_update_tick_timer.stop()
            self._panel_update_tick_timer = None
        self._panel_update_started_at = None
        self._panel_update_display_line = ""

    def _on_panel_update_succeeded(self, update: Any) -> None:
        self._stop_panel_update_tick()
        self._panel_update_worker = None
        self._panel_update_restart_pending = update
        self._show_panel_update_restart_notice()

    def _show_panel_update_restart_notice(self) -> None:
        update = self._panel_update_restart_pending
        if update is None:
            return
        self._notice.show_notice(
            Announcement(
                id=_panel_update_notice_id(update),
                severity="info",
                title=(
                    f"Updated {update.label} to {update.latest} — takes effect after "
                    "Houdini restarts. Starting a different agent for the first time "
                    "before then isn't recommended: some of the panel's own code may "
                    "now be a mix of old and new."
                ),
            )
        )

    def _on_panel_update_failed(self, update: Any, message: str) -> None:
        self._stop_panel_update_tick()
        self._panel_update_worker = None
        # `message` is already classified (`self_update._classify_failure`)
        # — a download failure, a write failure (the Windows sharing-
        # violation case above all), or the manual command if `uv` itself
        # couldn't be found. Whichever it is, it goes to the feed: unlike
        # success, a failure doesn't need to keep being said once the
        # artist has read it, and the ORIGINAL offer comes back so trying
        # again is one click, not a re-explanation.
        self._note(f"Updating {update.label} failed.\n{message}", error=True)
        if self._active_update is None:
            self._active_update = update
        self._notice.show_update(update)

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
        self._note(f"Could not update {self._display_label(agent_id)}: {message}", error=True)

    def _on_blocking_action(self, announcement_id: str, url: str) -> None:
        self._open_url(url)
        self._remember_seen(announcement_id)
        self._blocking.hide_notice()
        self._composer.unblock_input()

    def _open_url(self, url: str) -> None:
        if not url:
            return
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
        if self._auth_pending and self._pages.currentIndex() == self.PAGE_AUTH:
            # The only thing SOME agents ever say while a sign-in is
            # pending — gemini's `oauth-personal` never emits anything
            # else at all (docs/facts/acp-sdk.md §13: "Failed to
            # authenticate with authorization code:invalid_grant" /
            # "Failed to authenticate with user code. Retrying..."), and
            # neither line matches a `_FATAL_STDERR_MARKERS` entry below —
            # without this branch it went nowhere the artist could ever
            # see. Shown right beside the wait itself, replaced on each new
            # line (`AuthView.set_pending_detail`), not appended to the
            # transcript feed the artist may not be looking at.
            self._auth_view.set_pending_detail(line.strip())
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
                "agent again:\n    rm -rf ~/.npm/_npx",
                error=True,
            )
            return
        if "authorizationrequired" in lowered.replace(" ", ""):
            self._note(
                "The agent says it is not signed in. Open the ⋯ menu and pick Sign in."
            )
            self._offer_sign_in()
            return
        # Trimmed: agents put timestamps and ANSI colour in stderr, and the
        # useful part is the tail. Reached only when a fatal marker matched
        # and neither of the two named causes above did — still a real
        # problem, just not one this panel recognises by name.
        self._note(f"Agent: {line.strip()[-200:]}", error=True)

    def _offer_sign_in(self) -> None:
        """Show the sign-in screen using the methods `initialize` gave us.

        Needed because `auth_required` is not guaranteed: an agent may accept
        a session and fail authorization later, out of band. The methods are
        known from `initialize` either way, so signing in never has to depend
        on the agent asking first.
        """
        info = shared_client(self._agent_id).agent_info()
        if info is None:
            return
        if self._pages.currentIndex() == self.PAGE_AUTH:
            return
        if not info.auth_methods:
            # Reported for real: Claude Agent's Settings row could be
            # clicked, and clicking it did nothing — this used to return
            # right here. Zero methods is not the same as nothing to do
            # (`_no_methods_advice`); the row must lead SOMEWHERE.
            self._offer_sign_in_with_no_methods(info)
            return
        self._auth_view.set_methods(
            list(info.auth_methods), can_logout=self._can_sign_out(info)
        )
        self._show_page(self.PAGE_AUTH)

    def _offer_sign_in_with_no_methods(self, info: Any) -> None:
        """The way in from Settings for an agent that lists no sign-in
        methods at all.

        If a live session already knows about a real `/login` command,
        that's the more specific, already-established answer
        (`_offer_login_command`, also reached automatically from
        `auth_required`). Next, a built-in recipe the PANEL supplies
        itself (Claude's `setup-token`, `_builtin_terminal_auth_method`) —
        real and spawnable even though the agent advertised nothing.
        Otherwise this shows the same static advice directly on the
        sign-in screen — Settings just sent the artist here, they should
        not need to already be mid-conversation and hit a failure first
        to see it.
        """
        if self._has_login_command():
            self._offer_login_command(info)
            return
        builtin = self._builtin_terminal_auth_method(self._agent_id)
        if builtin is not None:
            self._auth_view.set_methods([builtin], can_logout=False)
            self._show_page(self.PAGE_AUTH)
            return
        self._auth_view.set_methods(
            [], can_logout=False, no_methods_help=self._no_methods_advice()
        )
        self._show_page(self.PAGE_AUTH)

    #: A terminal-auth recipe the PANEL supplies itself for an agent that
    #: advertises NO auth methods at all — claude-acp's own `initialize`
    #: reports an empty list (measured), so this can never come from
    #: `client._terminal_auth_from`, which only ever reads the wire. Real
    #: and spawnable all the same (docs/facts/acp-sdk.md §14, verbatim):
    #: `claude setup-token` opens a browser, prints an OAuth URL, and
    #: blocks at "Paste code here if prompted >" for exactly one line
    #: back. Written here as DATA, deliberately — the same rule `client.
    #: _terminal_auth_from` already follows for opencode's identical-
    #: looking prose (never scrape a description for a command): this is
    #: the one place the panel is allowed to invent a command, because
    #: it's the PANEL'S OWN knowledge about a specific, named agent id,
    #: never a guess about what some other agent's sentence means.
    _BUILTIN_TERMINAL_AUTH_IDS = frozenset({"claude-setup-token"})

    def _builtin_terminal_auth_method(self, agent_id: str) -> Any:
        """The order here is deliberate, cheapest and most reliable first:

        1. A `claude` already on PATH — skips npx's own fetch entirely,
           measured to happen BEFORE the CLI prints anything at all (seven
           TCP connections first, §14), and the single slowest, least
           reliable part of this on a bad connection.
        2. The bundled binary `claude-agent-acp` already downloaded to run
           the agent itself — `claude-agent-acp` bundles the real Claude
           CLI through `@anthropic-ai/claude-agent-sdk-<platform>`, so
           once ANY conversation with this agent has ever started, on
           THIS machine, the exact binary `setup-token` needs is already
           sitting on disk, fetched once already. Confirmed to behave
           identically to the standalone package (live run, docs/facts/
           acp-sdk.md §19): same "Welcome to Claude Code", same OAuth
           URL, same "Paste code here" prompt. This can't be decided HERE
           though — finding it means a filesystem search plus actually
           running `--version` on each candidate to confirm it isn't a
           half-downloaded or wrong-architecture leftover
           (`node.find_cached_npx_binary`'s own docstring), measured at
           ~1.7s on this Mac — an eternity on the main thread building
           this method right now, so it's deferred: this still returns
           the npx fallback below as a placeholder, and `_start_terminal_
           login` attaches a resolver that tries this off `TerminalLogin
           Worker`'s own thread before anything is actually spawned.
        3. `npx --yes @anthropic-ai/claude-code setup-token` — fetches
           the whole CLI fresh. Measured on the owner's own machine (a
           bad link): this package alone is ~282 MB, and at ~21 KB/60s
           that never finishes, not just "slow" — the only one of the
           three that can look exactly like a hang.
        """
        if agent_id != "claude-acp":
            return None
        claude_on_path = shutil.which("claude")
        if claude_on_path:
            command, args = claude_on_path, ["setup-token"]
        else:
            command, args = "npx", ["--yes", "@anthropic-ai/claude-code", "setup-token"]
        return acp_client.AuthMethod(
            id="claude-setup-token",
            name="Sign in with browser",
            description=(
                "Opens `claude setup-token` — it signs in through your "
                "browser and writes ~/.claude/.credentials.json. Prefer "
                "ANTHROPIC_API_KEY in your shell profile instead if you "
                "already have one: no flow needed, just restart the "
                "agent from Settings."
            ),
            terminal_auth=acp_client.TerminalAuth(command=command, args=args, env={}),
        )

    def _find_auth_method(self, method_id: str) -> Any:
        """Wire methods first (`agent_info().auth_methods`), then the
        panel's own built-in recipe — Claude's `setup-token` isn't
        advertised by the agent at all, so it would never be found the
        first way."""
        info = shared_client(self._agent_id).agent_info()
        for m in (info.auth_methods if info is not None else ()):
            if m.id == method_id:
                return m
        builtin = self._builtin_terminal_auth_method(self._agent_id)
        if builtin is not None and builtin.id == method_id:
            return builtin
        return None

    #: What each measured sign-in method actually does, so the panel can say
    #: it instead of leaving the artist watching a button. Keyed by the
    #: method id the agent advertises; anything unknown gets the generic
    #: line. Measured on a clean HOME (docs/facts/acp-sdk.md §12).
    _AUTH_ADVICE = {
        # Plain `authenticate()` wait, NOT a terminal-auth spawn, and
        # deliberately not given the paste-back treatment Claude's
        # `setup-token` gets (`_on_terminal_login_input_requested`):
        # Codex's own OAuth redirects to `http://localhost:1455/auth/
        # callback` — it completes itself the moment the browser is
        # approved, with nothing for the artist to copy back. Claude's
        # equivalent has no local callback at all, which is exactly why
        # IT needs the artist to paste a code. Two different agents
        # solving the same OAuth problem two different ways — do not
        # "unify" them into the same code path later.
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
        # Kimi's own auth method id (`auth_required: ['login']`,
        # docs/facts/acp-sdk.md §8) — falls back to this ONLY if the agent's
        # own `description` is empty (`_auth_advice_for` prefers that first).
        # Real Kimi never hits this: its method carries a description
        # ("Run `kimi login` command in the terminal…", §13/§14) that is
        # both more precise than this guess and guaranteed not to go stale.
        # Like `chat-gpt`, `authenticate` stays pending indefinitely rather
        # than returning quickly — but unlike `chat-gpt`'s browser, Kimi's
        # `login` isn't answered over the ACP channel at all; it wants a
        # SECOND, separate `kimi login` process the panel doesn't spawn yet
        # (§13 consequence 3) — hence "check its own command-line window"
        # rather than promising a browser that will never open here.
        "login": (
            "Waiting for the agent to finish signing in — this can take a "
            "few seconds. If nothing opens in Houdini, this agent may be "
            "expecting it in its OWN command-line window instead; check "
            "there. The panel is watching either way and will move on the "
            "moment it succeeds."
        ),
    }
    #: The fallback for any method id not in `_AUTH_ADVICE` above — still
    #: names both places the rest of a browser-based sign-in could be
    #: happening (docs/facts/acp-sdk.md §12-13: Codex and grok both spawn a
    #: real browser process the client never sees a URL from; gemini emits
    #: no URL at all, only a device code retried in stderr) — rather than
    #: assuming a browser is the only possibility just because that's the
    #: most-measured case.
    _GENERIC_AUTH_ADVICE = (
        "Signing in with {method}… if a browser window opens, finish it "
        "there; some agents complete sign-in in their own command-line "
        "tool instead. The panel is waiting either way."
    )
    #: Methods measured to return `authenticate` OK INSTANTLY without
    #: checking anything (docs/facts/acp-sdk.md §13: gemini's three
    #: environment-backed methods all did — "no validation happens at
    #: `authenticate` time — nothing was set in the environment for it to
    #: check"). `_on_authenticated` firing is real, but for these it is
    #: proof the CALL succeeded, not proof the credential works — reported
    #: for real: an artist read Gemini's normal "Signed in." as a green
    #: light, then hit "Could not load the default credentials" on the
    #: first prompt with no idea why a "successful" sign-in had failed.
    _UNVALIDATED_AUTH_METHODS = frozenset({"gemini-api-key", "vertex-ai", "gateway"})

    def _auth_advice_for(self, method_id: str) -> str:
        """What to say while `authenticate(method_id)` is in flight.

        The agent's OWN `description` for this method, if it bothered to
        set one, beats anything guessed here — this repo's rule is that the
        agent decides what exists, and a description is the agent talking
        directly to whoever is about to click. Measured proof this matters:
        Kimi's `login` describes itself as "Run `kimi login` command in the
        terminal, then follow the instructions to finish login."
        (docs/facts/acp-sdk.md §13) — more precise than any static guess,
        and it can never go stale the way a hardcoded id-keyed table would
        if Kimi changed how its own login worked. `_AUTH_ADVICE`/`_GENERIC_
        AUTH_ADVICE` only fill in for methods that describe themselves with
        nothing at all — measured true of `chat-gpt`/`api-key`/`grok.com`.
        """
        info = shared_client(self._agent_id).agent_info()
        if info is not None:
            for method in info.auth_methods:
                if method.id == method_id and method.description:
                    return method.description
        return self._AUTH_ADVICE.get(
            method_id, self._GENERIC_AUTH_ADVICE.format(method=method_id)
        )

    def _on_auth_method_chosen(self, method_id: str) -> None:
        self._last_auth_method = method_id
        method = self._find_auth_method(method_id)
        if method is not None and method.terminal_auth is not None and method.terminal_auth.command:
            # This method isn't answered over the ACP channel at all — see
            # `_start_terminal_login`'s own docstring (Kimi, docs/facts/
            # acp-sdk.md §13-14). Skip `authenticate()` entirely; calling it
            # anyway would just hang forever for no reason (measured: it
            # never returns).
            #
            # The built-in recipe's OWN description (ANTHROPIC_API_KEY as
            # the simpler alternative) is written FOR this spawned flow and
            # should replace the generic pending text; a wire method's own
            # description (Kimi's "run this yourself…") is written for the
            # opposite case and must not (`_start_terminal_login`'s own
            # docstring says why).
            override = method.description if method_id in self._BUILTIN_TERMINAL_AUTH_IDS else ""
            self._start_terminal_login(method, message_override=override)
            return
        message = self._auth_advice_for(method_id)
        self._note(message)
        # On screen too, not only in a feed the artist may have already
        # scrolled past by the time a browser actually appears (issue #33):
        # before this, the sign-in screen went quiet the instant a method
        # was picked, and a Codex login that was genuinely working looked
        # identical to a Kimi one stuck for some other reason — both
        # silence. `set_pending` also disables the buttons for the wait;
        # `AuthView.cancel_pending`/`_on_auth_cancel_pending` is the way
        # back if the artist gives up watching.
        self._auth_pending = True
        self._auth_view.set_pending(message)
        shared_client(self._agent_id).authenticate(method_id)

    def _start_terminal_login(self, method: Any, *, message_override: str = "") -> None:
        """Spawn the SEPARATE process `method.terminal_auth` points at, and
        read its output for a verification URL.

        Measured for real on Kimi (docs/facts/acp-sdk.md §13-14):
        `authenticate(methodId="login")` never returns — not because it is
        slow, but because this method isn't asking the ACP channel for
        anything. Its `initialize` response says so directly: "Run `kimi
        login` command in the terminal, then follow the instructions to
        finish login." Running that command ourselves (in a pipe the
        panel reads, not a human types into) and parsing its output is what
        turns "check your terminal" into an actual clickable link — the one
        agent measured where that's possible at all (§14; gemini and grok
        both hang on an OAuth method with no URL ever crossing the wire,
        and opencode's own command opens an interactive arrow-key menu no
        subprocess reader can drive).

        `method` need not come from the wire at all — `_offer_sign_in_
        with_no_methods` builds one for Claude's `setup-token`, a recipe
        the panel supplies itself (`_builtin_terminal_auth_for`), since
        claude-acp advertises no methods for this to come from.
        """
        from .terminal_login import TerminalLoginWorker

        ta = method.terminal_auth
        # A caller-supplied message (the Claude built-in recipe uses this
        # to mention ANTHROPIC_API_KEY as the simpler alternative) always
        # wins; otherwise the generic line. Deliberately NOT `method.
        # description` — Kimi's own description ("Run `kimi login`
        # command in the terminal…") is written for an artist running it
        # THEMSELVES, and would read as wrong now that the panel spawns it
        # instead (`AuthView` still shows that text as the button's
        # tooltip via `set_methods`, which is the right amount of it).
        message = message_override or (
            f"Opening {method.name} in a terminal the panel manages — this "
            f"can take a moment. If it prints a sign-in link, it will "
            f"appear here."
        )
        self._note(message)
        self._auth_pending = True
        # Whether `url_found` has fired yet THIS attempt — read by
        # `_on_terminal_login_exited` to tell "it printed a link and then
        # ended" (nothing more to say) from "it never did" (fall back to
        # the raw command, per §14: the `Verification URL:` line was
        # sampled once, with no format contract, so a future version — or
        # a different agent's terminal-auth command entirely — printing
        # something this regex doesn't recognise must never be a dead end).
        self._terminal_login_url_shown = False
        self._terminal_login_command = " ".join([ta.command or "", *ta.args])
        # Whether ANYTHING has printed yet — tells a fetch/start that never
        # got off the ground apart from a real authentication failure (see
        # `_terminal_login_no_output_message`'s own docstring).
        self._terminal_login_got_output = False
        self._terminal_login_input_requested_seen = False
        # A belt-and-suspenders check alongside `_stop_terminal_login`'s
        # signal-disconnect: Qt does not retract an already-QUEUED cross-
        # thread signal delivery just because `disconnect()` ran before it
        # was processed — a line already on its way from the worker thread
        # at the exact moment of a switch can still arrive after. Each
        # handler below checks this against `self._agent_id` and ignores
        # anything that no longer matches, so even that narrow leftover
        # race can't paint a stale result onto a different agent's screen.
        self._terminal_login_agent_id = self._agent_id
        self._auth_view.set_pending(message)

        # `claude-setup-token`'s own npx placeholder (`_builtin_terminal_
        # auth_method`'s own comment has the full reasoning) gets one more
        # chance before actually running: `claude-agent-acp` may have
        # already downloaded the real Claude binary just to run the agent
        # itself, sitting in npx's own cache — worth a real (if not
        # instant) look before a ~282 MB fetch that a bad connection may
        # never finish. Only attached for exactly that placeholder, never
        # for `claude` already found on PATH or for another agent's own
        # `terminal_auth` (Kimi's `kimi login` needs no such thing).
        resolve_command = None
        if method.id == "claude-setup-token" and ta.command == "npx":
            resolve_command = self._resolve_claude_terminal_command

        # `claude-setup-token`'s own binary (bundled or npx-resolved, either
        # way) prints nothing at all over plain pipes — it wants a real
        # controlling terminal and stays silent without one (measured on
        # mayfx02, §20: a live run sat with no output at all after the
        # owner completed the browser step). A pty fixes that; scoped to
        # this one method only, same as `resolve_command` above — Kimi's
        # own `kimi login` was measured unaffected by a pty and already
        # works over plain pipes, so it keeps the pipe path unchanged.
        use_pty = method.id == "claude-setup-token"

        worker = TerminalLoginWorker(
            self._agent_id,
            ta,
            cwd=scene.hip_dir(),
            parent=self,
            resolve_command=resolve_command,
            use_pty=use_pty,
        )
        worker.line_received.connect(self._on_terminal_login_line)
        worker.url_found.connect(self._on_terminal_login_url)
        worker.input_requested.connect(self._on_terminal_login_input_requested)
        worker.exited.connect(self._on_terminal_login_exited)
        worker.failed.connect(self._on_terminal_login_failed)
        worker.command_resolved.connect(self._on_terminal_login_command_resolved)
        self._terminal_login_worker = worker
        worker.start()

        # Informational only — never kills anything (unlike `authenticate`,
        # this process is genuinely ours to poll, not to time out). npx's
        # own fetch happens BEFORE the CLI prints a byte (§14: 7 TCP
        # connections first), and on a connection that needs a proxy but
        # doesn't have a working one, that fetch can hang for a very long
        # time rather than fail cleanly (measured on the owner's own
        # machine: ~21KB of 48KB in 60s direct, half a second through the
        # proxy) — silence that long looks exactly like a dead screen.
        timer = QtCore.QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda aid=self._agent_id: self._on_terminal_login_slow(aid))
        timer.start(15000)
        self._terminal_login_slow_timer = timer

        # The second, longer timer — see `_TERMINAL_LOGIN_STUCK_MS`'s own
        # comment for the report this answers. Independent of the one
        # above: this fires even if SOME output already arrived (the
        # owner's own case — a URL was found, a browser opened and
        # finished), as long as nothing conclusive has happened since.
        stuck_timer = QtCore.QTimer(self)
        stuck_timer.setSingleShot(True)
        stuck_timer.timeout.connect(lambda aid=self._agent_id: self._on_terminal_login_stuck(aid))
        stuck_timer.start(_TERMINAL_LOGIN_STUCK_MS)
        self._terminal_login_stuck_timer = stuck_timer

    @staticmethod
    def _resolve_claude_terminal_command() -> tuple[str, list[str]] | None:
        """Runs on `TerminalLoginWorker`'s own thread, never the main one
        — see `_builtin_terminal_auth_method`'s own comment for why this
        can't be decided synchronously (measured: ~1.7s on this Mac to
        search and verify). `staticmethod` and no `self` touched at all,
        deliberately: this only reads the filesystem and runs a
        subprocess, nothing that needs Qt's main-thread affinity, and
        keeping it that way is what makes it safe to call from here.

        `None` — nothing found, or nothing that actually runs (a
        half-finished download, a stale wrong-architecture leftover) —
        means keep the npx placeholder `_builtin_terminal_auth_method`
        already put in `terminal_auth`; this never invents a THIRD
        option, only possibly a better second one.
        """
        from .. import node as node_module

        bundled = node_module.find_cached_npx_binary(
            "@anthropic-ai", "claude-agent-sdk-", "claude"
        )
        if bundled is None:
            return None
        return str(bundled), ["setup-token"]

    def _on_terminal_login_command_resolved(self, command: str, args: list) -> None:
        """The worker found something better than the npx placeholder it
        started with (`_resolve_claude_terminal_command`, run off the main
        thread) — update the "run it yourself" fallback text
        (`_on_terminal_login_stuck`) so it names what's actually running,
        not the guess `_start_terminal_login` began with.
        """
        if self._terminal_login_agent_id != self._agent_id:
            return  # stale — see `_start_terminal_login`'s own comment
        self._terminal_login_command = " ".join([command, *args])

    def _on_terminal_login_line(self, line: str) -> None:
        if self._terminal_login_agent_id != self._agent_id:
            return  # stale — see `_start_terminal_login`'s own comment
        self._terminal_login_got_output = True
        if self._terminal_login_slow_timer is not None:
            self._terminal_login_slow_timer.stop()
            self._terminal_login_slow_timer = None
        if self._pages.currentIndex() == self.PAGE_AUTH:
            self._auth_view.set_pending_detail(line)

    def _on_terminal_login_slow(self, agent_id: str) -> None:
        self._terminal_login_slow_timer = None
        if agent_id != self._agent_id or self._terminal_login_worker is None:
            return
        if self._terminal_login_got_output:
            return  # something arrived in the meantime — nothing to say
        if self._pages.currentIndex() == self.PAGE_AUTH:
            self._auth_view.set_pending_detail(
                "Still working — this can take a while over a slow connection."
            )

    def _on_terminal_login_url(self, url: str, code: str) -> None:
        if self._terminal_login_agent_id != self._agent_id:
            return  # stale — see `_start_terminal_login`'s own comment
        self._terminal_login_url_shown = True
        self._note(f"Sign in at: {url}" + (f" (code {code})" if code else ""))
        if self._pages.currentIndex() == self.PAGE_AUTH:
            self._auth_view.set_terminal_login_link(url, code)

    def _on_terminal_login_stuck(self, agent_id: str) -> None:
        """Neither a real prompt nor the process ending has happened in
        `_TERMINAL_LOGIN_STUCK_MS` — see that constant's own comment for
        the report this answers. Unlike `_on_terminal_login_slow`, firing
        here does not mean nothing arrived; a URL may already be showing
        (the owner's own case: a browser tab that reached "you're all set
        up") and the child can still be sitting there regardless, waiting
        on something this panel never recognised. Says so explicitly and
        names the manual fallback — the Cancel button was already there,
        it just had no reason pointing at it before now.

        Reported for real, a second time (docs/facts/acp-sdk.md §19): this
        fired — confirmed, not assumed, by actually watching a shortened
        timer run — but was never SEEN, because it used to only write into
        `AuthView`'s own label, gated on the artist still being on
        PAGE_AUTH at the exact moment it fires. A download that looks
        stuck for over a minute is exactly the kind of wait someone
        navigates away from; the message was then computed and silently
        discarded, leaving no record anywhere that it ever happened. Now
        always reaches the feed too (`_note`, persists regardless of which
        page is showing when this fires) — the auth screen's own label
        stays as an extra, immediate copy for whoever IS still looking at
        it, not the only copy.
        """
        self._terminal_login_stuck_timer = None
        if agent_id != self._agent_id or self._terminal_login_worker is None:
            return
        if self._terminal_login_input_requested_seen:
            return  # already actionable — the artist has a field to use
        message = (
            "This is taking much longer than usual, and nothing recognisable "
            "has come back since. Press Cancel below and run it yourself in "
            f"a terminal instead:\n    {self._terminal_login_command}"
        )
        if self._pages.currentIndex() == self.PAGE_AUTH:
            self._auth_view.set_pending_detail(message)
        self._note(message, error=True)

    def _on_terminal_login_input_requested(self) -> None:
        """The child printed its own input prompt (Claude's `setup-token`,
        docs/facts/acp-sdk.md §14: "Paste code here if prompted >", or a
        newer build's own second shape, §18) — detected from ITS output,
        never from a timer."""
        if self._terminal_login_agent_id != self._agent_id:
            return  # stale — see `_start_terminal_login`'s own comment
        self._terminal_login_input_requested_seen = True
        if self._terminal_login_stuck_timer is not None:
            self._terminal_login_stuck_timer.stop()
            self._terminal_login_stuck_timer = None
        if self._pages.currentIndex() == self.PAGE_AUTH:
            self._auth_view.set_terminal_login_awaiting_input(True)

    def _on_terminal_login_input_submitted(self, text: str) -> None:
        """The artist pasted the code and submitted it — write it back to
        the child's stdin, the one thing Claude's `setup-token` needs to
        finish (§14). Not evidence of success: same as everywhere else in
        this flow, only a completed turn proves that."""
        worker = self._terminal_login_worker
        if worker is None or self._terminal_login_agent_id != self._agent_id:
            return
        worker.send_line(text)
        self._note("Code sent — waiting for the agent to finish.")

    def _terminal_login_fallback_message(self) -> str:
        """"No line → fall back to showing the command. Never a blank
        screen" — the format `_URL_RE` looks for was measured exactly once
        (docs/facts/acp-sdk.md §14), with no contract that it stays that
        way, so a run that never matches it is an expected outcome to
        handle, not a bug to fix by tightening the regex. Only used once
        the child has printed SOMETHING — see `_terminal_login_no_output_
        message` for the other case."""
        return (
            "This didn't produce a recognisable sign-in link. Run it "
            f"yourself in a terminal:\n    {self._terminal_login_command}"
        )

    def _terminal_login_no_output_message(self) -> str:
        """Nothing came out of the child AT ALL before it ended.

        Reported as the single most confusing failure mode the panel has:
        before the CLI prints a byte, `npx` does its own fetch over the
        network (measured: seven TCP connections open first, docs/facts/
        acp-sdk.md §14) — a proxy that's wrong, down, or simply not
        configured on a machine that needs one kills the whole attempt
        before any URL could ever exist, and from here that looks
        IDENTICAL to an authentication failure unless said explicitly.
        Names the proxy actually in use (sanitised — never the password)
        so the artist can tell at a glance whether Settings matches what
        they expect.
        """
        from .. import proxy as proxy_module

        address = proxy_module.effective_proxy(self._settings)
        proxy_text = proxy_module.sanitize(address) if address else "none configured"
        return (
            "Nothing came back at all — that usually means it couldn't "
            f"reach the network to start. Proxy currently in use: "
            f"{proxy_text}. Check Network in Settings, or run the "
            f"command yourself in a terminal:\n    {self._terminal_login_command}"
        )

    def _on_terminal_login_exited(self, exit_code: int) -> None:
        """The spawned process is gone — ended on its own, or `_stop_
        terminal_login` killed it.

        Not evidence of success OR failure: docs/facts/acp-sdk.md §14
        explicitly could not measure what a successful `kimi login` prints
        or exits with (the probe killed it first, deliberately), and this
        process never touches the ACP channel, so none of the agent's own
        signals (`authenticated`/`auth_required`) ever fire from it either.
        The one honest signal the rest of this file already relies on for
        every agent still applies here: a completed turn
        (`_remember_signed_in`, via `_on_turn_finished`).

        Checks the stale-worker guard BEFORE touching any state: a worker
        for an agent this tab has since left behind isn't just irrelevant
        to show — clearing `_terminal_login_worker`/`_auth_pending` here
        would wipe out whatever the CURRENT agent's own, newer attempt
        already set them to.
        """
        if self._terminal_login_agent_id != self._agent_id:
            return
        self._terminal_login_worker = None
        self._auth_pending = False
        if self._terminal_login_slow_timer is not None:
            self._terminal_login_slow_timer.stop()
            self._terminal_login_slow_timer = None
        if self._terminal_login_stuck_timer is not None:
            self._terminal_login_stuck_timer.stop()
            self._terminal_login_stuck_timer = None
        if self._pages.currentIndex() != self.PAGE_AUTH:
            return
        self._auth_view.set_terminal_login_awaiting_input(False)
        if not self._terminal_login_url_shown:
            message = (
                self._terminal_login_fallback_message()
                if self._terminal_login_got_output
                else self._terminal_login_no_output_message()
            )
            self._note(message, error=True)
            self._auth_view.set_pending(message)
            return
        # A real, live report (docs/facts/acp-sdk.md §20): the owner's own
        # browser page showed no code at all, just "you can close this
        # window" — a variant that never gives `_on_terminal_login_input_
        # submitted` anything to react to. "Terminal login process ended
        # (exit N)" alone would leave the artist with no idea whether that
        # actually worked. `signin_evidence.has_credential_evidence` is the
        # SAME check `_maybe_offer_sign_in` already uses at connect time —
        # reused here rather than inventing a second definition of "signed
        # in", and gated on a clean exit so a cancelled attempt on a machine
        # that happened to have older, unrelated credentials still gets the
        # neutral message below, not a false "Signed in.".
        #
        # Still not the LAST word either way — a completed turn
        # (`_remember_signed_in`, via `_on_turn_finished`) remains the one
        # signal the rest of this file treats as proof; this only replaces
        # an uninformative exit message with a genuinely checkable one.
        if exit_code == 0:
            env = shellenv.merged(dict(os.environ))
            if signin_evidence.has_credential_evidence(self._agent_id, env=env):
                message = "Signed in."
                self._note(message)
                self._auth_view.set_pending(message)
                return
        self._note(f"Terminal login process ended (exit {exit_code}).")

    def _on_terminal_login_failed(self, message: str) -> None:
        """`work()` raised before ever spawning anything readable — e.g. the
        command doesn't exist. Same fallback as a process that ran and
        said nothing useful: the artist still gets the exact command.

        Same stale-worker guard as `_on_terminal_login_exited`, first.
        """
        if self._terminal_login_agent_id != self._agent_id:
            return
        self._terminal_login_worker = None
        self._auth_pending = False
        if self._terminal_login_slow_timer is not None:
            self._terminal_login_slow_timer.stop()
            self._terminal_login_slow_timer = None
        if self._terminal_login_stuck_timer is not None:
            self._terminal_login_stuck_timer.stop()
            self._terminal_login_stuck_timer = None
        if self._pages.currentIndex() != self.PAGE_AUTH:
            self._note(f"Terminal login failed: {message}", error=True)
            return
        self._auth_view.show_error(message, self._last_auth_method)
        if not self._terminal_login_url_shown:
            fallback = (
                self._terminal_login_fallback_message()
                if self._terminal_login_got_output
                else self._terminal_login_no_output_message()
            )
            self._note(fallback, error=True)

    def _stop_terminal_login(self) -> None:
        """Ends whatever login was in progress in the spawned process —
        called when the artist cancels, leaves the sign-in screen, switches
        this tab to a different agent, or the panel closes. Not a
        courtesy: `kimi login` polls indefinitely on its own (§14), so
        leaving it running is a real leak, the same hazard `orphans.py`'s
        own module docstring describes for the agent process itself.

        Disconnects the worker's signals BEFORE asking it to stop, and
        before forgetting it — `stop()` only sends a terminate signal, it
        does not wait for the process to actually exit, so a line already
        buffered in the pipe (or a URL match already mid-emit on the
        worker thread) can still fire AFTER this call returns. Reported for
        real: switching from Kimi's sign-in screen to a different agent's
        (through Settings, which doesn't pass back through PAGE_AUTH on
        the way — see `_switch_agent_process`) left the old worker running
        long enough for its `url_found` to land on the NEW agent's screen,
        painting Kimi's link over it and disabling ITS buttons. A
        `_terminal_login_worker` that's been told to stop must never be
        able to reach these handlers again, no matter when its thread
        actually winds down.

        `worker_module.release()` after `stop()`, not a bare drop of the
        reference: `stop()` only sends the child a terminate signal, it
        does not wait for the OS thread to actually join — and this
        worker is parented to THIS widget, so if it's still running the
        moment this widget itself is destroyed (a switch immediately
        followed by closing the panel, say), that is `qFatal()`/`SIGABRT`,
        not a leak (docs/facts/houdini.md §14).
        """
        if self._terminal_login_slow_timer is not None:
            self._terminal_login_slow_timer.stop()
            self._terminal_login_slow_timer = None
        if self._terminal_login_stuck_timer is not None:
            self._terminal_login_stuck_timer.stop()
            self._terminal_login_stuck_timer = None
        worker = self._terminal_login_worker
        if worker is None:
            return
        for signal, slot in (
            (worker.line_received, self._on_terminal_login_line),
            (worker.url_found, self._on_terminal_login_url),
            (worker.input_requested, self._on_terminal_login_input_requested),
            (worker.exited, self._on_terminal_login_exited),
            (worker.failed, self._on_terminal_login_failed),
            (worker.command_resolved, self._on_terminal_login_command_resolved),
        ):
            with contextlib.suppress(RuntimeError, TypeError):
                signal.disconnect(slot)
        worker.stop()
        worker_module.release(worker)
        self._terminal_login_worker = None
        self._auth_pending = False

    def _on_auth_cancel_pending(self) -> None:
        """The artist gave up waiting on a pending sign-in.

        For a plain `authenticate()` call: UI-only, nothing to cancel on
        the protocol side (docs/facts/acp-sdk.md §12 — a client-side
        timeout would break a login that's actually working, so the panel
        has none and must not grow one here either). The call is simply
        left to resolve on its own; `_on_authenticated`/`_on_error` still
        apply whenever it does, even if the artist has since picked a
        different method or left the screen entirely.

        For a spawned terminal-auth process (Kimi): this genuinely stops
        something — see `_stop_terminal_login`.
        """
        self._stop_terminal_login()
        self._auth_pending = False
        self._auth_view.clear_pending()

    def _on_authenticated(self, method_id: str) -> None:
        """Sign-in worked — get out of the way and open a conversation.

        Leaving the artist on the sign-in screen after a successful sign-in
        was the bug: they approved it in the browser and the panel gave no
        sign it had noticed, so it looked like the login had failed.

        For `_UNVALIDATED_AUTH_METHODS`, "worked" only means the CALL
        returned without an error — the credential itself was never
        checked (§13). Saying "Signed in." for those would read as a
        promise the panel cannot back up; say what's actually true
        instead, and let the first prompt be the real test.
        """
        if method_id in self._UNVALIDATED_AUTH_METHODS:
            message = (
                "Reading the credential from the environment — nothing was "
                "checked yet, so whether it actually works shows at the "
                "first prompt."
            )
        else:
            message = "Signed in."
        self._note(message)
        self._remember_signed_in(True)
        self._record_auth_attempt(
            self._agent_id, action="sign_in", ok=True, message=message, method_id=method_id
        )
        self._auth_pending = False
        self._auth_view.clear_pending()
        self._show_page(self.PAGE_TRANSCRIPT)
        if self._current_session() is None:
            self._start_new_session()

    def _on_logout_requested(self) -> None:
        """Logging out sends the panel back where sign-in came from.

        After a successful logout, the client raises `auth_required` with
        the same methods that came from `initialize` — the sign-in screen
        shows up on its own, no separate branch needed here. If the agent
        couldn't log out, an `error` arrives instead and the human stays
        put: silently pretending the logout happened isn't an option. This
        is what makes the owner's one-button model ("Sign out" when signed
        in, "Sign in…" otherwise — `_AgentRow`'s own docstring) safe to
        ship: the escape route it depends on is real, not assumed.

        Reachable from two places now: the sign-in screen's own Sign out
        button, and (issue #33) a Settings row's Sign out, which can fire
        with no sign-in screen open at all — `_pending_logout_agent` is how
        `_on_auth_required`/`_on_error` tell a logout's own outcome apart
        from an ordinary sign-in failure landing on the same two signals.

        `is_running()` is checked FIRST, before calling out: `AcpClient.
        _submit` silently drops the request when there is no live worker
        to run it on — no `auth_required`, no `error`, nothing at all. Left
        unchecked, a Sign out click on an agent that isn't running would
        set `_pending_logout_agent` and then just sit there forever, with
        no feedback and no way to tell the click did nothing — the same
        trap this whole change exists to remove, one step further along.
        """
        client = shared_client(self._agent_id)
        if not client.is_running():
            message = "Not connected — nothing to sign out of right now."
            if self._pages.currentIndex() == self.PAGE_AUTH:
                self._auth_view.show_error(message, self._last_auth_method)
            else:
                self._note(f"Sign out failed: {message}", error=True)
            self._record_auth_attempt(self._agent_id, action="sign_out", ok=False, message=message)
            return
        self._pending_logout_agent = self._agent_id
        client.logout()

    def _on_agent_row_sign_in(self, agent_id: str) -> None:
        """"Sign in…" clicked on a Settings row — for ANY installed agent
        with cached auth methods, not only the one this tab happens to be
        connected to right now (issue #33). The current agent's own row
        opens the sign-in screen directly; any other row switches this tab
        onto that agent first — there is no way to hold a second live
        connection open per tab (see `_agent_id`) — and opens it the moment
        that agent actually connects (`_complete_pending_auth_switch`).
        """
        if agent_id == self._agent_id:
            self._offer_sign_in()
            return
        self._pending_auth_target = agent_id
        self._on_agent_chosen(agent_id)

    def _on_agent_row_sign_out(self, agent_id: str) -> None:
        """"Sign out" clicked on a Settings row. For the currently connected
        agent this logs out immediately — no need to detour through the
        sign-in screen just to press the same button that lives there. For
        any other agent, same detour as Sign in: switch to it and land on
        its sign-in screen, rather than firing a logout at an agent nobody
        is looking at yet.
        """
        if agent_id == self._agent_id:
            self._on_logout_requested()
            return
        self._pending_auth_target = agent_id
        self._on_agent_chosen(agent_id)

    def _complete_pending_auth_switch(self) -> None:
        """The agent `_on_agent_row_sign_in`/`_sign_out` switched this tab
        onto has just finished connecting — open its sign-in screen, the
        whole reason the switch happened. Called from both places a connect
        can end up: `_on_session_started` (a fresh session) and the tail of
        `_on_connected` (reattaching to one that was already live, where no
        new session — and so no `_on_session_started` — is coming)."""
        if self._pending_auth_target and self._pending_auth_target == self._agent_id:
            self._pending_auth_target = None
            self._offer_sign_in()

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

        Leaves Settings on the way. The chip lives in the header, which
        stays put while Settings is open, so an agent can be switched from
        that screen — and then the artist is looking at preferences while
        the thing they just asked for happens somewhere they cannot see.
        Picking an agent is a decision about the conversation, so the
        conversation is what to show.

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
        if self._pages.currentIndex() == self.PAGE_SETTINGS:
            self._show_page(self.PAGE_TRANSCRIPT)
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
        # A terminal-auth worker belongs to `old_agent_id`'s login attempt
        # specifically — reported for real: switching agents from Settings
        # while Kimi's spawned login was still running left it alive, and
        # its LATE `url_found` signal painted Kimi's link over the new
        # agent's own sign-in screen and disabled ITS buttons. The page-
        # based guard in `_show_page` doesn't catch this path: switching
        # from Settings goes PAGE_SETTINGS -> PAGE_TRANSCRIPT, never
        # touching PAGE_AUTH in between, so its "leaving PAGE_AUTH" check
        # never fires. Stopping unconditionally here, at the one place
        # every agent switch actually passes through, is what closes it.
        self._stop_terminal_login()

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
        """Write every transcript we have to disk, right now, synchronously.

        Called whenever a conversation could be about to lose its live agent
        session: an agent switch, a panel closing — and, via
        `_persist_conversations_soon`, a prompt just sent or a turn just
        finished. Failures are swallowed — losing history is bad, taking the
        panel down with it is worse.

        Kept as an unconditional, un-debounced write for the switch/close
        call sites on purpose: those are already single, deliberate events,
        and `shutdown()` in particular must not leave anything to a timer
        that a process about to go away may never get to run.
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

    def _persist_conversations_soon(self) -> None:
        """Persist, coalescing a burst of calls into at most one extra write.

        Written after a real loss: an artist's prompt went out, the agent
        was still working on it, and Houdini hung — a hard restart followed,
        `shutdown()` never ran, and the conversation had never touched disk.
        Persisting only at agent-switch/panel-close missed exactly this
        case, because neither happens while a turn is quietly in flight.
        Wired to two more moments now — a prompt going out (`_on_submitted`)
        and a turn finishing (`_on_turn_finished`) — so the worst a hang can
        cost is whatever happened since the last of those, not the whole
        conversation.

        The FIRST call reaching here writes immediately and synchronously,
        on the leading edge — measured on the real store on this machine
        (43 conversations, ~128KB): parse + rebuild + write is well under a
        millisecond, so there is no case for delaying the call that actually
        matters behind a timer. A crash in that gap would just reproduce the
        bug this exists to fix, only smaller, and a timer cannot promise it
        never happens — so it isn't given the chance to.

        Calls arriving again inside the short cooldown that follows (this
        tab's own turn finishing right on the heels of its own prompt, a
        second tab's trigger landing around the same moment) don't each pay
        for a full read-modify-write; they set a dirty flag instead, and one
        trailing write at the end of the window drains it — so a burst of N
        calls costs at most 2 writes, not N.

        What that trailing write is NOT is a second promise of durability.
        The call that OPENED the cooldown is the one guaranteed safe — it
        already went to disk before this method returns. Anything that
        arrives WHILE the cooldown is running is only as safe as the
        `QTimer` that will flush it, and a `QTimer` only fires if the event
        loop is still turning — the same condition under which "a hang"
        stops meaning anything. A crash inside that short window can cost
        the dirty delta (at most `_PERSIST_COOLDOWN_MS` of agent output);
        it cannot cost the prompt that started the window, because that was
        never deferred in the first place.
        """
        if self._persist_cooldown_active:
            self._persist_dirty = True
            return
        self._persist_conversations()
        self._persist_cooldown_active = True
        QtCore.QTimer.singleShot(_PERSIST_COOLDOWN_MS, self._end_persist_cooldown)

    def _end_persist_cooldown(self) -> None:
        """The trailing half of `_persist_conversations_soon`'s window."""
        self._persist_cooldown_active = False
        if self._persist_dirty:
            self._persist_dirty = False
            self._persist_conversations()

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
            # Whatever was still queued when this was last written survives
            # here too — only as plain text, though: `to_records` never
            # kept the original blocks (attachments in particular), the
            # same limit every other restored entry already has ("Only
            # text survives a restart" — `transcript_model.py`). Carried
            # onto a real session's `SessionState` the moment one opens for
            # this conversation (`_on_session_started`'s adoption).
            state.queued = [
                sessions.QueuedMessage(
                    id=record["id"], blocks=[{"type": "text", "text": record["text"]}]
                )
                for record in conversation.entries
                if record.get("kind") == "queued" and record.get("text")
            ]
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

    def _note(self, text: str, *, error: bool = False) -> None:
        """The panel's own single "say something in the feed" mechanism —
        every call site EXCEPT a genuine failure (`error=True`) is routine
        commentary, and used to be indistinguishable from one either way.

        Reported for real, from an owner's own persisted store: 408 of 570
        entries across 43 conversations were `kind="error"`, and the ones
        sampled ("Preparing Claude Agent…", "Agent stopped.") were never
        errors at all — every one of this method's call sites routed
        through `append_error` unconditionally, with nothing else to route
        the merely informational ones to. `error` defaults to `False`
        because most of them (a connection banner, "Signed in.", "Code
        sent — waiting…") are exactly that; the minority that report a
        real problem (a spawn failure, a stalled turn, a failed sign-out)
        pass `error=True` at their own call site — see
        `TranscriptModel.append_note`'s own docstring for the rest.
        """
        current = self._current_session()
        session_id = current.session_id if current else "__idle__"
        model = self._model(session_id)
        entry = model.append_error(text) if error else model.append_note(text)
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
        # The one marker that says "this tab went down in an orderly way".
        # Its absence right before the next `--- panel start ---` is what
        # tells a later reader a hang forced a hard restart instead — see
        # the incident `_persist_conversations_soon` was written for. No
        # session id, no prompt text: `logbook`'s one rule.
        _log.info("panel tab closing")
        self._persist_conversations()
        # Whatever `_persist_conversations_soon`'s cooldown was doing is
        # moot now — the line above already wrote the latest state, and
        # dropping the flag stops its trailing timer from writing again
        # (harmlessly, but pointlessly) into a tab that is on its way out.
        self._persist_cooldown_active = False
        self._persist_dirty = False
        _live_panels_for(self._agent_id).discard(self)

        if self._hip_watch_handle is not None:
            scene.unwatch_hip_dir_changes(self._hip_watch_handle)
            self._hip_watch_handle = None

        if self._session_refresh_timer is not None:
            self._session_refresh_timer.stop()
            self._session_refresh_timer = None

        # Background threads are asked to stop, but a thread in the middle of
        # a network round trip does not stop on request — it stops when the
        # socket does. Whatever it does afterwards must not reach this panel:
        # its widgets are about to be deleted, and a signal arriving after
        # that is "RuntimeError: The SignalInstance object was already
        # deleted". Disconnecting first is what makes the wait a courtesy
        # rather than a race we have to win.
        #
        # `worker_module.release()`, not a bare `.wait()` then drop the
        # reference: a worker still running after its bounded wait used to
        # stay a Qt CHILD of this widget regardless — Python letting go of
        # its own reference does nothing to that C++ parent-child link —
        # and a `QThread` still `isRunning()` when the widget that parented
        # it is destroyed is not a warning, it is `qFatal()`/`SIGABRT`, the
        # whole process down (docs/facts/houdini.md §14, reproduced
        # directly on both Houdini hythons and this project's own PySide
        # venv). `release()` reparents it out and keeps it alive elsewhere
        # until it actually finishes, so THIS widget's own destruction
        # can never be the thing that kills the process.
        worker = self._refresh_worker
        if worker is not None:
            with contextlib.suppress(RuntimeError, TypeError):
                worker.done.disconnect(self._on_refresh_done)
            worker_module.release(worker)
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
            worker_module.release(launch)
            self._launch_worker = None

        sweep = getattr(self, "_orphan_sweep_worker", None)
        if sweep is not None:
            with contextlib.suppress(RuntimeError, TypeError):
                sweep.done.disconnect(self._on_orphans_swept)
            worker_module.release(sweep)
            self._orphan_sweep_worker = None

        panel_update = getattr(self, "_panel_update_worker", None)
        if panel_update is not None:
            with contextlib.suppress(RuntimeError, TypeError):
                panel_update.progressed.disconnect()
                panel_update.succeeded.disconnect()
                panel_update.failed.disconnect()
            worker_module.release(panel_update)
            self._panel_update_worker = None
        self._stop_panel_update_tick()

        # Same reasoning as `_switch_agent_process`/`_show_page`: this
        # process polls indefinitely on its own (docs/facts/acp-sdk.md
        # §14) — the panel closing must not leave it running.
        # `_stop_terminal_login` itself now calls `release()` (its own
        # docstring explains why that moved there instead of staying a
        # separate step only `shutdown()` performed).
        self._stop_terminal_login()

        # `AgentsView`'s own install/update threads, and `Composer`'s own
        # voice-upload thread — same hazard, same fix, different widgets
        # (docs/facts/houdini.md §14 again: EVERY `Worker` this panel owns
        # needs this, not only the four constructed directly in this file).
        self._settings_view.shutdown()
        self._composer.shutdown()
        self._bug_report_view.shutdown()

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

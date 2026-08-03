"""Agents section: the ACP registry plus "custom agent", install/update/remove.

Embedded at the top of `ui/settings_view.py` — there is no standalone agents
screen any more. Switching which installed agent is currently running lives
in the header chip's dropdown menu (`ui/chips.py`, fed by `AgentPanel`), so
this view has no "Use" button: "the agent can't do it, no control gets
drawn" applies to switching just as much as to anything else, and there is
nothing here for a widget to switch *to* — it only changes what's on disk.

Whenever the on-disk installed/custom agent list changes, this view emits
`installed_changed` so the panel can refresh the chip's menu without
recreating anything. That is the only signal it sends outward — same
one-way layering as `ui/announcement.py`/`announcements.Announcement`: this
view is allowed to know about `registry.py`/`runtime.py`/`settings.py`, they
know nothing about UI (design.md, table "Four layers").

An agent unavailable on this platform (`AgentEntry.unavailable_reason()`
non-empty — e.g. Kimi CLI on darwin-x86_64) is shown as a row with that
reason, not hidden — a direct design.md requirement.

Install/update run on a `QThread` (`_InstallWorker`): `runtime.install_agent`
hits the network and the disk, and the GUI thread must not wait on either.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from .. import registry, runtime
from .. import settings as settings_module
from .qt import QtCore, QtWidgets, Signal

if TYPE_CHECKING:
    from ..network import Fetcher
    from ..registry import AgentEntry
    from ..updates import Update


class _InstallWorker(QtCore.QThread):
    """Installs (or updates) a single agent on a background thread."""

    progressed = Signal(int, object, str)  # done, total|None, note
    succeeded = Signal(object)  # runtime.LaunchSpec — this view doesn't use it
    failed = Signal(str)

    def __init__(self, entry: "AgentEntry", *, fetch: "Fetcher | None", parent=None) -> None:
        super().__init__(parent)
        self._entry = entry
        self._fetch = fetch

    def run(self) -> None:  # noqa: D102 - QThread.run override
        try:
            spec = runtime.install_agent(
                self._entry,
                progress=lambda done, total, note: self.progressed.emit(done, total, note),
                fetch=self._fetch,
            )
        except runtime.InstallError as exc:
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(spec)


def _installed_record(agent_id: str, current_settings) -> "settings_module.InstalledAgent | None":
    """What's actually installed, judged by the manifest on disk.

    Settings are only consulted for the extra detail they carry (when it was
    installed, which kind); the manifest decides whether it's there at all.
    """
    version = runtime.installed_version(agent_id)
    if version is None:
        return None
    known = current_settings.installed_agents.get(agent_id)
    if known is not None and known.version == version:
        return known
    return settings_module.InstalledAgent(agent_id=agent_id, version=version, kind="binary")


def _state_text(installed, update: "Update | None") -> str:
    if installed is None:
        return "not installed"
    if update is not None:
        return f"installed {installed.version} — update available: {update.latest}"
    return f"installed {installed.version}"


def _clear_layout(layout: "QtWidgets.QLayout") -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            # `setParent(None)` right away, not just `deleteLater()`: otherwise
            # the old row still counts as a child until the next event loop
            # pass and shows up in `findChildren`/gets counted twice.
            widget.setParent(None)
            widget.deleteLater()


class _AgentRow(QtWidgets.QWidget):
    """One row: a registry agent, or a "custom agent" entry."""

    install_requested = Signal()
    update_requested = Signal()
    uninstall_requested = Signal()
    remove_custom_requested = Signal()
    sign_in_requested = Signal()

    #: Fixed width for the state column and the actions column. Letting them
    #: size to content made every row's buttons land at a different x —
    #: "Install" is narrower than "Update"+"Remove" — which read as an
    #: unaligned list rather than a table. A shared fixed width plus
    #: right-alignment inside it gives every row the same right edge
    #: regardless of what that row happens to show.
    _STATE_COLUMN_WIDTH = 150
    _ACTIONS_COLUMN_WIDTH = 170

    def __init__(
        self,
        *,
        name: str,
        version: str = "",
        state_text: str,
        unavailable_reason: str = "",
        is_installed: bool = False,
        has_update: bool = False,
        is_custom: bool = False,
        can_sign_in: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(8)

        name_box = QtWidgets.QWidget(self)
        name_layout = QtWidgets.QHBoxLayout(name_box)
        name_layout.setContentsMargins(0, 0, 0, 0)
        name_layout.setSpacing(6)
        name_layout.addWidget(QtWidgets.QLabel(name, name_box))
        if version:
            # Version is a detail, not the headline — a name reads fine
            # without it, so it's de-emphasized rather than dropped.
            version_label = QtWidgets.QLabel(version, name_box)
            version_label.setStyleSheet("color: palette(disabled, text);")
            name_layout.addWidget(version_label)
        name_layout.addStretch(1)
        layout.addWidget(name_box, 1)

        self._state_label = QtWidgets.QLabel(unavailable_reason or state_text, self)
        self._state_label.setMinimumWidth(self._STATE_COLUMN_WIDTH)
        self._state_label.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        if unavailable_reason:
            self._state_label.setStyleSheet("color: palette(disabled, text);")
        layout.addWidget(self._state_label)

        self._progress = QtWidgets.QProgressBar(self)
        self._progress.setVisible(False)
        self._progress.setMaximumWidth(120)
        layout.addWidget(self._progress)

        self._actions = QtWidgets.QWidget(self)
        self._actions.setFixedWidth(self._ACTIONS_COLUMN_WIDTH)
        actions_layout = QtWidgets.QHBoxLayout(self._actions)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(6)
        actions_layout.addStretch(1)
        layout.addWidget(self._actions)

        self.unavailable = bool(unavailable_reason)
        if self.unavailable:
            # Shown with its reason, but there is nothing to install it
            # with — no action button at all (design.md: an unavailable
            # agent is shown, not hidden).
            return

        if can_sign_in:
            # Only ever true for whichever row is the currently connected
            # agent (`AgentsView.set_current_agent_auth`, driven by the
            # panel's own `agent_info()`) — moved here from the header
            # chip's switcher menu, which used to show "Sign in…" next to
            # every agent regardless of which one was actually running, or
            # whether the artist had already signed in. This is a setting
            # of the agent, not a choice about which agent to talk to.
            sign_in_btn = QtWidgets.QPushButton("Sign in…", self._actions)
            sign_in_btn.clicked.connect(self.sign_in_requested.emit)
            actions_layout.addWidget(sign_in_btn)

        if is_custom:
            remove_btn = QtWidgets.QPushButton("Remove", self._actions)
            remove_btn.clicked.connect(self.remove_custom_requested.emit)
            actions_layout.addWidget(remove_btn)
            return

        if not is_installed:
            install_btn = QtWidgets.QPushButton("Install", self._actions)
            install_btn.clicked.connect(self.install_requested.emit)
            actions_layout.addWidget(install_btn)
            return

        if has_update:
            update_btn = QtWidgets.QPushButton("Update", self._actions)
            update_btn.clicked.connect(self.update_requested.emit)
            actions_layout.addWidget(update_btn)

        remove_btn = QtWidgets.QPushButton("Remove", self._actions)
        remove_btn.clicked.connect(self.uninstall_requested.emit)
        actions_layout.addWidget(remove_btn)

    # --- download progress -------------------------------------------------

    def set_progress(self, done: int, total: int | None, note: str) -> None:
        self._progress.setVisible(True)
        if total:
            self._progress.setMaximum(total)
            self._progress.setValue(done)
        else:
            self._progress.setMaximum(0)  # total unknown — indeterminate progress
        self._progress.setToolTip(note)

    def clear_progress(self) -> None:
        self._progress.setVisible(False)

    def set_state_text(self, text: str) -> None:
        self._state_label.setText(text)


class AgentsView(QtWidgets.QWidget):
    """Registry list plus "custom agent" — embedded in the settings screen."""

    installed_changed = Signal()
    #: An install/update actually finished — `agent_id`. `installed_changed`
    #: already covers "the list needs a redraw"; this is for a caller that
    #: needs to react to ONE SPECIFIC agent's install (the panel: hide an
    #: "update available" banner for that agent, restart it if updating it
    #: meant stopping it first).
    install_succeeded = Signal(str)
    #: `agent_id, message` — a background install/update failed. The row
    #: already shows this inline, but a caller with a feed of its own (the
    #: panel) needs the fact too: a silent failure here is exactly what a
    #: broken button looks like from the artist's side.
    install_failed = Signal(str, str)
    #: The artist clicked "Sign in…" on whichever row is the currently
    #: connected agent (see `set_current_agent_auth`) — this view knows
    #: nothing about the agent connection itself (design.md's four layers),
    #: so opening the actual sign-in screen is entirely the panel's call.
    sign_in_requested = Signal()

    def __init__(
        self,
        parent=None,
        *,
        fetch: "Fetcher | None" = None,
        before_install: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._fetch = fetch
        # Called with the agent_id right before its files are touched —
        # this view doesn't know about the running agent process at all
        # (design.md's four layers: it knows registry/runtime/settings,
        # nothing about the agent connection), so "is this the one
        # currently running, and should it be stopped first" is entirely
        # the caller's call, made here rather than skipped.
        self._before_install = before_install
        self._entries: list["AgentEntry"] = []
        self._updates_by_target: dict[str, "Update"] = {}
        # Keep references to live threads here — otherwise Python's garbage
        # collector could claim the QThread before it has actually finished.
        self._threads: list[_InstallWorker] = []
        # One row per registry agent id — the notice banner's "Update"
        # triggers the SAME row's own install, rather than duplicating the
        # install machinery for a second entry point.
        self._rows_by_id: dict[str, "_AgentRow"] = {}
        # Guards a rapid double-click (row AND banner both reachable for the
        # same agent) from starting two installs onto the same files at once.
        self._installing: set[str] = set()
        # Which agent id is the one actually connected right now, and
        # whether ITS `initialize` declared any auth methods — the only row
        # that ever gets a "Sign in…" button (`set_current_agent_auth`).
        self._current_agent_id: str | None = None
        self._current_agent_can_sign_in = False

        self._rows_layout = QtWidgets.QVBoxLayout()
        self._custom_rows_layout = QtWidgets.QVBoxLayout()

        self._custom_name = QtWidgets.QLineEdit()
        self._custom_name.setPlaceholderText("Name")
        self._custom_command = QtWidgets.QLineEdit()
        self._custom_command.setPlaceholderText("Command")
        self._custom_args = QtWidgets.QLineEdit()
        self._custom_args.setPlaceholderText("Arguments, space-separated")
        add_custom_btn = QtWidgets.QPushButton("Add custom agent")
        add_custom_btn.clicked.connect(self._on_add_custom)

        custom_form = QtWidgets.QHBoxLayout()
        custom_form.addWidget(self._custom_name)
        custom_form.addWidget(self._custom_command)
        custom_form.addWidget(self._custom_args)
        custom_form.addWidget(add_custom_btn)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self._rows_layout)
        layout.addWidget(QtWidgets.QLabel("Custom agent"))
        layout.addLayout(self._custom_rows_layout)
        layout.addLayout(custom_form)

        self._load_custom_agents()

    # --- feeding data --------------------------------------------------

    def set_agents(self, entries: list["AgentEntry"], *, updates: list["Update"] | None = None) -> None:
        """Redraw the registry agent rows.

        State (installed / version / update available) is read from
        `settings.load()` on every redraw — there is exactly one source of
        truth, and duplicating it in the widget's own memory would risk it
        drifting out of sync.
        """
        self._entries = list(entries)
        self._updates_by_target = {u.target: u for u in (updates or []) if u.kind == "agent"}
        self._rebuild_registry_rows()

    def set_current_agent_auth(self, agent_id: str | None, can_sign_in: bool) -> None:
        """Which agent is actually connected right now, and whether ITS
        `initialize` declared any auth methods at all.

        Not "needs to sign in" — an agent declares its methods whether or
        not the artist is already signed in (design.md/architecture.md have
        no protocol signal for "currently authenticated" to check instead).
        Only the row for `agent_id` ever gets the button; every other row
        must not have one, registry or custom.
        """
        self._current_agent_id = agent_id
        self._current_agent_can_sign_in = can_sign_in
        self._rebuild_registry_rows()
        self._load_custom_agents()

    def refresh_from_registry(self, *, force: bool = False) -> None:
        """Shortcut for the panel: fetch the registry itself, with the same
        `fetch` given to the constructor (tests hand in a `FakeFetcher`,
        production passes nothing and `registry.fetch_registry` uses the
        real network)."""
        try:
            entries = registry.fetch_registry(force=force, fetch=self._fetch)
        except registry.RegistryError:
            entries = []
        self.set_agents(entries)

    def _rebuild_registry_rows(self) -> None:
        """Rebuild the registry rows.

        "Installed" is answered by the manifest on disk (`runtime`), not by
        `settings.installed_agents`. There used to be two sources of truth
        and they disagreed: an agent installed by the CLI (`--agents
        opencode`) writes a manifest but no settings entry, so the row said
        "not installed" — and clicking Install found the manifest, returned
        instantly with no download, and only then wrote the settings. From
        the outside that looked exactly like "it installs in no time and
        never remembers".
        """
        _clear_layout(self._rows_layout)
        self._rows_by_id = {}
        current_settings = settings_module.load()
        for entry in self._entries:
            reason = entry.unavailable_reason()
            installed = _installed_record(entry.id, current_settings)
            update = self._updates_by_target.get(entry.id)
            row = _AgentRow(
                name=entry.name,
                version=entry.version,
                state_text=_state_text(installed, update),
                unavailable_reason=reason,
                is_installed=installed is not None,
                has_update=update is not None,
                can_sign_in=(entry.id == self._current_agent_id and self._current_agent_can_sign_in),
                parent=self,
            )
            row.install_requested.connect(lambda checked=False, e=entry, r=row: self._install(e, r))
            row.update_requested.connect(lambda checked=False, e=entry, r=row: self._install(e, r))
            row.uninstall_requested.connect(lambda checked=False, e=entry: self._uninstall(e.id))
            row.sign_in_requested.connect(self.sign_in_requested.emit)
            self._rows_layout.addWidget(row)
            self._rows_by_id[entry.id] = row

    def trigger_update(self, agent_id: str) -> bool:
        """Start updating `agent_id` as if its own row's button were clicked.

        The notice banner's "Update" is a quick-access path onto this SAME
        row and this SAME install machinery — not a second implementation of
        "download, verify, extract" with its own bugs to find. `False` means
        there is no row for this id right now (the registry hasn't loaded,
        or the update no longer applies) — callers have to say something
        about that, not act as if it worked.
        """
        row = self._rows_by_id.get(agent_id)
        entry = next((e for e in self._entries if e.id == agent_id), None)
        if row is None or entry is None:
            return False
        self._install(entry, row)
        return True

    def _install(self, entry: "AgentEntry", row: "_AgentRow") -> None:
        if entry.id in self._installing:
            return  # already installing/updating this one — a double-click
        self._installing.add(entry.id)
        if self._before_install is not None:
            self._before_install(entry.id)
        worker = _InstallWorker(entry, fetch=self._fetch, parent=self)
        self._threads.append(worker)
        worker.progressed.connect(row.set_progress)
        worker.succeeded.connect(lambda _spec, e=entry, r=row: self._on_installed(e, r))
        worker.failed.connect(lambda message, e=entry, r=row: self._on_install_failed(r, message, e.id))
        worker.finished.connect(lambda w=worker: self._forget_thread(w))
        worker.start()

    def _forget_thread(self, worker: "_InstallWorker") -> None:
        # `finished` fires just BEFORE the thread actually stops — dropping
        # the last Python reference here without calling `wait()` first
        # risks deleting the QThread before the OS thread has physically
        # joined (a crash, not always reproducible).
        worker.wait()
        if worker in self._threads:
            self._threads.remove(worker)

    def _on_install_failed(self, row: "_AgentRow", message: str, agent_id: str) -> None:
        self._installing.discard(agent_id)
        row.clear_progress()
        row.set_state_text(f"error: {message}")
        self.install_failed.emit(agent_id, message)

    def _on_installed(self, entry: "AgentEntry", row: "_AgentRow") -> None:
        self._installing.discard(entry.id)
        row.clear_progress()
        current = settings_module.load()
        kind = "npx" if entry.needs_node else "binary"
        current.installed_agents[entry.id] = settings_module.InstalledAgent(
            agent_id=entry.id,
            version=entry.version,
            kind=kind,
            installed_at=settings_module.InstalledAgent.now(),
        )
        settings_module.save(current)
        self._rebuild_registry_rows()
        self.installed_changed.emit()
        self.install_succeeded.emit(entry.id)

    def _uninstall(self, agent_id: str) -> None:
        runtime.uninstall_agent(agent_id)
        current = settings_module.load()
        current.installed_agents.pop(agent_id, None)
        settings_module.save(current)
        self._rebuild_registry_rows()
        self.installed_changed.emit()

    # --- "custom agent" --------------------------------------------------

    def _load_custom_agents(self) -> None:
        _clear_layout(self._custom_rows_layout)
        current = settings_module.load()
        for agent in current.custom_agents:
            row = _AgentRow(
                name=agent.name,
                version=agent.command,
                state_text="custom agent",
                is_custom=True,
                can_sign_in=(agent.id == self._current_agent_id and self._current_agent_can_sign_in),
                parent=self,
            )
            row.remove_custom_requested.connect(lambda checked=False, a=agent: self._remove_custom(a.id))
            row.sign_in_requested.connect(self.sign_in_requested.emit)
            self._custom_rows_layout.addWidget(row)

    def _on_add_custom(self) -> None:
        name = self._custom_name.text().strip()
        command = self._custom_command.text().strip()
        if not name or not command:
            return
        args = self._custom_args.text().split()
        current = settings_module.load()
        agent_id = f"custom:{name}"
        current.custom_agents = [a for a in current.custom_agents if a.id != agent_id]
        current.custom_agents.append(settings_module.CustomAgent(id=agent_id, name=name, command=command, args=args))
        settings_module.save(current)
        self._custom_name.clear()
        self._custom_command.clear()
        self._custom_args.clear()
        self._load_custom_agents()
        self.installed_changed.emit()

    def _remove_custom(self, agent_id: str) -> None:
        current = settings_module.load()
        current.custom_agents = [a for a in current.custom_agents if a.id != agent_id]
        settings_module.save(current)
        self._load_custom_agents()
        self.installed_changed.emit()


__all__ = ["AgentsView"]

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

from typing import TYPE_CHECKING

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

    def __init__(
        self,
        *,
        title: str,
        state_text: str,
        unavailable_reason: str = "",
        is_installed: bool = False,
        has_update: bool = False,
        is_custom: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)

        layout.addWidget(QtWidgets.QLabel(title), 1)

        self._state_label = QtWidgets.QLabel(unavailable_reason or state_text)
        if unavailable_reason:
            self._state_label.setStyleSheet("color: gray;")
        layout.addWidget(self._state_label)

        self._progress = QtWidgets.QProgressBar()
        self._progress.setVisible(False)
        self._progress.setMaximumWidth(120)
        layout.addWidget(self._progress)

        self.unavailable = bool(unavailable_reason)
        if self.unavailable:
            # Shown with its reason, but there is nothing to install it
            # with — no action button at all (design.md: an unavailable
            # agent is shown, not hidden).
            return

        if is_custom:
            remove_btn = QtWidgets.QPushButton("Remove")
            remove_btn.clicked.connect(self.remove_custom_requested.emit)
            layout.addWidget(remove_btn)
            return

        if not is_installed:
            install_btn = QtWidgets.QPushButton("Install")
            install_btn.clicked.connect(self.install_requested.emit)
            layout.addWidget(install_btn)
            return

        if has_update:
            update_btn = QtWidgets.QPushButton("Update")
            update_btn.clicked.connect(self.update_requested.emit)
            layout.addWidget(update_btn)

        remove_btn = QtWidgets.QPushButton("Remove")
        remove_btn.clicked.connect(self.uninstall_requested.emit)
        layout.addWidget(remove_btn)

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

    def __init__(self, parent=None, *, fetch: "Fetcher | None" = None) -> None:
        super().__init__(parent)
        self._fetch = fetch
        self._entries: list["AgentEntry"] = []
        self._updates_by_target: dict[str, "Update"] = {}
        # Keep references to live threads here — otherwise Python's garbage
        # collector could claim the QThread before it has actually finished.
        self._threads: list[_InstallWorker] = []

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
        _clear_layout(self._rows_layout)
        current_settings = settings_module.load()
        for entry in self._entries:
            reason = entry.unavailable_reason()
            installed = current_settings.installed_agents.get(entry.id)
            update = self._updates_by_target.get(entry.id)
            row = _AgentRow(
                title=f"{entry.name} {entry.version}",
                state_text=_state_text(installed, update),
                unavailable_reason=reason,
                is_installed=installed is not None,
                has_update=update is not None,
                parent=self,
            )
            row.install_requested.connect(lambda checked=False, e=entry, r=row: self._install(e, r))
            row.update_requested.connect(lambda checked=False, e=entry, r=row: self._install(e, r))
            row.uninstall_requested.connect(lambda checked=False, e=entry: self._uninstall(e.id))
            self._rows_layout.addWidget(row)

    def _install(self, entry: "AgentEntry", row: "_AgentRow") -> None:
        worker = _InstallWorker(entry, fetch=self._fetch, parent=self)
        self._threads.append(worker)
        worker.progressed.connect(row.set_progress)
        worker.succeeded.connect(lambda _spec, e=entry, r=row: self._on_installed(e, r))
        worker.failed.connect(lambda message, r=row: self._on_install_failed(r, message))
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

    def _on_install_failed(self, row: "_AgentRow", message: str) -> None:
        row.clear_progress()
        row.set_state_text(f"error: {message}")

    def _on_installed(self, entry: "AgentEntry", row: "_AgentRow") -> None:
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
            row = _AgentRow(title=f"{agent.name} ({agent.command})", state_text="custom agent", is_custom=True, parent=self)
            row.remove_custom_requested.connect(lambda checked=False, a=agent: self._remove_custom(a.id))
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

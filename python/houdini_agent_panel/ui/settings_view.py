"""Settings screen — the design.md set of fields, plus the agents section.

The "Agents" block (the registry six plus custom agents: install / update /
remove) lives at the top of this screen instead of on its own page. Switching
between already-installed agents now happens from the header chip's dropdown
(`ui/chips.py`), so a whole separate screen dedicated to "which agent to run"
stopped earning its keep — see `AgentPanel._open_agent_management`, which is
just "open settings, scroll to top".

Everything is grouped into collapsible `_Section`s (Agents / Behaviour /
Updates & notices / Voice / Privacy / Data) inside a fixed-width, centered
rail — the same 736 px column the feed and composer use — instead of one
long form stretched edge to edge.

Reads and writes `settings.json` directly (`settings.load`/`settings.save`) —
the same one-way layering as `ui/agents.py`: the settings screen is allowed
to know about `settings.py`, not the other way round.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Callable

from .. import paths
from .. import settings as settings_module
from .agents import AgentsView
from .chips import ChoiceButton
from . import theme
from .qt import QtCore, QtWidgets, Signal

if TYPE_CHECKING:
    from ..network import Fetcher

_RAIL_WIDTH = 736
#: Floor for the centered rail — see `Composer._MIN_RAIL_WIDTH`.
_MIN_RAIL_WIDTH = 180


class _Section(QtWidgets.QWidget):
    """A titled, collapsible group of settings rows.

    A flat form gives every field the same visual weight, so there's
    nothing to scan — an artist has to read the whole screen to find the
    one setting they came for. Grouping by topic under a foldable header
    lets them collapse what they don't need to think about (Privacy, Data)
    and land on the rest.
    """

    def __init__(self, title: str, parent=None, *, expanded: bool = True) -> None:
        super().__init__(parent)
        self._toggle = QtWidgets.QToolButton(self)
        self._toggle.setObjectName("sectionToggle")
        self._toggle.setToolButtonStyle(QtCore.Qt.ToolButtonTextBesideIcon)
        self._toggle.setArrowType(QtCore.Qt.DownArrow if expanded else QtCore.Qt.RightArrow)
        self._toggle.setCheckable(True)
        self._toggle.setChecked(expanded)
        # A bare "&" is Qt's mnemonic marker (it underlines the next letter
        # instead of printing itself) — "&&" is how you get a literal one.
        self._toggle.setText(title.replace("&", "&&"))
        self._toggle.clicked.connect(self._on_toggled)

        self.form = QtWidgets.QFormLayout()
        self.form.setContentsMargins(6, 4, 6, 14)
        self.form.setSpacing(8)
        self._body = QtWidgets.QWidget(self)
        self._body.setLayout(self.form)
        self._body.setVisible(expanded)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self._toggle)
        layout.addWidget(self._body)

        self.setStyleSheet(
            "QToolButton#sectionToggle {"
            " border: none; background: transparent; padding: 10px 4px 6px 0;"
            " font-weight: 600; color: palette(text); text-align: left;"
            "}"
            "QToolButton#sectionToggle:hover { color: palette(highlight); }"
        )

    def add_row(self, *args: object) -> None:
        """Same signature as `QFormLayout.addRow` (label+field, or a single
        full-width widget/layout for a lone checkbox or button)."""
        self.form.addRow(*args)

    def add_widget(self, widget: QtWidgets.QWidget) -> None:
        """A full-width, non-form child — the embedded `AgentsView`."""
        self.form.addRow(widget)

    def _on_toggled(self, checked: bool) -> None:
        self._body.setVisible(checked)
        self._toggle.setArrowType(QtCore.Qt.DownArrow if checked else QtCore.Qt.RightArrow)


class SettingsView(QtWidgets.QWidget):
    changed = Signal()
    closed = Signal()
    #: Forwarded straight from `AgentsView` — see its docstring. The panel
    #: needs these to react to ONE specific agent's install, which the
    #: generic `changed` (settings reload) doesn't carry enough to do.
    install_succeeded = Signal(str)
    install_failed = Signal(str, str)
    #: Forwarded from `AgentsView` — the panel is the one that actually
    #: knows how to open the sign-in screen (`AgentPanel._offer_sign_in`);
    #: this view only knows registry/runtime/settings, never the connection.
    sign_in_requested = Signal()

    def __init__(
        self,
        parent=None,
        *,
        fetch: "Fetcher | None" = None,
        before_install: "Callable[[str], None] | None" = None,
    ) -> None:
        super().__init__(parent)
        self._loading = False

        close_button = QtWidgets.QToolButton()
        close_button.setText("←")
        close_button.setToolTip("Back")
        close_button.clicked.connect(self.closed.emit)

        # The back arrow lines up with the left edge of the settings rail
        # below it, not with the panel's own edge. Full width put it out at
        # the very border, where an open conversation drawer covered it —
        # and the one control that leaves settings has no business hiding.
        self._header_rail = QtWidgets.QWidget()
        header_rail_layout = QtWidgets.QHBoxLayout(self._header_rail)
        header_rail_layout.setContentsMargins(0, 10, 0, 4)
        header_rail_layout.setSpacing(6)
        header_rail_layout.addWidget(close_button)
        header_rail_layout.addWidget(QtWidgets.QLabel("Settings"))
        header_rail_layout.addStretch(1)

        header = QtWidgets.QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setAlignment(QtCore.Qt.AlignHCenter)
        header.addWidget(self._header_rail)

        self._agents_view = AgentsView(self, fetch=fetch, before_install=before_install)
        # An installed/custom agent list change is exactly the kind of
        # settings change that should make the default-agent combo and the
        # header chip's menu refresh, so it rides the same `changed` signal
        # rather than getting a parallel one panel.py has to wire up too.
        self._agents_view.installed_changed.connect(self._on_agents_changed)
        self._agents_view.install_succeeded.connect(self.install_succeeded.emit)
        self._agents_view.install_failed.connect(self.install_failed.emit)
        self._agents_view.sign_in_requested.connect(self.sign_in_requested.emit)

        self._default_agent_combo = ChoiceButton(self)
        self._default_agent_combo.currentIndexChanged.connect(self._on_field_changed)

        self._autostart_checkbox = QtWidgets.QCheckBox("Autostart agent when the panel opens")
        self._autostart_checkbox.toggled.connect(self._on_field_changed)

        self._check_updates_checkbox = QtWidgets.QCheckBox("Check for updates")
        self._check_updates_checkbox.toggled.connect(self._on_field_changed)

        self._show_announcements_checkbox = QtWidgets.QCheckBox("Show announcements")
        self._show_announcements_checkbox.toggled.connect(self._on_field_changed)

        self._telemetry_checkbox = QtWidgets.QCheckBox(
            "Telemetry (anonymous, off by default)"
        )
        self._telemetry_checkbox.toggled.connect(self._on_field_changed)

        self._whisper_edit = QtWidgets.QLineEdit()
        self._whisper_edit.setPlaceholderText("http://127.0.0.1:9000 (local whisper)")
        self._whisper_edit.textChanged.connect(self._on_field_changed)

        self._data_dir_label = QtWidgets.QLabel()
        self._data_dir_label.setWordWrap(True)
        open_data_dir_button = QtWidgets.QPushButton("Open")
        open_data_dir_button.clicked.connect(self._on_open_data_dir)

        data_dir_row = QtWidgets.QHBoxLayout()
        data_dir_row.addWidget(self._data_dir_label, 1)
        data_dir_row.addWidget(open_data_dir_button)

        copy_diagnostics_button = QtWidgets.QPushButton("Copy diagnostics")
        copy_diagnostics_button.clicked.connect(self._on_copy_diagnostics)

        agents_section = _Section("Agents", self, expanded=True)
        agents_section.add_widget(self._agents_view)

        behaviour_section = _Section("Behaviour", self, expanded=True)
        behaviour_section.add_row("Default agent", self._default_agent_combo)
        behaviour_section.add_row(self._autostart_checkbox)

        updates_section = _Section("Updates & notices", self, expanded=True)
        updates_section.add_row(self._check_updates_checkbox)
        updates_section.add_row(self._show_announcements_checkbox)

        voice_section = _Section("Voice", self, expanded=True)
        voice_section.add_row("Whisper endpoint", self._whisper_edit)

        privacy_section = _Section("Privacy", self, expanded=False)
        privacy_section.add_row(self._telemetry_checkbox)

        data_section = _Section("Data", self, expanded=False)
        data_section.add_row("Data folder", data_dir_row)
        data_section.add_row(copy_diagnostics_button)

        rail = QtWidgets.QWidget()
        rail_layout = QtWidgets.QVBoxLayout(rail)
        rail_layout.setContentsMargins(0, 8, 0, 24)
        rail_layout.setSpacing(2)
        for section in (
            agents_section,
            behaviour_section,
            updates_section,
            voice_section,
            privacy_section,
            data_section,
        ):
            rail_layout.addWidget(section)
        # Slack goes to the bottom, not between the sections. The scroll area
        # stretches its content to the viewport, and without this the spare
        # height was shared out among the section bodies, drifting them
        # apart into a form with holes in it.
        rail_layout.addStretch(1)
        self._rail = rail

        content = QtWidgets.QWidget()
        content_layout = QtWidgets.QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setAlignment(QtCore.Qt.AlignHCenter)
        content_layout.addWidget(rail, 0, QtCore.Qt.AlignHCenter)

        self._scroll = QtWidgets.QScrollArea()
        self._scroll.setStyleSheet(theme.scrollbar_stylesheet())
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self._scroll.setWidget(content)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(header)
        layout.addWidget(self._scroll, 1)

        self.reload()

    def minimumSizeHint(self) -> QtCore.QSize:  # noqa: N802 - Qt override
        """Don't let the rail's fixed width become the panel's minimum.

        Same reason as `Composer.minimumSizeHint`: a `setFixedWidth` child
        propagates its width upward as a minimum, and the settings page is
        part of the panel's page stack, so its rail was pinning the whole
        panel wide.
        """
        hint = super().minimumSizeHint()
        return QtCore.QSize(min(hint.width(), _MIN_RAIL_WIDTH), hint.height())

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        # Same centered-rail rule as the header and composer (chips.py,
        # composer.py): a fixed reading width up to 736px, shrinking only
        # when the panel itself is narrower than that.
        width = max(_MIN_RAIL_WIDTH, min(_RAIL_WIDTH, self.width() - 28))
        self._rail.setFixedWidth(width)
        self._header_rail.setFixedWidth(width)

    # --- public -----------------------------------------------------

    def set_agents(self, entries, *, updates=None) -> None:
        """Forwarded to the embedded `AgentsView` — callers (the panel's
        registry refresh) don't need to know the agents block moved in
        here."""
        self._agents_view.set_agents(entries, updates=updates)

    def focus_agents(self) -> None:
        """Scroll to the agents section. It's first, so this is just "top"."""
        self._scroll.verticalScrollBar().setValue(0)

    def trigger_agent_update(self, agent_id: str) -> bool:
        """Forwarded to the embedded `AgentsView` — see its `trigger_update`."""
        return self._agents_view.trigger_update(agent_id)

    def set_current_agent_auth(self, agent_id: str | None, can_sign_in: bool) -> None:
        """Forwarded to the embedded `AgentsView` — see its `set_current_agent_auth`."""
        self._agents_view.set_current_agent_auth(agent_id, can_sign_in)

    def reload(self) -> None:
        """Re-read `settings.json` from disk and refresh the controls without
        writing it back (otherwise an external file change would loop back
        through `_on_field_changed`)."""
        self._loading = True
        try:
            current = settings_module.load()
            self._populate_default_agent_options(current)
            self._autostart_checkbox.setChecked(current.autostart_agent)
            self._check_updates_checkbox.setChecked(current.check_updates)
            self._show_announcements_checkbox.setChecked(current.show_announcements)
            self._telemetry_checkbox.setChecked(current.telemetry)
            self._whisper_edit.setText(current.whisper_endpoint)
            self._data_dir_label.setText(str(paths.data_dir()))
        finally:
            self._loading = False

    # --- internal ------------------------------------------------------

    def _populate_default_agent_options(self, current: "settings_module.Settings") -> None:
        self._default_agent_combo.blockSignals(True)
        self._default_agent_combo.clear()
        self._default_agent_combo.addItem("—", None)
        agent_ids = sorted(set(current.installed_agents) | {a.id for a in current.custom_agents})
        for agent_id in agent_ids:
            self._default_agent_combo.addItem(agent_id, agent_id)
        index = self._default_agent_combo.findData(current.default_agent)
        self._default_agent_combo.setCurrentIndex(index if index >= 0 else 0)
        self._default_agent_combo.blockSignals(False)

    def _on_field_changed(self, *_args: object) -> None:
        if self._loading:
            return
        current = settings_module.load()
        current.default_agent = self._default_agent_combo.currentData()
        current.autostart_agent = self._autostart_checkbox.isChecked()
        current.check_updates = self._check_updates_checkbox.isChecked()
        current.show_announcements = self._show_announcements_checkbox.isChecked()
        current.telemetry = self._telemetry_checkbox.isChecked()
        current.whisper_endpoint = self._whisper_edit.text().strip()
        settings_module.save(current)
        self.changed.emit()

    def _on_agents_changed(self) -> None:
        # An install/update/remove writes settings.json directly (like
        # AgentsView._on_add_custom always has) — reload rather than merge,
        # same reasoning as `AgentPanel._on_agent_chosen`.
        self.reload()
        self.changed.emit()

    def _on_open_data_dir(self) -> None:
        paths.open_in_file_manager(paths.data_dir())

    def _on_copy_diagnostics(self) -> None:
        current = settings_module.load()
        text = settings_module.diagnostics(current)
        clipboard = QtWidgets.QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)


__all__ = ["SettingsView"]

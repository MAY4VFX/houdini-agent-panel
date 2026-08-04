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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from .. import paths
from .. import settings as settings_module
from .agents import AgentsView
from .chips import ChoiceButton
from . import theme
from .qt import QtCore, QtGui, QtWidgets, Signal

if TYPE_CHECKING:
    from ..network import Fetcher

_RAIL_WIDTH = 736
#: Floor for the centered rail — see `Composer._MIN_RAIL_WIDTH`.
_MIN_RAIL_WIDTH = 180

# --- the settings grid --------------------------------------------------
#
# Reported as "a staircase": section headers, agent rows, "Custom agent",
# checkboxes and field labels each started at their own X, and values
# ("Default agent", "Whisper endpoint", agent status) landed wherever the
# label in front of them happened to end. `_Section` used to hand each
# instance its own `QFormLayout`, which computes its label column from
# ONLY that section's own rows — four sections, four independent column
# widths, four left edges.
#
#   header text   →|  ←indent→  label  ←label_value_gap→  value →|
#                              ├──────┤
#                            label_width
#
# Every number in `_GridMetrics` is derived from `QFontMetrics` against the
# live application font, not a standalone pixel guess — apple-design's
# typography rule applies here even though this is Qt, not CSS: "Respect
# the user's text-size setting. Scale layout WITH the text, in rem/em, not
# fixed px" — otherwise the grid computed today quietly comes apart the
# day someone runs Houdini with a larger UI font scale, which is exactly
# the kind of drift this rewrite exists to stop.
_ROW_LABELS = ("Default agent", "Whisper endpoint", "Data folder")


@dataclass(frozen=True)
class _GridMetrics:
    """Every spacing the settings grid uses, computed once from the live
    font (`measure()` below) instead of stored as fixed pixel constants.

    Base unit is `em` — one line's height (`QFontMetrics.height()`), the
    desktop-Qt analogue of a CSS `rem`. Every field is a named, defensible
    fraction or multiple of it, not an unrelated guess:

    - `indent` = 1 em. A full line's worth of indent reads unambiguously
      as "this body is nested under that header" at any font size, and
      can't collapse into the toggle's own small inner padding the way a
      fixed few-pixel number eventually would on a larger font.
    - `row_gap` = 0.5 em. Rows inside one topic breathe by half a line.
    - `section_gap` = 1.5 em. Visibly more than `row_gap` — scanning down
      the page reads "new topic" at a boundary, not "next field" — but
      derived from the SAME base unit, applied in exactly one place
      (`rail_layout.setSpacing`), instead of whatever a form's bottom
      margin plus the next header's own top padding used to add up to.
    - `label_value_gap` = the width of two spaces in the running text —
      enough for a label and its value to read as two columns, not one
      run-on line.
    - `label_width` = the widest `QLabel.sizeHint()` among `_ROW_LABELS`
      ("Default agent" / "Whisper endpoint" / "Data folder"), measured
      directly rather than estimated. At this codebase's default font
      that's 104px ("Whisper endpoint"). Below `_MIN_RAIL_WIDTH` (180px)
      that leaves the value column under 50px — cramped, but not broken:
      `ChoiceButton` already elides its text to whatever width it's given
      (`chips.py::ChoiceButton.paintEvent`), `QLineEdit` scrolls instead
      of truncating, and the data-folder path label already wraps
      (`setWordWrap(True)`). No new narrow-width behaviour was added
      here — the existing widgets already degrade gracefully, this grid
      just stopped fighting them for space.
    """

    indent: int
    row_gap: int
    section_gap: int
    label_value_gap: int
    label_width: int

    @staticmethod
    def measure() -> "_GridMetrics":
        metrics = QtGui.QFontMetrics(QtWidgets.QApplication.font())
        em = metrics.height()
        # `QLabel.sizeHint()`, not `QFontMetrics.horizontalAdvance` on the
        # bare string — measuring the actual widget that occupies the
        # grid's column 0, not a separate approximation of it. The two
        # disagreed by a pixel for "Whisper endpoint" (Qt's internal text
        # layout rounds a fractional glyph advance up in a way
        # `horizontalAdvance` alone does not); a column sized from the
        # approximation then had to grow itself by that pixel to fit the
        # real label, which put JUST that section's column-0 a pixel wider
        # than every other section's — the exact cross-section drift this
        # shared grid exists to prevent.
        label_width = max(QtWidgets.QLabel(text).sizeHint().width() for text in _ROW_LABELS)
        return _GridMetrics(
            indent=em,
            row_gap=round(em * 0.5),
            section_gap=round(em * 1.5),
            label_value_gap=metrics.horizontalAdvance("  "),
            label_width=label_width,
        )


class _Section(QtWidgets.QWidget):
    """A titled, collapsible group of settings rows.

    A flat form gives every field the same visual weight, so there's
    nothing to scan — an artist has to read the whole screen to find the
    one setting they came for. Grouping by topic under a foldable header
    lets them collapse what they don't need to think about (Privacy, Data)
    and land on the rest.

    Every row is one of four shapes, so the section reads as a single
    two-column grid instead of a form with its own idea of alignment:

    - `add_row(label, field)` — label in the fixed column, value beside it.
    - `add_checkbox(checkbox)` — no separate label; starts at the SAME X as
      every row's label, since the checkbox's own text plays that role.
    - `add_action_row(button)` — no label; right-aligned to the section's
      own right edge — the same edge a value column's own right-aligned
      content (e.g. "Open" beside "Data folder") already reaches.
    - `add_widget(widget)` — a full-bleed child (the embedded `AgentsView`)
      that lays out its own rows; only the section's left indent applies.
    """

    def __init__(
        self, title: str, parent=None, *, expanded: bool = True, grid: "_GridMetrics"
    ) -> None:
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

        self.grid = QtWidgets.QGridLayout()
        self.grid.setContentsMargins(grid.indent, 4, 0, 4)
        self.grid.setHorizontalSpacing(grid.label_value_gap)
        self.grid.setVerticalSpacing(grid.row_gap)
        self.grid.setColumnMinimumWidth(0, grid.label_width)
        # The value column takes whatever width the indent and the label
        # column don't — this is what makes an action row's right edge and
        # a value row's right-aligned content land on the same X.
        self.grid.setColumnStretch(1, 1)
        self._row = 0

        self._body = QtWidgets.QWidget(self)
        self._body.setLayout(self.grid)
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

    def add_row(self, label: str, field: QtWidgets.QWidget | QtWidgets.QLayout) -> None:
        """Label in the fixed column, value beside it."""
        text = QtWidgets.QLabel(label, self._body)
        self.grid.addWidget(text, self._row, 0, QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter)
        if isinstance(field, QtWidgets.QLayout):
            self.grid.addLayout(field, self._row, 1)
        else:
            self.grid.addWidget(field, self._row, 1)
        self._row += 1

    def add_checkbox(self, checkbox: QtWidgets.QCheckBox) -> None:
        """A lone checkbox, no separate label — spans both columns, so it
        starts at the label column's own X."""
        self.grid.addWidget(checkbox, self._row, 0, 1, 2)
        self._row += 1

    def add_action_row(self, button: QtWidgets.QPushButton) -> None:
        """A lone action button, no label — pinned to the section's right
        edge regardless of the button's own text width."""
        row = QtWidgets.QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addStretch(1)
        row.addWidget(button)
        self.grid.addLayout(row, self._row, 0, 1, 2)
        self._row += 1

    def add_widget(self, widget: QtWidgets.QWidget) -> None:
        """A full-bleed child (the embedded `AgentsView`) — only the
        section's left indent applies; it lays out its own rows."""
        self.grid.addWidget(widget, self._row, 0, 1, 2)
        self._row += 1

    def widget_at(self, row: int, column: int) -> QtWidgets.QWidget | None:
        """The widget at a given grid cell — for a test that checks
        alignment across sections without needing every row's label as its
        own named attribute (`test_settings_grid_alignment.py`)."""
        item = self.grid.itemAtPosition(row, column)
        return item.widget() if item is not None else None

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
        before_uninstall: "Callable[[str], None] | None" = None,
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

        self._agents_view = AgentsView(
            self, fetch=fetch, before_install=before_install, before_uninstall=before_uninstall
        )
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
        self._open_data_dir_button = QtWidgets.QPushButton("Open")
        self._open_data_dir_button.clicked.connect(self._on_open_data_dir)

        data_dir_row = QtWidgets.QHBoxLayout()
        # A nested layout's own default contents margins are NOT
        # guaranteed zero (they come from the style) — without this, the
        # data-folder path landed a pixel off from every other value
        # column, the exact "off by one" a numeric alignment test exists
        # to catch (test_settings_grid_alignment.py).
        data_dir_row.setContentsMargins(0, 0, 0, 0)
        data_dir_row.addWidget(self._data_dir_label, 1)
        data_dir_row.addWidget(self._open_data_dir_button)

        self._copy_diagnostics_button = QtWidgets.QPushButton("Copy diagnostics")
        self._copy_diagnostics_button.clicked.connect(self._on_copy_diagnostics)

        # Measured once from the live font and handed to every section, so
        # "Default agent", "Whisper endpoint" and "Data folder" share one
        # label column and one rhythm instead of each `_Section` sizing
        # its own — see `_GridMetrics`.
        grid_metrics = _GridMetrics.measure()

        agents_section = _Section("Agents", self, expanded=True, grid=grid_metrics)
        agents_section.add_widget(self._agents_view)

        behaviour_section = _Section("Behaviour", self, expanded=True, grid=grid_metrics)
        behaviour_section.add_row("Default agent", self._default_agent_combo)
        behaviour_section.add_checkbox(self._autostart_checkbox)

        updates_section = _Section(
            "Updates & notices", self, expanded=True, grid=grid_metrics
        )
        updates_section.add_checkbox(self._check_updates_checkbox)
        updates_section.add_checkbox(self._show_announcements_checkbox)

        voice_section = _Section("Voice", self, expanded=True, grid=grid_metrics)
        voice_section.add_row("Whisper endpoint", self._whisper_edit)

        privacy_section = _Section("Privacy", self, expanded=False, grid=grid_metrics)
        privacy_section.add_checkbox(self._telemetry_checkbox)

        data_section = _Section("Data", self, expanded=False, grid=grid_metrics)
        data_section.add_row("Data folder", data_dir_row)
        data_section.add_action_row(self._copy_diagnostics_button)

        # Kept as attributes (not just locals) so a test can reach a given
        # section's grid directly, the same way `test_ui_settings.py`
        # already reaches `view._autostart_checkbox` etc. — see
        # `test_settings_grid_alignment.py`.
        self._agents_section = agents_section
        self._behaviour_section = behaviour_section
        self._updates_section = updates_section
        self._voice_section = voice_section
        self._privacy_section = privacy_section
        self._data_section = data_section

        rail = QtWidgets.QWidget()
        rail_layout = QtWidgets.QVBoxLayout(rail)
        rail_layout.setContentsMargins(0, 8, 0, 24)
        rail_layout.setSpacing(grid_metrics.section_gap)
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

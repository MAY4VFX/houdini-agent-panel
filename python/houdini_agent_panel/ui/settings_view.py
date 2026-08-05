"""Settings screen — the design.md set of fields, plus the agents section.

The "Agents" block (the registry six plus custom agents: install / update /
remove) lives at the top of this screen instead of on its own page. Switching
between already-installed agents now happens from the header chip's dropdown
(`ui/chips.py`), so a whole separate screen dedicated to "which agent to run"
stopped earning its keep — see `AgentPanel._open_agent_management`, which is
just "open settings, scroll to top".

Everything is grouped into collapsible `_Section`s (Agents / Behaviour /
Updates & notices / Voice / Privacy / Network / Data) inside a fixed-width,
centered rail — the same 736 px column the feed and composer use — instead
of one long form stretched edge to edge.

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
# ("Whisper endpoint", "Data folder", agent status) landed wherever the
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
_ROW_LABELS = ("Whisper endpoint", "Data folder", "Proxy", "No proxy", "CA bundle")


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
    - `label_width` = the widest `QLabel.sizeHint()` among `_ROW_LABELS`,
      measured directly rather than estimated. At this codebase's default
      font that's 104px ("Whisper endpoint" — still the widest after
      "Default agent" was removed as a row entirely, issue owner's call).
      Below `_MIN_RAIL_WIDTH` (180px) that leaves the value column under
      50px — cramped, but not broken: `QLineEdit` scrolls instead of
      truncating, and the data-folder path label already wraps
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
    #: Forwarded from `AgentsView`, `agent_id` — the panel is the one that
    #: actually knows how to act on it (open the sign-in screen directly for
    #: the agent this tab is already connected to, or switch onto a
    #: different one first — `AgentPanel._on_agent_row_sign_in`); this view
    #: only knows registry/runtime/settings, never the connection.
    sign_in_requested = Signal(str)
    #: Same, for "Sign out" (`AgentPanel._on_agent_row_sign_out`).
    sign_out_requested = Signal(str)
    #: Fired only by the three Network fields (proxy/no-proxy/CA bundle),
    #: never by any other field — an agent reads its environment once, at
    #: spawn (docs/2026-08-03-proxy-support.md), so ONLY these three leave a
    #: running agent out of date; every other setting takes effect through
    #: the ordinary `changed` reload. Drives this view's own restart banner
    #: (see `_on_network_field_changed`); nothing outside this file needs
    #: to know a network field specifically changed, so it isn't forwarded
    #: any further.
    proxy_changed = Signal()
    #: The restart banner's own button. Restarting the agent process is the
    #: panel's job, not this view's — it knows `shared_client`, `_pool`,
    #: session persistence, none of which this screen has any business
    #: touching (`AgentPanel._restart_agent`).
    restart_agent_requested = Signal()

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
        #: True from the moment a Network field is edited until the restart
        #: is either done (button clicked) or explicitly dismissed
        #: (`_on_network_section_toggled`) or the screen reloads. The single
        #: source of truth for "is there a restart the artist hasn't acted
        #: on" — `_restart_banner.isVisible()` alone isn't enough for that,
        #: since Qt reports a child as not visible whenever its ANCESTOR
        #: (here, the collapsed section body) is hidden, regardless of the
        #: child's own state; this flag doesn't have that ambiguity.
        self._restart_pending = False

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
        self._agents_view.sign_out_requested.connect(self.sign_out_requested.emit)

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

        # --- Network — see docs/2026-08-03-proxy-support.md and issue #26.
        # `settings.py`'s `proxy_url`/`no_proxy`/`ca_bundle` and their
        # translation into environment variables (`proxy.py`) and the
        # panel's own downloads (`network.py`) already exist and are already
        # wired up (`AgentPanel._apply_network_settings`, called at startup
        # and on every `changed`); this section is only the missing UI for
        # fields that already work.
        self._proxy_edit = QtWidgets.QLineEdit()
        self._proxy_edit.setPlaceholderText(
            "http://proxy.studio.local:8080 (blank = inherit from the machine)"
        )
        self._proxy_edit.textChanged.connect(self._on_network_field_changed)

        self._no_proxy_edit = QtWidgets.QLineEdit()
        self._no_proxy_edit.setPlaceholderText(
            "extra hosts to bypass, comma-separated — localhost is always excluded"
        )
        self._no_proxy_edit.textChanged.connect(self._on_network_field_changed)

        self._ca_bundle_edit = QtWidgets.QLineEdit()
        self._ca_bundle_edit.setPlaceholderText("/path/to/ca-bundle.pem")
        self._ca_bundle_edit.textChanged.connect(self._on_network_field_changed)
        self._browse_ca_bundle_button = QtWidgets.QPushButton("Browse…")
        self._browse_ca_bundle_button.clicked.connect(self._on_browse_ca_bundle)

        ca_bundle_row = QtWidgets.QHBoxLayout()
        # Same reasoning as `data_dir_row` above: a nested layout's own
        # margins aren't guaranteed zero, and this row sits in the same
        # value column as every other field.
        ca_bundle_row.setContentsMargins(0, 0, 0, 0)
        ca_bundle_row.addWidget(self._ca_bundle_edit, 1)
        ca_bundle_row.addWidget(self._browse_ca_bundle_button)

        # The honest caption the field trio needs to be trustworthy, not
        # decoration — stated plainly, no hedging: what blank means, where
        # a typed password actually ends up, and the one thing that is
        # never sent through the proxy regardless of what's typed above.
        self._network_caption = QtWidgets.QLabel(
            "Blank fields fall back to whatever the machine already exports. "
            "A password typed into the proxy URL is written to settings.json "
            "as plain text — prefer a proxy with no login, or one restricted "
            "by IP. localhost is never sent through the proxy. HTTP/HTTPS "
            "only — SOCKS is not supported."
        )
        self._network_caption.setWordWrap(True)
        # Muted, same idiom `agents.py` already uses for secondary text
        # (`version_label`/`_state_label`) — a live palette role through Qt's
        # `palette()` stylesheet function, never a hex literal (`test_theme.py`
        # forbids those in `ui/**`).
        self._network_caption.setStyleSheet("color: palette(disabled, text);")

        # The restart banner: hidden until a Network field is actually
        # edited (`_on_network_field_changed`), never shown for any other
        # setting. Lives inside the section itself — right where the
        # artist's eyes already are the moment they type — rather than a
        # separate global notice. Names BOTH ways the new value actually
        # reaches the agent, not just the button: a restart is not the only
        # path (`AgentPanel._switch_agent_process` always launches with
        # whatever is in settings.json right now), so collapsing this
        # section without clicking the button is a real choice the artist
        # can make, not a dead end they were never told about — see
        # `_on_network_section_toggled`.
        self._restart_banner = QtWidgets.QWidget()
        self._restart_label = QtWidgets.QLabel(
            "The agent will pick this up after a restart — or the next time you switch agents."
        )
        self._restart_label.setWordWrap(True)
        self._restart_button = QtWidgets.QPushButton("Restart agent")
        self._restart_button.clicked.connect(self._on_restart_agent_clicked)
        restart_banner_layout = QtWidgets.QHBoxLayout(self._restart_banner)
        restart_banner_layout.setContentsMargins(0, 0, 0, 0)
        restart_banner_layout.addWidget(self._restart_label, 1)
        restart_banner_layout.addWidget(self._restart_button)
        self._restart_banner.setVisible(False)

        # Measured once from the live font and handed to every section, so
        # "Default agent", "Whisper endpoint" and "Data folder" share one
        # label column and one rhythm instead of each `_Section` sizing
        # its own — see `_GridMetrics`.
        grid_metrics = _GridMetrics.measure()

        agents_section = _Section("Agents", self, expanded=True, grid=grid_metrics)
        agents_section.add_widget(self._agents_view)

        # No "Default agent" field here any more (owner's call, seeing it
        # live: "непонятно, какая модель дефолта выбрана, в меню этого не
        # нужно" — a second control for a fact the header chip already
        # decides). `settings.default_agent` still exists and still works
        # exactly as before — the last agent actually picked from the
        # header chip's menu (`AgentPanel._on_agent_chosen`) — this screen
        # just no longer shows or lets you set it directly.
        behaviour_section = _Section("Behaviour", self, expanded=True, grid=grid_metrics)
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

        # Collapsed by default (issue #26) — same rank as Privacy/Data: an
        # artist on a studio with no proxy never needs to open this.
        network_section = _Section("Network", self, expanded=False, grid=grid_metrics)
        network_section.add_row("Proxy", self._proxy_edit)
        network_section.add_row("No proxy", self._no_proxy_edit)
        network_section.add_row("CA bundle", ca_bundle_row)
        network_section.add_widget(self._network_caption)
        network_section.add_widget(self._restart_banner)
        # `.toggled` (not `.clicked`, which `_Section._on_toggled` itself
        # uses) — it fires for a real click AND for a programmatic
        # `setChecked()` alike, so a test can drive this the same way
        # `reload()`/`_expand_network_section` already do elsewhere,
        # without a second manual call.
        network_section._toggle.toggled.connect(self._on_network_section_toggled)

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
        self._network_section = network_section
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
            network_section,
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
        # NOT `AlignHCenter` — see `resizeEvent`. Centering here would center
        # within `content`'s own width, which is the scroll viewport's width:
        # a few pixels narrower than `self.width()` the moment the page is
        # tall enough to need a scrollbar. `_header_rail` sits OUTSIDE the
        # scroll area and has no such scrollbar, so it kept centering a few
        # pixels further right than `rail` did — everything inside settings
        # read as sticking out past the back button. `resizeEvent` sets an
        # explicit left margin computed from `self.width()` instead, the
        # same reference `_header_rail` centers against, so the two always
        # agree regardless of whether a scrollbar happens to be showing.
        content_layout.addWidget(rail, 0, QtCore.Qt.AlignLeft)
        self._content_layout = content_layout

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
        # Both rails get the same WIDTH above, but that alone doesn't put
        # them at the same X — `_header_rail` centers within `self.width()`
        # (it lives directly in the top-level layout), while `_rail` lives
        # inside a `QScrollArea` and would center within the VIEWPORT's
        # width if left to its own `AlignHCenter` (see where `content_
        # layout` is built) — a scrollbar reduces that by its own width the
        # moment the page is tall enough to need one, which the header
        # never loses. Centering `_rail` against `self.width()` explicitly,
        # same reference as the header, keeps both rails' left edges
        # together whether or not a scrollbar happens to be showing right
        # now — nothing here depends on the scrollbar's current state.
        margin = max(0, (self.width() - width) // 2)
        self._content_layout.setContentsMargins(margin, 0, 0, 0)

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

    def refresh_agent_auth(self) -> None:
        """Forwarded to the embedded `AgentsView` — see its `refresh_auth_rows`."""
        self._agents_view.refresh_auth_rows()

    def reload(self) -> None:
        """Re-read `settings.json` from disk and refresh the controls without
        writing it back (otherwise an external file change would loop back
        through `_on_field_changed`)."""
        self._loading = True
        try:
            current = settings_module.load()
            self._autostart_checkbox.setChecked(current.autostart_agent)
            self._check_updates_checkbox.setChecked(current.check_updates)
            self._show_announcements_checkbox.setChecked(current.show_announcements)
            self._telemetry_checkbox.setChecked(current.telemetry)
            self._whisper_edit.setText(current.whisper_endpoint)
            self._proxy_edit.setText(current.proxy_url)
            self._no_proxy_edit.setText(current.no_proxy)
            self._ca_bundle_edit.setText(current.ca_bundle)
            # A reload is a fresh read of what's on disk, not an edit — the
            # invitation to restart only belongs to an edit THIS screen just
            # made (`_on_network_field_changed`).
            self._restart_pending = False
            self._restart_banner.setVisible(False)
            self._data_dir_label.setText(str(paths.data_dir()))
        finally:
            self._loading = False

    # --- internal ------------------------------------------------------

    def _save_from_fields(self) -> "settings_module.Settings":
        """Read every field on the screen into a freshly-loaded `Settings`
        and save it. The one place that knows the full field list, shared
        by `_on_field_changed` and `_on_network_field_changed` — those
        differ only in what they do AFTER saving (the latter also shows the
        restart banner and fires `proxy_changed`), not in what gets saved."""
        current = settings_module.load()
        # `default_agent` is deliberately NOT set from anything on this
        # screen — see the comment where `behaviour_section` is built.
        # Loaded above and saved back below unchanged.
        current.autostart_agent = self._autostart_checkbox.isChecked()
        current.check_updates = self._check_updates_checkbox.isChecked()
        current.show_announcements = self._show_announcements_checkbox.isChecked()
        current.telemetry = self._telemetry_checkbox.isChecked()
        current.whisper_endpoint = self._whisper_edit.text().strip()
        current.proxy_url = self._proxy_edit.text().strip()
        current.no_proxy = self._no_proxy_edit.text().strip()
        current.ca_bundle = self._ca_bundle_edit.text().strip()
        settings_module.save(current)
        return current

    def _on_field_changed(self, *_args: object) -> None:
        if self._loading:
            return
        self._save_from_fields()
        self.changed.emit()

    def _on_network_field_changed(self, *_args: object) -> None:
        """One of the three Network fields changed — see `proxy_changed`'s
        docstring for why this is not just `_on_field_changed`."""
        if self._loading:
            return
        self._save_from_fields()
        self._restart_pending = True
        self._restart_banner.setVisible(True)
        self.changed.emit()
        self.proxy_changed.emit()

    def _on_network_section_toggled(self, expanded: bool) -> None:
        """Collapsing "Network" while a restart is pending is treated as an
        explicit dismissal, not a silent loss of the fact — the two are
        different things. A locked-open section (never letting the artist
        collapse it until they click "Restart agent") was the other option
        considered; rejected because it punishes an artist who just wants
        to glance at another field for looking at this one first, and
        because the banner's own text already names a way to get the new
        setting into the agent that ISN'T this button — restarting is not
        the only door out, so collapsing without pressing it is a real
        choice, not a trap. Re-expanding starts clean: nothing is carried
        that the screen doesn't also show, i.e. this is never a case of
        "the panel knows the agent is stale and says nothing about it" —
        it said so once, plainly, and the artist chose to move on.
        """
        if not expanded and self._restart_pending:
            self._restart_pending = False
            self._restart_banner.setVisible(False)

    def _on_browse_ca_bundle(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "CA bundle", "", "Certificates (*.pem *.crt *.cer);;All files (*)"
        )
        if path:
            # Triggers `_on_network_field_changed` through the field's own
            # `textChanged` — nothing else to do here.
            self._ca_bundle_edit.setText(path)

    def _on_restart_agent_clicked(self) -> None:
        self._restart_pending = False
        self._restart_banner.setVisible(False)
        self.restart_agent_requested.emit()

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

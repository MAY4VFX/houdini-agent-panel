"""Shared visual decisions for the feed widgets.

Not part of the public contract in docs/architecture.md §10 — a helper module
`chips.py`, `transcript.py` and `permissions.py` lean on so their spacing,
fonts and colours don't drift apart. The panel lives inside Houdini as a guest
in someone else's window, so colour is never hardcoded as hex — Houdini's
theme can be dark, light or entirely custom (see facts/houdini.md §5).

`QApplication.palette()` is THE source of colour — `color()`/`accent_color()`
and everything built on them read it directly, nothing else. Houdini fills
the live Qt palette from whatever theme is active, and does so identically
on every version the panel supports: `QPalette.Highlight` is the accent,
`Window`/`Base`/`Text`/`Mid` are surfaces and borders, one code path for
20.5, 21 and 22, no version checks.

This used to also consult `hou.qt.getColor(name)` — the `.hcs` scheme files
— FIRST, for roles with a checked scheme-name analog (`_HOU_COLOR_NAMES`,
now gone). That table mapped every one of its roles to something the
palette already covers directly, so once the palette leads, the `.hcs` path
had nothing left to do there — keeping it "for symmetry" would have been a
dead mapping dressed up as a working mechanism.

The deeper reason the palette leads, not just here but for `accent_color()`
too: a Houdini 22 "Edit Theme" preset (52 of them in
`$HFS/houdini/config/Themes/default.theme.json`, each an HSV triple)
recolours the live palette — that part is certain, it's the only way
`QApplication.palette()` could show Plumtree's own tone at all. What is
NOT established is whether `hou.qt.getColor("SomeSchemeName")` follows that
same recolouring or keeps answering from the static `.hcs` file underneath
it — that would need calling `hou.qt.getColor` inside a GUI session with a
preset active, and `hou.qt` doesn't exist even in `hython` on either
20.5.445 or 22.0.368 (that part IS confirmed, by running it), so this
project has never been able to check it either way. The palette needs no
such check — it works identically on 20.5, 21 and 22, preset active or
not — so it goes first regardless of how that open question resolves.
`accent_color()` keeps a narrow `.hcs` fallback of its own (only reached if
the palette has no usable `Highlight` at all, which hasn't been observed)
— see its docstring for the same reasoning spelled out for the accent
specifically.

`hou` is only ever imported lazily, inside a try/except (`_hou_scheme_color`)
— this module has to work with no `hou` on the path at all (unit tests,
`dev_preview`), and `hou.qt` specifically doesn't exist even inside `hython`
on either 20.5.445 or 22.0.368 (confirmed), so nothing here can depend on it
answering at all, let alone correctly.
"""

from __future__ import annotations

from .qt import QtCore, QtGui, QtWidgets

# --- geometry ----------------------------------------------------------

SPACING = 6
SPACING_TIGHT = 3
MARGIN = 8
RADIUS = 4
ICON_SIZE = 16

#: The ten ACP tool-call kinds (facts/acp-sdk.md §4, ToolKind).
TOOL_KINDS: tuple[str, ...] = (
    "read",
    "edit",
    "delete",
    "move",
    "search",
    "execute",
    "think",
    "fetch",
    "switch_mode",
    "other",
)

_KIND_GLYPH: dict[str, str] = {
    "read": "R",
    "edit": "E",
    "delete": "D",
    "move": "M",
    "search": "S",
    # Execution is a tool KIND, not a failure state. ``X`` read as an
    # unfinished/failed step beside the separate ``✓ done`` status.
    "execute": "•",
    "think": "T",
    "fetch": "F",
    "switch_mode": "⇄",
    "other": "•",
}

_STATUS_GLYPH: dict[str, str] = {
    "pending": "○",
    "in_progress": "◐",
    "completed": "✓",
    "failed": "✕",
}

_STATUS_LABEL: dict[str, str] = {
    "pending": "pending",
    "in_progress": "running",
    "completed": "done",
    "failed": "failed",
}


def palette() -> QtGui.QPalette:
    """The host's (Houdini's) palette, not our own — the only source of colour."""
    return QtWidgets.QApplication.palette()


def _hou_scheme_color(name: str) -> QtGui.QColor | None:
    """`hou.qt.getColor(name)`, or `None` for any reason at all.

    `hou` may not be importable (outside Houdini entirely), `hou.qt` may not
    exist (confirmed absent in `hython` on both 20.5.445 and 22.0.368), or
    the name may not exist in whatever scheme is active. All three, and
    anything else, mean "there is nothing usable here" — this never raises.
    The only remaining caller is `accent_color()`'s narrow fallback; `color()`
    below no longer consults `hou` at all (see the module docstring).
    """
    try:
        import hou
    except ImportError:
        return None
    try:
        if not hou.isUIAvailable():
            return None
        return hou.qt.getColor(name)
    except Exception:  # noqa: BLE001 - a theme lookup has no right to break paint code
        return None


def color(
    role: QtGui.QPalette.ColorRole,
    group: QtGui.QPalette.ColorGroup = QtGui.QPalette.Active,
) -> QtGui.QColor:
    """The live Qt palette — the one entry point every call site goes
    through instead of reading `self.palette()`/`QApplication.palette()`
    directly (a test greps for that).

    No `hou.qt.getColor` lookup happens here any more (see the module
    docstring for why: it used to run first, for a table of roles that all
    had a direct palette equivalent anyway, and it couldn't be trusted to
    follow a Houdini 22 "Edit Theme" preset). If a role ever needs a Houdini
    scheme name with no palette analog at all, that's a new, individually
    justified function — the way `accent_color()` keeps its own narrow
    `.hcs` fallback — not a reason to route every role through `hou` again.
    """
    return palette().color(group, role)


def monospace_font() -> QtGui.QFont:
    """System monospace font — for code/diffs inside an expanded tool call."""
    return QtGui.QFontDatabase.systemFont(QtGui.QFontDatabase.FixedFont)


def scaled_size(value: int) -> int:
    """A Houdini Global-UI-Size-aware pixel value, or ``value`` elsewhere."""
    try:
        import hou
    except ImportError:
        return value
    try:
        if hou.isUIAvailable():
            return int(hou.ui.scaledSize(value))
    except Exception:  # noqa: BLE001 - host styling must never break a widget
        pass
    return value


def kind_glyph(kind: str) -> str:
    """A one-letter or symbol badge for a tool call's kind."""
    return _KIND_GLYPH.get(kind, _KIND_GLYPH["other"])


def kind_icon(kind: str, *, size: int = ICON_SIZE) -> QtGui.QIcon:
    """Tool-call icon for a `kind`.

    Houdini's own icons (`hicon:/SVGIcons.index?...`) aren't reachable
    outside Houdini and their exact names aren't confirmed in facts/ — rather
    than guess, we draw a small text badge in `color()`'s colours, so it
    tracks Houdini's own live theme wherever that is available. Behaves
    identically in unit tests and inside Houdini, and needs no `hou`.
    """
    pixmap = QtGui.QPixmap(size, size)
    pixmap.fill(QtCore.Qt.transparent)

    painter = QtGui.QPainter(pixmap)
    try:
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        painter.setBrush(color(QtGui.QPalette.Mid))
        painter.setPen(QtCore.Qt.NoPen)
        painter.drawRoundedRect(0, 0, size, size, RADIUS, RADIUS)

        font = painter.font()
        font.setPointSize(max(6, size // 2))
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(color(QtGui.QPalette.Text))
        painter.drawText(pixmap.rect(), QtCore.Qt.AlignCenter, kind_glyph(kind))
    finally:
        painter.end()

    return QtGui.QIcon(pixmap)


def status_glyph(status: str) -> str:
    """Status symbol — shape, not colour: the only signal that survives any theme."""
    return _STATUS_GLYPH.get(status, _STATUS_GLYPH["pending"])


def status_label(status: str) -> str:
    """Human-readable status caption for the collapsible tool-call row."""
    return _STATUS_LABEL.get(status, status)


#: Scheme names consulted for the accent, and ONLY as a refinement — see
#: `accent_color`. Both are present in `UIDark.hcs` and `UILight.hcs` under
#: 20.5 and 22 alike.
_ACCENT_SCHEME_NAMES: tuple[str, ...] = ("SelectedTextBG", "ActiveHandleColor")


def accent_color() -> QtGui.QColor:
    """The active theme's accent — read from the Qt palette, not from a file.

    The mode chip, the sidebar's busy/unread dots, a pinned conversation and
    the checked item in every popup all read this instead of a hardcoded
    hex, so changing Houdini's theme actually changes what they look like.

    Source order is deliberate and was got wrong once. `QPalette.Highlight`
    comes FIRST because Houdini fills the palette from whatever theme is
    live, and it does so on every version the panel supports — colour themes
    as an artist-facing feature are new in Houdini 22 (52 presets in
    `$HFS/houdini/config/Themes/default.theme.json`, each an HSV triple),
    and 20.5 and 21 have no such file at all. One palette read covers all
    three with no version checks.

    The `.hcs` scheme names are consulted only where the palette has no
    usable highlight, and never in front of it: `SelectedTextBG` resolves to
    `SELECTION_BASE`, which is `HSV 40 0.825 0.725` — the stock amber. If
    that lookup ignores the chosen preset (it reads scheme files, and
    whether a Houdini 22 theme rewrites them is NOT something this project
    has verified — `hou.qt` does not exist in `hython`, so it cannot be
    checked outside a GUI session), putting it first would hand back amber
    under a pink theme and reintroduce exactly the bug this replaced.
    """
    from_palette = palette().color(QtGui.QPalette.Highlight)
    if from_palette.isValid():
        return from_palette
    for name in _ACCENT_SCHEME_NAMES:
        found = _hou_scheme_color(name)
        if found is not None:
            return QtGui.QColor(found)
    return from_palette


def to_hex(color_value: QtGui.QColor) -> str:
    """`#rrggbb` for the stylesheet strings the popup surfaces build by hand.

    `setStyleSheet` takes a literal, not a `QColor` — this is the one place
    a colour ever turns into text, and it happens at the point of use, from
    whatever `color()`/`accent_color()` return THIS time, never from a
    module-level constant frozen at import time.
    """
    return color_value.name(QtGui.QColor.HexRgb)


def scrollbar_handle_color() -> QtGui.QColor:
    """The scrollbar handle — the window colour pulled a third of the way
    toward the text colour.

    Not a palette role. `Mid` and `Dark` were the obvious candidates and
    both fail the same way: each one reads as "a shade apart" on only one
    of the two theme families and collapses toward the background on the
    other, which is how the drawer ended up with a scrollbar an artist had
    to hunt for. Moving toward `Text` is contrasting by construction —
    whatever the theme, the text is legible against the window, so a third
    of that distance is visible against it too.
    """
    return _blend(palette().color(QtGui.QPalette.Window),
                  palette().color(QtGui.QPalette.Text), 0.34)


def _blend(base: QtGui.QColor, other: QtGui.QColor, amount: float) -> QtGui.QColor:
    """`base` shifted toward `other` by `amount` (0..1) — for tones that need
    to sit a little apart from a palette role (a popup's hover row, the
    tinted background behind a checked item) without inventing a fixed
    colour of our own to do it."""
    amount = max(0.0, min(1.0, amount))
    return QtGui.QColor(
        int(base.red() + (other.red() - base.red()) * amount),
        int(base.green() + (other.green() - base.green()) * amount),
        int(base.blue() + (other.blue() - base.blue()) * amount),
    )


def _luminance(color_value: QtGui.QColor) -> float:
    return (
        0.299 * color_value.red()
        + 0.587 * color_value.green()
        + 0.114 * color_value.blue()
    )


def _subtle_surface(
    preferred: QtGui.QColor,
    *,
    fallback_amount: float,
    maximum_amount: float = 0.20,
) -> QtGui.QColor:
    """Use a palette surface only while it is a subtle step from Window.

    Houdini 20.5's live Qt5 palette is internally inconsistent: under the
    stock dark scheme ``Window`` is ``#3a3a3a`` and ``Text`` is ``#cccccc``,
    but ``Base`` is pure black and ``AlternateBase`` is ``#989898``. Both
    roles are valid QColors, yet neither means what Qt6/Houdini 22 makes it
    mean for a quiet raised surface. Judge the candidate on the host's own
    Window -> Text contrast axis; if it points the wrong way or travels too
    far, derive the small step that the role was meant to represent.

    This is semantic rather than a Qt-version branch, so custom/light themes
    keep working and any future host with a coherent palette uses its own
    value unchanged.
    """
    window = palette().color(QtGui.QPalette.Window)
    text = palette().color(QtGui.QPalette.Text)
    span = _luminance(text) - _luminance(window)
    if preferred.isValid() and abs(span) > 1.0:
        amount = (_luminance(preferred) - _luminance(window)) / span
        if 0.015 <= amount <= maximum_amount:
            return preferred
    return _blend(window, text, fallback_amount)


def settings_background() -> QtGui.QColor:
    """A quiet overlay shade: AlternateBase when that role is coherent."""
    return _subtle_surface(
        palette().color(QtGui.QPalette.AlternateBase), fallback_amount=0.08
    )


def composer_background() -> QtGui.QColor:
    """The input card surface: Base when it lies on the contrast axis."""
    return _subtle_surface(palette().color(QtGui.QPalette.Base), fallback_amount=0.11)


def composer_border() -> QtGui.QColor:
    """A restrained edge around the input card, never darker than Window."""
    return _subtle_surface(
        palette().color(QtGui.QPalette.Mid), fallback_amount=0.025, maximum_amount=0.12
    )


def quiet_link_color() -> QtGui.QColor:
    """Close enough to the background to not catch the eye — the "Report a
    bug…" footer link's own colour (`Composer._position_bug_report_link`),
    per the owner's own ask: "еле заметная по цвету от фона" (barely
    visible against the background).

    `Window`, nudged toward `Disabled/Text` rather than used at full
    disabled-text strength: plain disabled text already reads as muted,
    but the owner wants closer to invisible than that — findable by
    someone looking for it, unnoticed by someone who isn't. Blending from
    a palette role rather than a fixed grey is what keeps this legible
    (never literally the same colour as the background) on every theme,
    light or dark, instead of picking one value that happens to work on
    whichever theme it was written against.
    """
    return _blend(
        palette().color(QtGui.QPalette.Window),
        palette().color(QtGui.QPalette.Disabled, QtGui.QPalette.Text),
        0.22,
    )


def contrasting_text_color(background: QtGui.QColor) -> QtGui.QColor:
    """Black or white — whichever reads better on `background`.

    For text painted directly onto the accent colour (the primary button in
    a permission prompt): the accent can be a warm amber or a bright
    Plumtree pink, and pairing every possible accent with one fixed text
    tone isn't safe the way pairing it with a `QPalette` role would be, so
    this picks by measured luminance instead of assuming light-on-dark.
    """
    luminance = 0.299 * background.red() + 0.587 * background.green() + 0.114 * background.blue()
    return QtGui.QColor(20, 20, 20) if luminance > 140 else QtGui.QColor(240, 240, 240)


# --- popup / floating-menu surfaces --------------------------------------
#
# The agent switcher, the mode/model choice popups, and a conversation row's
# "more" menu all draw their own flat, non-native surface (design.md: no
# native `QMenu`/`QComboBox` chrome). What that surface is built FROM has to
# be the live theme, not a fixed dark palette — a light Houdini scheme drew
# a dark popup floating on a light panel until these existed.


def popup_background() -> QtGui.QColor:
    """Menu/popup fill from the live palette's coherent surface roles."""
    return settings_background()


def popup_border() -> QtGui.QColor:
    return composer_border()


def popup_hover_background() -> QtGui.QColor:
    """A touch lighter than the resting surface — blended toward the text
    colour rather than a second fixed tone, so it stays inside whatever
    contrast the active theme itself uses."""
    return _blend(popup_background(), palette().color(QtGui.QPalette.Text), 0.12)


def popup_selected_background() -> QtGui.QColor:
    """The tinted background behind a popup's currently-checked item — the
    accent blended low into the surface, not a second hardcoded hex."""
    return _blend(popup_background(), accent_color(), 0.22)


def scrollbar_stylesheet(scope: str = "") -> str:
    """Shared QSS for every scrollbar in the panel.

    Two things, both asked for from a real panel. The stepper arrows go:
    they sat at the ends of the bar looking like controls belonging to the
    list next to them, and nobody uses them — the wheel and the handle do
    the work. And the handle gets `scrollbar_handle_color()` so it is
    actually findable instead of dissolving into the track.

    `scope` prefixes the selectors (e.g. `"QScrollArea#drawerScroll "`) so
    one surface can be styled without reaching into another's popups; empty
    means every scrollbar under the widget this is set on.

    Built fresh from the live theme on each call, like `popup_stylesheet` —
    never a module-level constant, or the colours freeze at import time.
    """
    handle = to_hex(scrollbar_handle_color())
    accent = to_hex(accent_color())
    return (
        f"{scope}QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}"
        f"{scope}QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}"
        f"{scope}QScrollBar::handle:vertical {{"
        f" background: {handle}; min-height: 32px; border-radius: 5px; margin: 2px;"
        f"}}"
        f"{scope}QScrollBar::handle:horizontal {{"
        f" background: {handle}; min-width: 32px; border-radius: 5px; margin: 2px;"
        f"}}"
        f"{scope}QScrollBar::handle:vertical:hover,"
        f"{scope}QScrollBar::handle:horizontal:hover {{ background: {accent}; }}"
        # The arrows. Qt draws them unless every dimension is zeroed.
        f"{scope}QScrollBar::sub-line, {scope}QScrollBar::add-line {{"
        f" height: 0; width: 0; border: none; background: none;"
        f"}}"
        f"{scope}QScrollBar::sub-page, {scope}QScrollBar::add-page {{ background: none; }}"
    )


def popup_stylesheet(frame_object_name: str) -> str:
    """Shared QSS for every free-floating popup surface in the panel.

    Built fresh from the live theme each time a popup is (re)created —
    called from `__init__`/`showEvent`, never stored as a module-level
    constant — so a panel opened under a different Houdini colour scheme (or
    a different `QApplication` palette in tests/the dev preview) gets that
    scheme's own tones, not whatever was active when this module first ran.
    """
    bg = to_hex(popup_background())
    border = to_hex(popup_border())
    hover_bg = to_hex(popup_hover_background())
    # Straight from the palette, same reasoning as `popup_background` above —
    # not `color()`'s `hou.qt`-first path.
    resting_text = to_hex(palette().color(QtGui.QPalette.Disabled, QtGui.QPalette.Text))
    hover_text = to_hex(palette().color(QtGui.QPalette.Text))
    selected_bg = to_hex(popup_selected_background())
    accent = to_hex(accent_color())
    return (
        f"QFrame#{frame_object_name} {{"
        f" background: {bg};"
        f" border: 1px solid {border};"
        " border-radius: 10px;"
        "}"
        "QFrame[popupSeparator=\"true\"] {"
        f" background: {border}; max-height: 1px; min-height: 1px; margin: 4px 6px;"
        "}"
        "QPushButton {"
        " min-height: 30px;"
        " padding: 0 10px;"
        " border: none;"
        " border-radius: 6px;"
        f" color: {resting_text};"
        " background: transparent;"
        " text-align: left;"
        "}"
        f"QPushButton:hover, QPushButton:focus {{ background: {hover_bg}; color: {hover_text}; }}"
        f'QPushButton[checkedChoice="true"] {{ color: {accent}; background: {selected_bg}; }}'
        # A popup that overflows gets the same scrollbar as everything else:
        # no stepper arrows, a handle you can actually see.
        + scrollbar_stylesheet()
    )


def status_color(status: str) -> QtGui.QColor:
    """Status colour — `QPalette` roles only, nothing of our own.

    `QPalette` has no semantic "error" role: painting `failed` red would mean
    hardcoding a colour around the theme's palette, which this module's rules
    forbid. So `completed`/`failed` differ by the shape of the glyph
    (`status_glyph`), not by colour — while `in_progress`/`pending` take a
    visible accent (`Highlight`) and a muted tone (`Disabled`/`Text`) from
    the palette itself.
    """
    if status == "in_progress":
        return color(QtGui.QPalette.Highlight)
    if status == "pending":
        return color(QtGui.QPalette.Text, QtGui.QPalette.Disabled)
    return color(QtGui.QPalette.Text)


__all__ = [
    "ICON_SIZE",
    "MARGIN",
    "RADIUS",
    "SPACING",
    "SPACING_TIGHT",
    "TOOL_KINDS",
    "accent_color",
    "color",
    "contrasting_text_color",
    "kind_glyph",
    "kind_icon",
    "monospace_font",
    "palette",
    "popup_background",
    "popup_border",
    "popup_hover_background",
    "popup_selected_background",
    "popup_stylesheet",
    "status_color",
    "status_glyph",
    "status_label",
    "to_hex",
]

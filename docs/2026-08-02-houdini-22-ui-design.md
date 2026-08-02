# Houdini 22 UI: how to embed the Agent Panel natively

**TL;DR:** no separate public SideFX design system / Figma / UI style guide for Houdini 22 was found.  
The public design contract for H22 is: the new Houdini theme + standard Qt widgets + `hou.qt` + scaling via `hou.ui`; that's enough to make the panel look native across every theme.  
For the Agent Panel, the right move is to inherit the host style, take colors/icons/sizes from Houdini, and reserve custom styling only for chat-specific entities.

Date verified: 2026-08-02. Target local build: Houdini 22.0.368.

## Short answer

SideFX doesn't have a document with visual rules in the style of Material Design — instead
it has a set of executable primitives:

1. Houdini 22 got a new UI skin and a Theme Editor.
2. The Theme Editor derives the whole palette from three semantic colors: Base, Accent, and
   Highlight; the user can change the theme and contrast.
3. `hou.qt` hands back Houdini's own widgets, stylesheet, resource colors, and SVG icons.
4. `hou.ui.scaledSize()` / `globalScaleFactor()` keep sizes in sync with the Global UI Size setting.
5. Python Panel is the standard way to embed a PySide6/PyQt6 widget in a pane tab.

So "blending in natively" doesn't mean reproducing one dark screenshot of H22,
but staying correct under a different Base/Accent/Highlight theme, UI scale, or platform.

Search also turns up a page titled `Style guide`, but it applies only to
info panels inside the viewport's Python state HUD, not to Qt pane tabs: [HUD info
style guide](https://www.sidefx.com/docs/houdini/hom/hud_info.html).

SideFX staff separately clarify the practical ceiling of "nativeness" in H22: the interface
mixes traditional Houdini UI, QML, and QtWidgets; they've been visually aligned as far as
possible, but some differences remain. So a custom Qt panel can be native without having
to be pixel-identical to QML panels ([official forum, SideFX
staff reply](https://www.sidefx.com/forum/topic/104193/)).

## What SideFX actually documents

### 1. H22 — a new theme-driven UI

SideFX states directly that Houdini 22 introduces a new UI skin, configurable through the Theme
Editor; the old UI is deprecated and will disappear in future versions. The Theme Editor builds
the palette from Base, Accent, and Highlight and heuristically maintains contrast and
legibility. Accent is used for buttons and the colored parts of UI icons, Highlight is used
for the current/selected state. Light themes are currently labeled experimental, but they already
exist, so code shouldn't assume a dark background.

Sources:

- [What's new: User interface and viewport](https://www.sidefx.com/docs/houdini/news/22/viewport.html)
- [Theme Editor](https://www.sidefx.com/docs/houdini/ref/windows/theme_editor.html)

### 2. Python Panel — the standard container

A Python Panel embeds a PySide6/PyQt6 interface into a pane tab. The root widget is
returned by `onCreateInterface()`. Qt UI and `hou.PythonPanel` methods must be called
only from Houdini's main thread.

Sources:

- [Python Panel](https://www.sidefx.com/docs/houdini/ref/panes/pythonpanel.html)
- [Python Panel Editor](https://www.sidefx.com/docs/houdini/ref/windows/pythonpaneleditor.html)
- [`hou.PythonPanel`](https://www.sidefx.com/docs/houdini/hom/hou/PythonPanel.html)
- [HOM Qt cookbook](https://www.sidefx.com/docs/houdini/hom/cb/qt.html)

### 2.1. The exact H22 stack, and an important discrepancy about imports

The official platform page for Houdini 22 lists Qt 6.8.3, PySide6 6.8.3, and
Python 3.13.10 (a Python 3.11 build is also available); there's no more Qt 5 build.

Source: [Houdini 22 system requirements and supported platforms](https://www.sidefx.com/docs/houdini/news/22/platforms.html).

Official H22 examples show a direct `from PySide6 import ...`, and comments in several factory
`.pypanel` files from the local install explicitly call `hutil.PySide` internal-use only and
advise third-party code to import PySide directly. This contradicts our own project rule of
"Qt only through `hutil.PySide`."

The project's decision remains a deliberate compatibility layer for one codebase across H20.5
and H22, but it must not be presented as something recommended by SideFX's public H22
documentation. This is our own trade-off; if compatibility issues show up, the shim will be
the first suspect.

### 3. Inheriting the Houdini stylesheet

`hou.qt.styleSheet()` returns the Houdini stylesheet. If you pass it a path to your own
QSS, Houdini expands its own placeholders inside it: resource colors like
`@MenuBG@` and scale-aware sizes like `@14px@`. Child widgets inherit their parent's style;
`hou.qt.mainWindow()`'s documentation separately confirms that a widget parented to the
main window inherits the Houdini stylesheet.

This gives two modes:

- ordinary widgets inside a Python Panel: don't recolor anything globally, let
  Qt inherit the host style;
- a chat-specific widget: load a small local QSS via
  `hou.qt.styleSheet(path)`, using Houdini's tokens instead of hex values and plain `px`.

Sources:

- [`hou.qt.styleSheet`](https://www.sidefx.com/docs/houdini/hom/hou/qt/styleSheet.html)
- [`hou.qt.mainWindow`](https://www.sidefx.com/docs/houdini/hom/hou/qt/mainWindow.html)

### 4. Colors — resource names, not hex

`hou.qt.getColor(name)` returns a `QColor` for a named Houdini resource color;
the names are defined in the current `.hcs` color scheme files. For custom painting this is a
more accurate Houdini API than copying RGB values out of the factory dark theme. For plain
`QWidget`s it's even simpler to just use the current `QPalette`.

A practical split:

- `QPalette.Window`, `Text`, `Base`, `Button`, `Highlight` — for basic custom painting;
- `hou.qt.getColor("SecondaryText")`, `getColor("IconError")`, and other resource
  tokens — only when semantics missing from `QPalette` are needed;
- don't cache a computed color as a permanent constant: the user can switch
  themes mid-session.

Source: [`hou.qt.getColor`](https://www.sidefx.com/docs/houdini/hom/hou/qt/getColor.html).

The last point, about refreshing the cache, is our own engineering recommendation, not an
explicitly stated SideFX rule.

### 5. Icons — the standard Houdini icon registry

`hou.qt.Icon(icon_name)` builds a `QIcon` from a Houdini icon name. Names can be browsed
via the file chooser under `hicon://`; the old `hou.qt.createIcon()` is deprecated. This is
preferable to letter badges and Unicode symbols wherever Houdini already has a clear
semantic icon.

Sources:

- [`hou.qt.Icon`](https://www.sidefx.com/docs/houdini/hom/hou/qt/Icon.html)
- [`hou.qt.createIcon` (deprecated)](https://www.sidefx.com/docs/houdini/hom/hou/qt/createIcon.html)

### 6. Sizes — Global UI Size

`hou.ui.scaledSize(n)` scales a hard-coded size according to Houdini's Global UI Size.
`hou.ui.globalScaleFactor()` is needed wherever an API takes a raw factor, e.g. for
`QWebEngineView`. QSS placeholders `@Npx@` express the same principle declaratively.

Source: [`hou.ui`](https://www.sidefx.com/docs/houdini/hom/hou/ui.html).

### 6.1. Fonts — inherit, don't fix

No public typography scale or `hou.qt.getFont(token)` was found. The safe
contract is to inherit the application/widget font, changing only weight or size
relative to the current font. H22 bundles Routed Gothic, but the platform page doesn't promise
it as a stable public UI-font contract. The factory QSS uses `@FontFixed@`,
but the public documentation explicitly guarantees only the placeholder mechanism itself, not
a full, stable list of font tokens.

Practical takeaway: ordinary text inherits the host font; for code,
`QFontDatabase.systemFont(QFontDatabase.FixedFont)` fits, and the project already uses it.

### 7. Ready-made Houdini-look widgets

The public `hou.qt` includes `Menu`, `MenuButton`, `ComboBox`, `SearchLineEdit`,
`FieldLabel`, `Separator`, `ToolTip`, `HelpButton`, `FileChooserButton`,
`NodeChooserButton`, `GridLayout`, and other classes explicitly documented as widgets with
Houdini look and feel or stable cross-platform layout geometry.

For the Agent Panel, particularly relevant ones are:

- `hou.qt.Menu` / `MenuButton` for picking the agent, mode, and session;
- `hou.qt.SearchLineEdit` on the agents screen;
- `hou.qt.Separator` instead of drawn lines;
- `hou.qt.ToolTip` for Houdini-native tooltips;
- `hou.qt.Icon` for toolbar actions.

Source: [`hou.qt` package](https://www.sidefx.com/docs/houdini/hom/hou/qt/index.html).

## What the installed Houdini 22.0.368 shows

These are observations from factory files, not a promised public API.

Checked:

- `$HFS/houdini/config/Styles/base.qss`;
- `$HFS/houdini/config/UIDark.hcs` and `UILight.hcs`;
- `$HFS/houdini/config/Icons/SVGIcons.index`;
- 35 factory `.pypanel` files under `$HFS/houdini/python_panels`.

The local build confirms Python 3.13.10, Qt 6.8.3, and PySide6 6.8.3.

Observations:

- the factory QSS itself uses tokens (`@BackColor@`, `@TextColor@`,
  `@ButtonGradHi@`, `@SelectedTextBG@`) and scale placeholders (`@1px@`, `@17px@`);
- the base geometry is dense: a line edit and a tool button are about 17 px at normal scale,
  tabs are about 20 px, checkbox/radio indicators are about 14 px;
- corner radii are restrained: a tool button is 4 px, a group box is 5 px, tabs are square;
- native controls matter more than custom card styling: lists, fields, menus, tabs, and scrollbars
  are already handled consistently by the stylesheet;
- the standard panels are mostly a thin `.pypanel` wrapper over a Qt/QML module,
  rather than each copying a shared stylesheet into its own interface;
- the factory code itself occasionally calls `setStyleSheet(hou.qt.styleSheet())` for detached
  menus and complex panel roots.

None of this means you should copy `base.qss` or depend on its exact
selectors: the file is internal and can change. It's useful as a visual reference,
while `hou.qt`'s public methods are the runtime contract.

### Which standard panels are worth studying as reference

- `hrecipes/manager.py` (Recipe Manager) — compact header, notices, search/list
  states;
- `lightmixer/lmui.py` and `lmmixer.py` (Light Mixer) — toolbar, tree, custom cells,
  scoped QSS;
- `scenegraphdetails/` — split panes, toolbar, tables, a secondary hierarchy;
- `pdgdatalayer/datalayerpanel.py` — an explicit `hou.qt.styleSheet()` call and scaled geometry;
- `paintinstances/panel.py`, `charpicker/controlbutton.py` — `hou.qt.Icon`,
  `getColor`, state styling.

These are local implementation references for H22.0.368, not public APIs. The private
dynamic properties from the factory QSS (`plain`, `transparent`, `field_label`) shouldn't be
copied into the product.

## Recommended visual approach for the Agent Panel

### Decision hierarchy

1. Start with a standard Qt widget and no local stylesheet.
2. If a suitable public `hou.qt` widget exists — use it.
3. For custom painting — the current `QPalette`; for Houdini-specific semantics —
   `hou.qt.getColor()`.
4. For custom QSS — a separate short file using `@ColorToken@` and `@Npx@`, processed via
   `hou.qt.styleSheet(path)`.
5. For layout/custom-paint sizes — one `scaled(n)` adapter wrapping
   `hou.ui.scaledSize(n)` with a fallback of `n` for tests outside Houdini.
6. Icons — `hou.qt.Icon`; a custom SVG only when the Houdini registry has no
   matching product semantics.

The project's import rule stays stronger than SideFX's examples: Qt is only taken through
`hutil.PySide`; `hou` is only allowed on the UI/main thread and through a small host adapter,
so widgets stay testable without Houdini.

### A minimal adapter

This is a direction for the code, not a ready-made patch:

```python
# ui/host_style.py — called only from Houdini's UI/main thread
try:
    import hou
except ImportError:
    hou = None

from .qt import QtGui, QtWidgets


def scaled(value: int) -> int:
    return hou.ui.scaledSize(value) if hou is not None else value


def icon(name: str) -> QtGui.QIcon:
    if hou is not None and hou.qt.canCreateIcon(name):
        return hou.qt.Icon(name)
    return QtGui.QIcon()


def color(resource: str, fallback_role=QtGui.QPalette.Text) -> QtGui.QColor:
    if hou is not None:
        try:
            return hou.qt.getColor(resource)
        except hou.OperationFailed:
            pass
    return QtWidgets.QApplication.palette().color(fallback_role)
```

In a real implementation, the `hou` import should be isolated even more strictly, so the
module can't accidentally be called from the ACP worker thread.

### What to do with the chat, which has no standard Houdini control

The transcript, tool call, permission request, and composer have no direct equivalents in HOM.
A custom visual language is fine here, but it should read as "a layer on top of Houdini":

- messages — no bubble-card border by default; separated by spacing and typography;
- tool call — a compact row the height of a native control, expandable on click;
- permission — standard `QPushButton`s, the primary action styled via Accent/Highlight, not a
  brand hex value;
- composer — a `QPlainTextEdit`/`QTextEdit` with the host field background and native border;
- toolbar actions — `QToolButton` with Houdini icons, 16–18 logical px;
- distinguish status by shape + text, not color alone;
- keep spacing dense: a 4 px base grid, an 8 px outer gutter, but both values must be
  scale-aware.

The last set of rules is a conclusion drawn from the factory QSS and the panel's own makeup,
not an official SideFX guideline.

## Audit of the current code

The current [`ui/theme.py`](../python/houdini_agent_panel/ui/theme.py) already gets the main
thing right: it uses `QApplication.palette()` and doesn't hardcode dark-theme hex values.

Worth improving as a separate task:

1. Scale `SPACING`, `MARGIN`, `RADIUS`, `ICON_SIZE`, and the other literal
   sizes through a host adapter. Right now Houdini's UI scale isn't taken into account.
2. Replace `color: gray` in `ui/agents.py` with a palette/resource-based semantic.
3. Don't set `font-size: 14px` in `ui/auth_view.py`; use the host font with
   bold/relative sizing instead.
4. Check the Houdini icon registry for add/settings/send/stop/attachment and tool kinds;
   use `hou.qt.Icon` wherever the semantics match. Keep text badges as a deliberate
   fallback.
5. Don't apply the full Houdini stylesheet to the entire panel root without a reason: the panel
   is already embedded in a styled tree. Only explicitly style detached popups/menus or local
   custom selectors.
6. Test H22 on at least Houdini Dark, one heavily customized theme, the
   experimental light theme, and UI scale 1.0/1.5/2.0, plus in a narrow and a wide dock.

## Conclusion

SideFX has no single found tutorial on "how to draw a good-looking Houdini UI," but
Houdini 22 has something more useful for an embedded plugin: a live theme system and a
public Qt integration layer. For this panel, the right strategy is not to build a
Houdini-like theme ourselves, but to let Houdini draw everything standard and build
only the chat-specific components on top of its colors, metrics, and icons.

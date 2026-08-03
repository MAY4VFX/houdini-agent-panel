# Houdini Python panels and packages — verified facts

Gathered on-site from an install of Houdini 20.5.445 and Houdini 22.0.368
(`/Applications/Houdini/`), user prefs at
`~/Library/Preferences/houdini/20.5` and `~/Library/Preferences/houdini/22.0`.
Nothing here is made up — every claim has a source (a file path).

---

## 1. The `.pypanel` format

The root tag is `<pythonPanelDocument>`, containing one or more
`<interface>` elements. A comment in Houdini's own files warns explicitly:
"It should not be hand-edited when it is being used by the application."
(but using it as a template to generate the file by hand is fine — Houdini
will just overwrite it the next time it's saved through the UI).

### Example 1 — the simplest one, with an `onNodePathChanged` callback

Source: `.../Houdini20.5.445/.../Resources/houdini/help/examples/python_panels/nodepath.pypanel`
(an identical file also exists in the install's `python_panels/` examples
folder; verbatim):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<pythonPanelDocument>
  <!-- This file contains definitions of Python interfaces and the
 interfaces menu.  It should not be hand-edited when it is being
 used by the application.  Note, that two definitions of the
 same interface or of the interfaces menu are not allowed
 in a single file. -->
  <interface name="NodePathExample" label="Node Path Example" icon="hicon:/SVGIcons.index?DATATYPES_node_path.svg" showNetworkNavigationBar="true" help_url="">
    <script><![CDATA[from hutil.Qt import QtWidgets

class NodePathExample(QtWidgets.QWidget):
    def __init__(self):
        super(NodePathExample, self).__init__()
        
        instruction_label = QtWidgets.QLabel(
            "Please navigate the Houdini node network using the network editor.")
            
        self.currentNodePathLabel = QtWidgets.QLabel()
        
        layout = QtWidgets.QVBoxLayout()
        layout.addWidget(instruction_label)
        layout.addSpacing(5)
        layout.addWidget(self.currentNodePathLabel)
        layout.addStretch(1)
        
        self.setLayout(layout)
        
    def updateCurrentNodePathLabel(self, node_path):
        self.currentNodePathLabel.setText("Current Node Path: %s" % node_path)

theExampleWidget = NodePathExample()

def onCreateInterface():
    global theExampleWidget
    return theExampleWidget

def onNodePathChanged(node):
    global theExampleWidget

    if node:
        node_path = node.path()
    else:
        node_path = "None"
    theExampleWidget.updateCurrentNodePathLabel(node_path)

 ]]></script>
    <includeInToolbarMenu menu_position="102" create_separator="false"/>
    <help><![CDATA[]]></help>
  </interface>
</pythonPanelDocument>
```

### Example 2 — a real SideFX panel with a full set of lifecycle callbacks

Source: `.../Houdini22.0.368/.../Resources/houdini/python_panels/NodeInfo.pypanel`
(verbatim):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<pythonPanelDocument>
  <!-- This file contains definitions of Python interfaces and the
 interfaces menu.  It should not be hand-edited when it is being
 used by the application.  Note, that two definitions of the
 same interface or of the interfaces menu are not allowed
 in a single file. -->
    <interface name="sidefx::node_info" label="Node Info"
               icon="BUTTONS_chooser_node" showNetworkNavigationBar="true"
               help_url="/network/info">
        <script><![CDATA[
from hutil.qt.info import window

thePanel = None

def onCreateInterface():
    global thePanel
    thePanel = window.NodeInfoPanel()
    return thePanel

def onActivateInterface():
    thePanel.panelActivated()

def onDeactivateInterface():
    thePanel.panelDeactivated()

def onNodePathChanged(node):
    thePanel.nodePathChanged(node)

]]>
        </script>
        <includeInPaneTabMenu menu_position="500" create_separator="false"/>
        <help></help>
    </interface>
</pythonPanelDocument>
```

Another real, minimal one (no node navigation, uses
`toolutils.safe_reload` to hot-reload the module whenever the interface is
recreated) — `.../Houdini22.0.368/.../python_panels/LogViewer.pypanel`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<pythonPanelDocument>
  <!-- ... -->
  <interface name="log_viewer" label="Log Viewer" icon="MISC_python" showNetworkNavigationBar="false" help_url="">
    <script><![CDATA[import logviewer.panel
import toolutils
toolutils.safe_reload(logviewer.panel)

def onCreateInterface():
    return logviewer.panel.LogViewerPanel()
]]></script>
    <includeInPaneTabMenu menu_position="500" create_separator="false"/>
    <help><![CDATA[]]></help>
  </interface>
</pythonPanelDocument>
```

### `<interface>` attributes (observed values)

- `name` — the interface's unique ID (a string, can contain `::` as a
  namespace on SideFX panels, e.g. `sidefx::node_info`).
- `label` — the visible name in the panels menu.
- `icon` — either a Houdini icon name (`MISC_python`, `BUTTONS_chooser_node`),
  or a `hicon:/` path to an SVG (`hicon:/SVGIcons.index?DATATYPES_node_path.svg`).
- `showNetworkNavigationBar` — `"true"`/`"false"`: whether to show the
  breadcrumb navigation bar for the network above the panel (doesn't
  directly affect `onNodePathChanged` calls, it's about a UI strip).
- `help_url` — either empty, or a path in Houdini's help (`/network/info`).

### Required and optional functions in `<script>`

- `onCreateInterface()` — **required**. Returns a `QWidget` (or a
  subclass), which Houdini embeds into the panel. Called every time the
  user creates a new tab for this panel.
- `onNodePathChanged(node)` — optional, called when the "current node"
  changes (whatever's highlighted in the network editor / selected as
  context), `node` is a `hou.Node` or `None`.
- `onActivateInterface()` / `onDeactivateInterface()` — optional, called
  when the panel's tab becomes active/inactive. This is a tab switch, NOT
  closing: resource cleanup must not be hung off of them.
- `onDestroyInterface()` — optional, and this is what a tab closing
  actually is. Found in 29 of the standard 22.0 panels
  (`PoseLibrary.pypanel` calls `cleanup()` from it, `LightLinker.pypanel`
  nulls out its object). Tally across the install: `onCreateInterface` — 35,
  `onDestroyInterface` — 29, `onActivateInterface`/`onDeactivateInterface`
  — 25 each, `onNodePathChanged` — 19.
- A global `kwargs` is available in the `<script>`'s scope, and it holds
  `paneTab` — this is how several open tabs of the same panel are told
  apart. Verbatim from `LightLinker.pypanel`:
  ```python
  def onActivateInterface():
      global theLightLinker
      theLightLinker.activate(kwargs.get('paneTab', None))
  ```

Menu tags — pick one:
- `<includeInToolbarMenu menu_position="N" create_separator="false"/>` —
  the panel shows up in the panel list's "Toolbar" menu.
- `<includeInPaneTabMenu menu_position="N" create_separator="false"/>` —
  shows up in the pane-tab creation menu (Tab menu → Python Panels and the
  pane's context menu).

### Registration — where `.pypanel` is looked for

Houdini scans the `python_panels/` directory inside every path listed in
`HOUDINI_PATH` (including paths added by packages via `"path"` in the
package JSON). No separate `Panels.txt`/menu file is required for python
panels — unlike shelf tools (`toolbar/*.shelf` + `MainMenuCommon`), each
`.pypanel` is self-contained and registers itself in the shared Python
Panels registry at Houdini startup. Confirmed by their locations: SideFX
puts its own panels in `$HFS/houdini/python_panels/*.pypanel`, while
third-party packages put theirs in
`<package_path>/python_panels/*.pypanel`, e.g.:
`.../Houdini22.0.368/.../Resources/packages/shotbuilder/python_panels/ShotLoad.pypanel`,
`.../packages/kinefx/python_panels/mixer.pypanel`,
`.../packages/apex/python_panels/apexselectionmanager.pypanel` — meaning
the directory is looked for relative to any `HOUDINI_PATH` entry, not just
`$HFS/houdini`.

---

## 2. The Houdini package JSON

Real examples from the user's
`~/Library/Preferences/houdini/{20.5,22.0}/packages/*.json` (verbatim, the
path values are real, from the user's own machine):

`~/Library/Preferences/houdini/22.0/packages/fxhoudinimcp.json`:
```json
{
    "env": [
        {
            "FXHOUDINIMCP": "~/.local/share/uv/tools/fxhoudinimcp/lib/python3.12/site-packages/fxhoudinimcp/houdini"
        }
    ],
    "path": "$FXHOUDINIMCP"
}
```

`~/Library/Preferences/houdini/22.0/packages/houdinimcp.json`:
```json
{
  "path": "$HOME/Library/Preferences/houdini/22.0/scripts/python/houdinimcp",
  "load_package_once": true,
  "version": "0.1",
  "env": [
    {
      "PYTHONPATH": {
        "value": "$HOME/Library/Preferences/houdini/22.0/scripts/python",
        "method": "append"
      }
    }
  ]
}
```

`~/Library/Preferences/houdini/20.5/packages/tentaculo.json`:
```json
{
  "load_package_once": true,
  "env": [
    {
      "CTENTACULO_LOCATION": "~/cerebro/ctentaculo"
    },
    {
      "HOUDINI_MENU_PATH": {
        "value": "&:~/cerebro/ctentaculo/tentaculo/api/ihoudini",
        "method": "append"
      }
    },
    {
      "PYTHONPATH": {
        "value": "~/cerebro/ctentaculo",
        "method": "append"
      }
    }
  ]
}
```

Observed keys and what they mean:
- `"path"` — added to `HOUDINI_PATH` (this is also where `python_panels/`
  would end up, if such a subdirectory exists inside it — see section 1).
- `"env"` — a list of `{VARIABLE_NAME: value}` objects. A value can be a
  plain string (overwrite/set), or an object
  `{"value": ..., "method": "append"|"prepend"|"set"}` controlling how the
  variable is combined with an already-existing value (important for
  `PYTHONPATH`/`HOUDINI_PATH`, so as not to clobber system values).
- `"load_package_once": true` — the package is processed only once even if
  the file is found across several package search paths (dedup protection).
- `"version"` — an arbitrary package version string, doesn't directly
  affect the loader's behavior, more for documentation/debugging.
- Variables inside `env` values are lazily expanded in the `$VAR` style
  (e.g. `$FXHOUDINIMCP` uses a variable defined earlier in the same file —
  order within the `env` list matters).
- `"enable"` and `"hpath"` weren't found in these real files, but per
  SideFX's documentation: `"enable": false` disables a package without
  deleting the file, `"hpath"` adds an entry to `HOUDINI_PATH` the same way
  the top-level `"path"` does, but specifically for the case of multiple
  paths. Since neither shows up in these files, I'm not confirming their
  exact form — only naming them.
- `"recommends"` is absent from the files checked — not confirmed on this
  machine.

Directories where Houdini looks for `packages/*.json` (confirmed simply by
files existing there): `$HOUDINI_USER_PREF_DIR/packages/` — meaning
`~/Library/Preferences/houdini/20.5/packages/` and
`~/Library/Preferences/houdini/22.0/packages/` separately per version.

---

## 3. `hutil.PySide`

The module genuinely exists in both versions, at
`.../Resources/houdini/python3.{11,13}libs/hutil/PySide/__init__.py`.
Full text (verbatim, identical in 20.5 and 22.0):

```python
"""
hutil.PySide is a thin wrapper package that imports from PySide6 if
running in a Houdini Qt 6 build and from PySide2 otherwise.
"""

import os
import importlib
import pkgutil
import sys

if os.environ.get("__HOUDINI_USE_QT6__", "0") == "1":
    import PySide6 as _internal_pyside

    # QAction and QActionGroup are commonly used so make it available here.
    from PySide6.QtGui import QAction, QActionGroup

    isQt6 = True
else:
    import PySide2 as _internal_pyside

    # QAction and QActionGroup are commonly used so make it available here.
    from PySide2.QtWidgets import QAction, QActionGroup

    isQt6 = False


# Register submodules.
# Call `pkgutil.walk_packages()` to discover PySide submodules.
for submodule_info in pkgutil.walk_packages(_internal_pyside.__path__):
    try:
        # Import the PySide submodule.
        submodule = importlib.import_module(
            f"{_internal_pyside.__name__}.{submodule_info.name}")

        # Register the submodule as an hutil.PySide submodule.
        sys.modules[f"{__name__}.{submodule_info.name}"] = submodule
    except ImportError:
        # The submodule could not be imported.
        # This can happen in the Qt5/PySide2 where not all Qt libraries
        # (i.e. QtPositioningQuick) are packaged in Houdini.
        pass


# Purposely set __all__ to an empty list to discourage people from
# doing `from hutil.PySide import *`.
__all__ = []
```

The mechanism: the module does **not** declare `QtWidgets`/`QtCore`/`QtGui`/`QtNetwork`
as explicit package attributes — it registers them dynamically into
`sys.modules["hutil.PySide.<submodule>"]` via `pkgutil.walk_packages`.
The practical consequence: the correct import is a **direct submodule
import**, like an ordinary subpackage:

```python
from hutil.PySide import QtWidgets, QtCore, QtGui, QtNetwork
```

This works because the names are already in `sys.modules` by the time the
import happens (registration happens when `hutil/PySide/__init__.py` runs,
i.e. on the first `import hutil.PySide` or `from hutil.PySide import ...`).
The project rule "Qt only through `hutil.PySide`" corresponds directly to
this fact — the module transparently substitutes PySide6 on Houdini's Qt6
builds (22.0) and PySide2 on its Qt5 builds (20.5), transparent to the rest
of the code.

There's also a separate `hutil/Qt.py` module (a third-party library,
[Qt.py by Marcus Ottosson](https://github.com/mottosso/Qt.py), version
`__version__ = "1.2.3"`, bundled with both Houdini versions) — an older,
general-purpose shim with a priority order of
`PySide6 → PySide2 → PyQt5 → PySide → PyQt4` and its own environment
variables (`HOUDINI_QT_VERBOSE`, `HOUDINI_QT_PREFERRED_BINDING`). This is
exactly what the `nodepath.pypanel` example uses (`from hutil.Qt import
QtWidgets`). This is not the same thing as `hutil.PySide` — both exist at
the same time, but the project rule names `hutil.PySide` specifically, so
that's what must be used, not `hutil.Qt`.

### Qt/PySide versions (verified by running Houdini's interpreter)

- **Houdini 20.5.445**: `PySide2.__version__ == "5.15.15"` (Qt 5.15.15),
  the package lives at
  `.../Houdini20.5.445/Frameworks/Python.framework/Versions/3.11/lib/python3.11/site-packages-forced/PySide2`.
  Next to it: `shiboken2`, `shiboken2_generator`.
- **Houdini 22.0.368**: `PySide6` version `6.8.3`
  (`.../Houdini22.0.368/.../site-packages-forced/PySide6-6.8.3-py3.13.egg-info`),
  the package at
  `.../Houdini22.0.368/Frameworks/Python.framework/Versions/3.13/lib/python3.13/site-packages-forced/PySide6`.

### Availability of `QtWebEngineWidgets`, `QtWebSockets`, `QtMultimedia`

Verified by actually importing them in both versions (not just the
presence of a `.pyi` stub — the runtime module imports without errors):

```
PySide2 (20.5, Qt 5.15.15): QtWebEngineWidgets OK, QtWebSockets OK, QtMultimedia OK, QtNetwork OK
PySide6 (22.0, Qt 6.8.3):   QtWebEngineWidgets OK, QtWebSockets OK, QtMultimedia OK, QtNetwork OK
```

All four modules are available in both versions — a network layer
(ACP over WebSocket/stdio) and web content can be built without worrying
about their absence.

Additionally: 22.0 (PySide6) has no `QtWebEngine.pyi` module (it existed in
Qt5/PySide2 as a unifying module), instead there are the separate
`QtWebEngineCore`, `QtWebEngineWidgets`, `QtWebEngineQuick`. In 20.5
(PySide2) there's a separate `QtWebEngine.pyi` in addition to those same
three.

---

## 4. Python version and autoload directories

Verified by directly running Houdini's Python binary:

```
Houdini 20.5.445 → Python 3.11.7 (main, May  9 2024, ...) [Clang 15.0.0]
Houdini 22.0.368 → Python 3.13.10 (main, Mar  4 2026, ...) [Clang 15.0.0]
```

The corresponding user-script autoload directories (confirmed by the mere
existence and content there, e.g. `hutil` lives exactly there):
- Houdini 20.5 → `python3.11libs` (e.g.
  `.../Houdini20.5.445/.../Resources/houdini/python3.11libs/hutil/...`)
- Houdini 22.0 → `python3.13libs` (e.g.
  `.../Houdini22.0.368/.../Resources/houdini/python3.13libs/hutil/...`)

Any `python3.11libs`/`python3.13libs` directory found in any of the
`HOUDINI_PATH` entries (including package paths) is automatically added to
`sys.path` — that's how `hutil`, `toolutils`, `hdefereval`, and the rest
become importable without manually adding them to `PYTHONPATH`.

When these run (per SideFX's documentation; not stored as an example in the
install, since these are user scripts created by the artist themselves —
confirmed by the absence of `123.py`/`pythonrc.py` files by default in
`$HFS/houdini/scripts/`, where only `scripts/` and `config/Scripts`
directories were found, without these specific files):
- `$HOUDINI_USER_PREF_DIR/scripts/pythonrc.py` — runs once when Houdini
  starts, before the first scene/UI is opened.
- `$HOUDINI_USER_PREF_DIR/scripts/123.py` — runs every time a new scene is
  created and when a `.hip` file loads (the Python equivalent of
  `456.cmd`/`123.cmd` for hscript).
- `uiready.py` — **confirmed physically**, the file exists:
  `.../Houdini22.0.368/.../Resources/houdini/python3.13libs/uiready.py`.
  Per SideFX's documentation it runs after Houdini's UI is fully ready
  (a point where it's safe to create Qt widgets, open panels, etc.) — a
  later point than `123.py`, which can run before the UI is ready in
  batch/hbatch mode.

---

## 5. Dark theme / Qt styling

Confirmed by `hou.py`'s source code (a SWIG wrapper, real docstrings):

```python
def colorFromName(self, name: "char const *") -> "HOM_Color":
    r"""
    colorFromName(self, name) -> hou.Color
    ...
      > >>> hou.ui.colorFromName("DisplayOnColor")
    """
    return _hou.ui_colorFromName(self, name)
```

So `hou.ui.colorFromName("ColorName")` is a working, documented way to
fetch a specific Houdini theme color (names like `GraphDisplayHighlight`,
`GraphRenderHighlight`, `DisplayOnColor` come from docstrings of
neighboring functions in the same file, around lines 80410-80426).

No separate public method for getting Houdini's qss stylesheet
(something like `hou.ui.qtStyleSheet()`) was found while searching this
file — its existence isn't confirmed. The standard practice in SideFX's
panels is not to override the global QSS, but to selectively paint via the
application's palette (`QtWidgets.QApplication.palette()`, already
configured by Houdini) and via `hou.ui.colorFromName` for accent colors,
leaving base widgets (buttons, fields) on Houdini's native style — that
way a panel looks native "for free," with no QSS of its own.

---

## 6. `$HIP`

Confirmed by `hou.py`'s docstrings:

```python
def path(self) -> "std::string":   # hou.hipFile.path()
    r"""
    path() -> str
        Return the absolute file path of the current scene file. Remember
        that a file may not exist at this path if the current scene hasn't
        been saved yet.
    """
```

```python
def expandString(self, str: "char const *", expand_tilde: "bool"=True) -> "std::string":
    r"""
    expandString(str, expand_tilde=True) -> str
      > >>> hou.text.expandString("$HIP/file.geo")
    """
    return _hou.text_expandString(self, str, expand_tilde)
```

So there are two working ways:
- `hou.hipFile.path()` — the absolute path to the current `.hip` file.
- `hou.text.expandString("$HIP/...")` — expands Houdini's `$HIP` variable
  inside an arbitrary string (works for paths with placeholders).

`hou.hipFile.isNewFile()` (a method present in `hou.py`, class `hipFile`,
around line 65590 in the 22.0 file) is a way to find out that a scene
hasn't been saved yet (a new, unsaved scene); for it, `path()` presumably
returns a default like `untitled.hip` in the current working directory —
this behavior wasn't verified with a live Houdini run within this task's
scope (the `hou` interpreter couldn't be run standalone outside the
application itself, due to a missing part of the runtime environment/dylib,
see section 8), so the exact value for a new scene isn't confirmed directly
— only that `hou.hipFile.isNewFile()` can be checked for this separately,
before trusting `path()`.

---

## 7. The main thread and safely calling `hou` from a background thread

Confirmed by the source of the `hdefereval.py` module, which genuinely
exists in both versions:
`.../Houdini22.0.368/.../Resources/houdini/python3.13libs/hdefereval.py`.

The module's docstring (verbatim):
```python
"""This module provides functions to perform deferred evaluation of Python
code.  You can call these functions from any thread, and they are executed
in the main thread when Houdini's event loop is idle.
"""
```

Signatures (verbatim):
```python
def executeDeferred(code, *args, **kwargs):
    """Run the specified Python code in the main thread and do not wait
    for it to finish running.

    code: Either a string containing a Python expression, a callable object,
        or a code object.
    args, kwargs: Only valid for callable objects.
    """

def executeDeferredAfterWaiting(code, num_waits, *args, **kwargs):
    """This function is like executeDeferred, except it waits for the event
    loop callback to be triggered the given number of times before running
    the callback.

    Use this function when starting Houdini with a script that needs to run
    after Houdini's UI has fully initialized.
    """

def executeInMainThreadWithResult(code, *args, **kwargs):
    return _queueDeferred(code, args, kwargs, block=True)
```

The practical rule (matches the project rule "we never touch `hou` from the
worker thread"):
- From the panel's background Python thread: only call `hou` code via
  `hdefereval.executeDeferred(callable, *args, **kwargs)` (fire-and-forget,
  runs on the main thread on the next event loop pass) or
  `hdefereval.executeInMainThreadWithResult(callable, *args, **kwargs)`
  (blocks the calling thread until it runs on the main thread and returns
  the result).
- `hou.ui.postEventCallback(callback)` is also documented in `hou.py`
  (around line ~105389, verbatim):
  ```python
  def postEventCallback(self, callback: "InterpreterObject") -> "void":
      r"""
      postEventCallback(callback)

          Register a Python callback to be called next in Houdini's event
          loop. This will be called only once.

          callback
              Any callable Python object that expects no parameters. It could
              be a Python function, a bound method, or any object implementing
              __call__.
      """
  ```
  Unlike `hdefereval`, `hou.ui.postEventCallback`'s callback takes no
  arguments and returns no result to the caller — so it's a lower-level
  primitive, while `hdefereval` is a convenient wrapper over a similar
  mechanism, specifically aimed at cross-thread calls.
- The check for "can I touch `hou` right now" is
  `hou.isUIAvailable()` (docstring from `hou.py`, verbatim):
  ```python
  def isUIAvailable() -> "bool":
      r"""
      hou.isUIAvailable
      Return whether or not the hou.ui module is available.
      USAGE
        isUIAvailable() -> bool
      The hou.ui module is not available in the command-line interpreter or in
      MPlay, and this function helps you to write scripts that will run in
      Houdini and command-line and/or MPlay.
      """
  ```
  Important: this is about `hou.ui`'s availability (batch/hbatch vs. full
  UI), not about "am I on the main thread right now." No separate public
  API for "am I the main thread" was found in `hou.py` — the standard
  practice (also confirmed by the mere existence of `hdefereval`) is to
  simply never assume you're on the main thread inside the panel's
  worker/QThread, and always go through `hdefereval` rather than trying to
  dynamically detect the thread.

---

## 8. Limitations of this research

- `hou` couldn't be imported standalone with Houdini 22.0's Python outside
  the application itself — it fails at `dlopen` (`Symbol not found: _iconv`
  in `libfbxsdk.dylib` due to an incomplete `DYLD_LIBRARY_PATH`/environment).
  Every fact about `hou.*` in this document is taken from `hou.py`'s source
  text (a SWIG wrapper with full docstrings) and from the docstrings of
  `hipFile`/`ui`/`text`/`isUIAvailable`, rather than from a live call
  inside Houdini — meaning the signatures and docstrings are confirmed
  verbatim, but the actual runtime behavior (the type of value returned for
  a new, unsaved scene, etc.) wasn't re-verified with a live run within
  this task's scope.
- No method of the form `hou.ui.qtStyleSheet()` was found in `hou.py` — if
  it exists, it didn't turn up while grepping this file; neither its
  presence nor its absence is asserted beyond that.

---

## 9. asyncio inside Houdini — `haio` (found during integration, breaks a naive QThread)

Houdini swaps in its own asyncio policy: `asyncio.get_event_loop_policy()`
returns `haio.HoudiniEventLoopPolicy`, and `asyncio.new_event_loop()`
returns `haio.HoudiniEventLoop`. Verified by running it in both versions
(`python3.11libs/haio.py`, `python3.13libs/haio.py`).

Two consequences, each of which breaks a client written "the normal way":

**1. Houdini's loop only works on the main thread.**
```
RuntimeError: Current thread is not the main thread
  haio.py(116): check_thread
  haio.py(2116): run_forever
```
That is, `asyncio.new_event_loop()` + `run_forever()` on a worker QThread
fails. Fixed by taking the loop class directly, bypassing the policy:
```python
loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.SelectorEventLoop()
```

**2. A subprocess via `asyncio.create_subprocess_exec` doesn't come up.**
The stock `_UnixSelectorEventLoop._make_subprocess_transport` goes through
the policy to get a child watcher, and Houdini's policy doesn't provide one:
```
  asyncio/unix_events.py(202): watcher = events.get_child_watcher()
  asyncio/events.py(842): return get_event_loop_policy().get_child_watcher()
  haio.py(3084): raise NotImplementedError
```
That means `acp.spawn_agent_process()` doesn't work inside Houdini — it's
built specifically on `create_subprocess_exec`.

A working workaround, verified by running it in Houdini 22.0.368 and
20.5.445: spawn the process with plain `subprocess.Popen`, and hook its
pipes into the loop through the public API that doesn't need a watcher:
```python
proc = subprocess.Popen(argv, stdin=PIPE, stdout=PIPE, stderr=PIPE, env=env, cwd=cwd)

reader = asyncio.StreamReader(limit=50 * 1024 * 1024, loop=loop)
await loop.connect_read_pipe(
    lambda: asyncio.StreamReaderProtocol(reader, loop=loop), proc.stdout)
transport, protocol = await loop.connect_write_pipe(
    lambda: asyncio.streams.FlowControlMixin(loop=loop), proc.stdin)
writer = asyncio.StreamWriter(transport, protocol, reader, loop)

conn = acp.connect_to_agent(client, writer, reader)   # the byte-stream form
```
`connect_to_agent` accepts a StreamWriter/StreamReader pair — this is a
documented calling form (see [`acp-sdk.md`](acp-sdk.md) §1), so the
workaround stays within the SDK's public API.

Why unit tests didn't catch this: outside Houdini the policy is the stock
one, and both calls work. A test that catches the regression has to install
a policy that mimics `haio` — one that fails on `run_forever()` off the
main thread and raises `NotImplementedError` from `get_child_watcher()`.

---

## 10. A top-level Qt widget inside Houdini never gets a native window freed on its own

Measured, not read: 20 unparented `QWidget`s created and left alone —
0 new native (OS-level) windows. Calling `winId()` on each (the same thing
`show()` triggers internally) — +20 native windows. Deleting the widgets
correctly afterwards (`setParent(None)` then `deleteLater()`, event loop
pumped) — 0 of those 20 native windows freed. Checked externally, from
outside the process, with `CGWindowListCopyWindowInfo` filtered by this
Houdini process's pid — not by asking Qt about its own widget count, which
would not have caught this.

Consequence: a popup that gets recreated on every use (a fresh `QWidget`
each time instead of the same instance shown/hidden/reused) leaks one
native window per use, for the life of the Houdini process — there is no
point later at which it gets reclaimed. The fix is structural, not a
cleanup call: build the popup once, keep it, and only ever call
`show()`/`hide()` on the same instance. Measured effect of applying this to
the panel's own popups: opening the panel went from +28 native windows to
+6.

---

## 11. `hou.qt` does not exist inside `hython`

Confirmed in both Houdini 20.5.445 and 22.0.368: `import hou; hou.qt` raises
`AttributeError` in `hython`. Not "empty" or "a stub" — the attribute is
absent.

Consequence: any code path that reads a color via `hou.qt.getColor(...)` (or
anything else under `hou.qt`) cannot be exercised or verified headless —
`hython` has no such thing to call. It can only be checked live, inside
Houdini's actual GUI process. This is the concrete reason `theme.py` treats
`QApplication.palette()` as the primary source for the panel's colors and
`hou.qt`-based lookups (where they exist at all, GUI-only) as at most a
narrow, secondary fallback — the primary source has to be something
`hython` can actually reach, since that's what the test suite and any
headless verification run on.

---

## 12. Houdini 22's colorscheme presets — `Themes/default.theme.json`

Lives at `$HFS/houdini/config/Themes/default.theme.json` in a Houdini 22
install (`$HFS` is Houdini's own install-root environment variable). 52
presets in that file, each one three HSV triples: `base`, `primary`,
`highlight`. Example: the "Plumtree" preset's `highlight` is `[356, 30, 50]`
in HSV, which converts to `#7f595b`.

In Houdini 20.5 and 21, this file does not exist at all — there is no
`Themes/` directory to check against.

**Not established**: whether `hou.qt.getColor()` (or any other live
in-app color lookup) actually follows a preset selected this way — i.e.,
whether picking "Plumtree" in Houdini 22's theme editor changes what
`hou.qt.getColor()` returns at runtime. This file only proves the preset
data exists and what it contains; it says nothing about which live API
reads it. Checking that requires a live GUI session (see fact 11 — it
cannot be checked in `hython` at all), and it hasn't been done within this
project's scope.

---

## 13. `QCoreApplication.processEvents()` does not run Qt's deferred deletes

`widget.deleteLater()` schedules a `QEvent.DeferredDelete`, and
`processEvents()` alone does not drain that queue — a widget-lifetime
measurement that calls only `processEvents()` between "delete" and
"count what's left" will report a leak in a place where the object is
actually gone, just not yet swept. The queue needs
`QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)` (or an
equivalent explicit flush) run as well.

Cost of not knowing this, paid in full during this project: about an hour
spent chasing a "leak" that was this artifact, not a real one, before
sendPostedEvents was added and the reading came back clean.

---

## 14. A `QThread` still running at process exit is fatal, not a warning

From a real crash report, not a lab reproduction: Qt does not print a
warning and move on if a `QThread` is destroyed while its `run()` is still
executing — it calls `qFatal()`, which raises `SIGABRT` and takes the whole
process down. In this project this showed up as Houdini itself crashing on
close, not as a log line.

Consequence: any background `QThread` a panel starts must be positively
stopped before the process can go down — relying on a single shutdown path
(e.g. only the widget's own `shutdown()`/`onDestroyInterface()`) is not
enough, because Houdini does not guarantee that path runs on every kind of
exit. The thread needs to be stopped from more than one place: on the
panel's own teardown AND on `QCoreApplication.aboutToQuit`, AND as a last
resort in an `atexit` handler — whichever of those actually fires first for
a given exit path is what saves the process.

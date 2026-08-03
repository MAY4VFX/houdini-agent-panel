# Architecture — module contract

Required reading before touching code. This records the **exact** public
API of every module: signatures, dataclasses, Qt signals. Modules are
written by different people in parallel and only fit together through this
document — a signature can change, but then this doc and every caller get
updated too.

Verified facts about external APIs live in [`facts/`](facts/): [ACP SDK](facts/acp-sdk.md),
[fxhoudinimcp](facts/fxhoudinimcp.md), [Houdini](facts/houdini.md). Product
decisions live in [`design.md`](design.md).

---

## 0. How this even runs

Three different Pythons take part in the panel's life, and mixing them up
is the main source of bugs:

| Who | What it is | What lives in it |
|---|---|---|
| **installer python** | the one you ran `pip install houdini-agent-panel` with | the installer CLI, `fxhoudinimcp` |
| **Houdini python** | `$HFS/bin/hython`, 3.11 on H20.5, 3.13 on H22 | the panel and its dependencies — in a separate `--target` tree |
| **agent process** | Node/the agent's binary | nothing of ours |

`pydantic` carries a compiled `pydantic_core`, so putting the installer
Python's site-packages onto Houdini's `PYTHONPATH` isn't an option: 3.11
and 3.13 have different ABI tags, and `import pydantic` would fail. So the
installer installs the panel **into Houdini itself**:

```
python -m houdini_agent_panel install
  ├─ finds the packages folder of every Houdini on the machine
  ├─ for each one, finds its hython and its version (3.11 / 3.13)
  ├─ hython -m pip install --target <data>/deps/py3.11 houdini-agent-panel==<version>
  └─ writes <prefs>/packages/houdini_agent_panel.json
```

The package json (the one place where paths get glued together):

```json
{
    "env": [
        { "HAP_DEPS": "/Users/x/Library/Application Support/HoudiniAgentPanel/deps/py3.11" },
        { "HAP_PYTHON": "/opt/homebrew/bin/python3.12" },
        { "PYTHONPATH": { "value": "$HAP_DEPS", "method": "prepend" } }
    ],
    "path": "$HAP_DEPS/houdini_agent_panel/houdini"
}
```

- `PYTHONPATH` is **prepended**, not appended: the panel's tree must win
  over anything the user has already piled onto the environment.
- `HAP_PYTHON` is the installer python. The panel needs it for exactly one
  thing: building `mcpServers[0].command`, because `fxhoudinimcp` as an
  MCP server lives there specifically (see §4).
- `path` gives Houdini the plugin tree: `python3.11libs/`, `python3.13libs/`,
  `python_panels/`.

Requires network access to install. Offline — `--find-links DIR`, and pip
takes its wheels from there.

---

## 1. `paths.py` — where things live

We don't take on our own dependency on `platformdirs`: one function for
three OSes is cheaper than an extra wheel in the `--target` tree.

```python
APP_NAME = "HoudiniAgentPanel"

def data_dir() -> Path
    """The user data root. Created on first access.

    macOS   ~/Library/Application Support/HoudiniAgentPanel
    Windows %LOCALAPPDATA%/HoudiniAgentPanel
    Linux   $XDG_DATA_HOME/houdini-agent-panel (or ~/.local/share/...)

    Overridden by the HAP_DATA_DIR variable — this is also the entry point for tests.
    """

def deps_dir(python_tag: str | None = None) -> Path   # <data>/deps/py3.11
def agents_dir() -> Path                              # <data>/agents
def agent_dir(agent_id: str) -> Path                  # <data>/agents/<id>
def node_dir() -> Path                                # <data>/node
def cache_dir() -> Path                               # <data>/cache
def logs_dir() -> Path                                # <data>/logs
def settings_path() -> Path                           # <data>/settings.json
def python_tag(version_info=None) -> str              # "py3.11"
def open_in_file_manager(path: Path) -> None          # the "Open" button
```

---

## 2. `settings.py` — panel settings

One JSON file, read whole, written atomically (`.tmp` + `os.replace`). No
partial merges: the file is small, and an atomic swap saves you from a
truncated file if Houdini crashes.

```python
@dataclass
class CustomAgent:
    id: str
    name: str
    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)

@dataclass
class InstalledAgent:
    agent_id: str
    version: str
    kind: str            # "npx" | "binary" | "custom"
    installed_at: str    # ISO 8601 UTC

@dataclass
class Settings:
    version: int = 1
    default_agent: str | None = None
    autostart_agent: bool = True
    check_updates: bool = True
    show_announcements: bool = True
    telemetry: bool = False
    telemetry_consent_asked: bool = False
    whisper_endpoint: str = ""
    custom_agents: list[CustomAgent] = ...
    installed_agents: dict[str, InstalledAgent] = ...
    seen_announcements: list[str] = ...

    def to_dict(self) -> dict
    @classmethod
    def from_dict(cls, payload: dict) -> "Settings"   # ignores unknown keys,
                                                      # takes missing ones from the defaults

def load(path: Path | None = None) -> Settings
def save(settings: Settings, path: Path | None = None) -> None
def diagnostics(settings: Settings) -> str
    """Text for the "Copy diagnostics" button: panel/fx/Qt/Python versions,
    OS, Houdini version, installed agents' ids and versions, fx port, Qt source.
    No scene paths, no secret settings content."""
```

A corrupted JSON file isn't an error: `load` returns the defaults and
renames the file to `settings.json.broken`, so a single stray comma
doesn't leave someone without a panel.

---

## 3. `registry.py` — the ACP registry

```python
REGISTRY_URL = "https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json"

#: The six from design.md. The order is the order they're shown in the UI.
FEATURED_AGENT_IDS: tuple[str, ...] = (
    "claude-code-acp", "codex-acp", "gemini-cli", "grok-build", "kimi-cli", "opencode",
)

@dataclass(frozen=True)
class NpxDistribution:
    package: str              # "@zed-industries/claude-code-acp@1.2.3"
    args: list[str]

@dataclass(frozen=True)
class BinaryDistribution:
    archive: str
    cmd: str                  # "./opencode" — relative to the extracted archive's root
    args: list[str]
    sha256: str

@dataclass(frozen=True)
class AgentEntry:
    id: str
    name: str
    version: str
    description: str = ""
    repository: str = ""
    website: str = ""
    license: str = ""
    icon: str = ""
    authors: tuple[str, ...] = ()
    npx: NpxDistribution | None = None
    binaries: Mapping[str, BinaryDistribution] = ...   # key is platform_key()

    @property
    def needs_node(self) -> bool
    def distribution_for(self, key: str | None = None) -> NpxDistribution | BinaryDistribution | None
        """None — the agent can't be installed on this platform (e.g. Kimi on
        darwin-x86_64). The UI must show this as a reason, not silently hide it."""

def platform_key() -> str
    """darwin-aarch64 | darwin-x86_64 | linux-aarch64 | linux-x86_64 | windows-x86_64"""

def parse_registry(payload: Mapping) -> list[AgentEntry]
def fetch_registry(*, force: bool = False, max_age: float = 86400.0,
                   fetch: Fetcher | None = None) -> list[AgentEntry]
    """Cached at <cache>/registry.json. Network unavailable — returns the cache at
    any age; no cache — RegistryError."""

class RegistryError(RuntimeError): ...
```

`Fetcher` is the network-access protocol shared across the whole project,
so tests never touch the network:

```python
class Fetcher(Protocol):
    def __call__(self, url: str, *, timeout: float = 30.0) -> bytes: ...

def urlopen_fetch(url: str, *, timeout: float = 30.0) -> bytes   # urllib-based implementation
```

---

## 4. `scene.py` — binding to its own Houdini scene

The panel lives **inside** the Houdini process, so it knows its own fx
server's port exactly, without guessing:
`fxhoudinimcp_server.startup.get_port()` — the same in-process variable the
server itself uses. The HTTP scan over 8100..8115 remains only a fallback
(the fx plugin isn't loaded / is an old version).

```python
FX_SERVER_NAME = "fxhoudini"

def fx_port() -> int | None
    """The fx server's port in THIS Houdini process. None — the server isn't up."""

def fx_host() -> str                       # "127.0.0.1"

def fx_python() -> str
    """The interpreter fxhoudinimcp is installed in: $HAP_PYTHON, otherwise
    sys.executable. Inside Houdini, sys.executable is Houdini's own binary, so
    without HAP_PYTHON the MCP server can't come up; this state should be
    shown to the human, not crash."""

def mcp_servers() -> list[dict]
    """Exactly what goes into session/new as mcpServers.

    [{"name": "fxhoudini",
      "command": "/opt/homebrew/bin/python3.12",
      "args": ["-m", "fxhoudinimcp"],
      "env": [{"name": "HOUDINI_HOST", "value": "127.0.0.1"},
              {"name": "HOUDINI_PORT", "value": "8101"}]}]

    Pinning the port here is mandatory: without it the MCP server scans the
    range and might connect to SOMEONE ELSE's open Houdini. env's shape is a list of
    {name, value} (McpServerStdio.env: list[EnvVariable]), not a dict.
    """

def hip_dir() -> str
    """$HIP. From the main thread ONLY. An unsaved scene resolves to $HOME, not a
    nonexistent untitled path: the cwd in session/new must exist."""

def houdini_version() -> str
def is_fx_available() -> bool
```

`hou` is imported lazily inside functions: the module must be importable in
tests outside Houdini.

---

## 5. `runtime.py` + `node.py` — installing agents

```python
# node.py
MIN_NODE = (20, 0, 0)
NODE_VERSION = "22.14.0"      # what we download if there's no system Node

def find_system_node(minimum: tuple[int, int, int] = MIN_NODE) -> Path | None
def node_platform() -> tuple[str, str]                # ("darwin", "arm64")
def dist_url(version: str = NODE_VERSION) -> str
def shasums_url(version: str = NODE_VERSION) -> str
def install_node(*, version: str = NODE_VERSION, progress: Progress | None = None,
                 fetch: Fetcher | None = None) -> Path
    """Downloads the archive from nodejs.org, verifies it against SHASUMS256.txt,
    extracts it into <data>/node/<version>. Returns the path to the node binary.
    Never touches the system."""
def ensure_node(*, progress: Progress | None = None) -> Path
def npx_argv(node_bin: Path, package: str, args: Sequence[str]) -> list[str]
    """[<node>, <npx-cli.js>, "--yes", package, *args] — calls npx-cli.js
    directly with that same node, rather than the shell shim: the shim looks
    for node on PATH, which we don't have."""

# runtime.py
class Progress(Protocol):
    def __call__(self, done: int, total: int | None, note: str) -> None: ...

@dataclass(frozen=True)
class LaunchSpec:
    command: str
    args: list[str]
    env: dict[str, str]      # added to the environment, not a replacement for it

class InstallError(RuntimeError): ...
class ChecksumError(InstallError): ...

def is_installed(entry: AgentEntry) -> bool
def installed_version(agent_id: str) -> str | None
def install_agent(entry: AgentEntry, *, progress: Progress | None = None,
                  fetch: Fetcher | None = None) -> LaunchSpec
    """A binary one downloads, verifies sha256, extracts into <data>/agents/<id>/<version>,
    sets +x. An npx one calls ensure_node() and writes the manifest; the package
    itself is pulled in by npx on first launch. A hash mismatch — ChecksumError and
    NOTHING is left on disk."""
def uninstall_agent(agent_id: str) -> None
def launch_spec(entry: AgentEntry) -> LaunchSpec
def custom_launch_spec(agent: CustomAgent) -> LaunchSpec
def download_and_verify(url, sha256, dest, *, progress=None, fetch=None) -> Path
def extract_archive(archive: Path, dest: Path) -> None
    """tar.gz/tgz/zip. Paths with .. or absolute ones are rejected (Zip Slip)."""
```

---

## 6. `client.py` — ACP on top of a QThread

The riskiest part of the project. Rules:

- the asyncio loop lives on its own `QThread`, `hou` is **never** touched from it;
- outward — only Qt signals (Qt's queue makes them thread-safe);
- inward — only `asyncio.run_coroutine_threadsafe`;
- no `qasync`.

```python
class AcpWorker(QtCore.QObject):
    """Lives on the worker thread. Owns the loop, the agent process, the connection."""

class AcpClient(QtCore.QObject):
    """Facade on the MAIN thread. The only thing the UI sees."""

    # --- connection lifecycle
    connected = Signal(object)            # AgentInfo
    disconnected = Signal(str)            # reason, "" on a normal stop
    failed = Signal(str)                  # human-readable text
    auth_required = Signal(list)          # list[AuthMethod]
    log_line = Signal(str)                # the agent's stderr, for diagnostics

    # --- sessions
    session_started = Signal(str, object) # session_id, SessionState
    modes_changed = Signal(str, object)   # session_id, SessionModeState
    commands_changed = Signal(str, list)  # session_id, list[AvailableCommand]

    # --- feed
    message_chunk = Signal(str, str, str) # session_id, message_id, text
    thought_chunk = Signal(str, str, str)
    tool_call = Signal(str, object)       # session_id, ToolCall
    tool_call_update = Signal(str, object)
    plan_changed = Signal(str, list)      # session_id, list[PlanEntry]
    usage_changed = Signal(str, object)   # session_id, Usage
    turn_finished = Signal(str, str)      # session_id, stop_reason
    error = Signal(str, str)              # session_id (may be ""), text

    # --- permissions: a request going out, an answer coming back
    permission_requested = Signal(str, str, object, list)
        # request_key, session_id, ToolCallUpdate, list[PermissionOption]

    def __init__(self, parent=None) -> None
    def start(self, spec: LaunchSpec, *, cwd: str) -> None
    def stop(self) -> None
    def is_running(self) -> bool
    def agent_info(self) -> AgentInfo | None

    def authenticate(self, method_id: str) -> None
    def new_session(self, *, cwd: str, mcp_servers: list[dict]) -> None
    def prompt(self, session_id: str, blocks: list[dict]) -> None
    def cancel(self, session_id: str) -> None
    def set_mode(self, session_id: str, mode_id: str) -> None
    def answer_permission(self, request_key: str, option_id: str | None) -> None
        """option_id=None — "cancelled", results in a DeniedOutcome."""
```

`AgentInfo` is a flat snapshot of `initialize`, so the UI doesn't have to
pull in pydantic models:

```python
@dataclass(frozen=True)
class AgentInfo:
    name: str
    version: str
    protocol_version: int
    supports_image: bool
    supports_audio: bool
    supports_embedded_context: bool
    supports_load_session: bool
    supports_logout: bool
    auth_methods: tuple[AuthMethod, ...]

@dataclass(frozen=True)
class AuthMethod:
    id: str
    name: str
    description: str = ""
```

**The UI rule lives here**: `supports_*` is the single source of truth for
whether to draw the attachment button and microphone. The panel never
decides anything on its own.

The implementation relies on `acp.spawn_agent_process` (see
[facts/acp-sdk.md §1](facts/acp-sdk.md)). Two pitfalls from there, both
mandatory to handle:

1. `default_environment()` hands the agent an almost-empty environment
   (`HOME`, `PATH`, `SHELL`, `TERM`, `USER`). Everything else via an
   explicit `env=`.
2. The default stdio buffer limit is 64 KB. A base64 image will overflow
   it, and the connection will hang. We pass
   `transport_kwargs={"limit": 50 * 1024 * 1024}`.

The agent's `stderr` is read by a separate task and forwarded to
`log_line` — otherwise a full pipe hangs the process.

---

## 7. `sessions.py` — a session pool over one connection

```python
@dataclass
class SessionState:
    session_id: str
    title: str                      # the first line of the first prompt, otherwise "New conversation"
    cwd: str
    created_at: float
    current_mode_id: str | None = None
    available_modes: list[SessionMode] = ...
    available_commands: list[AvailableCommand] = ...
    entries: list[Entry] = ...      # the feed, see §8
    usage: Usage | None = None
    busy: bool = False
    unread: bool = False    # something arrived while this wasn't the visible session

class SessionPool(QtCore.QObject):
    added = Signal(str)
    removed = Signal(str)
    changed = Signal(str)

    def add(self, state: SessionState) -> None
    def get(self, session_id: str) -> SessionState | None
    def all(self) -> list[SessionState]
    def remove(self, session_id: str) -> None
```

One `SessionPool` per Houdini process (the module-level singleton
`pool()`), because a second panel tab must see the same session list and
the same live agent process. Two tabs — one `AcpClient`, one process,
different `current`: the pool has no notion of "current" at all — that's a
fact about a tab, not about the shared list, and lives on `AgentPanel`
itself (`_current_session_id`/`_set_current_session`/`_current_session()`).
It used to live here as one shared field, which meant picking a different
conversation in one tab silently moved every other open tab too (issue
#21) — exactly the opposite of "different current" above.

---

## 8. `transcript_model.py` — the feed model (no Qt widgets)

Kept separate from rendering, so the logic that assembles the feed can be
tested without a QApplication.

```python
EntryKind = Literal["user", "agent", "thought", "tool", "plan", "permission", "error"]

@dataclass
class Entry:
    kind: EntryKind
    id: str              # message_id / tool_call_id / uuid
    text: str = ""
    tool: ToolCallView | None = None
    plan: list[PlanEntry] = ...
    permission: PermissionView | None = None

@dataclass
class ToolCallView:
    tool_call_id: str
    title: str
    kind: str            # ToolKind, "other" if the agent didn't send one
    status: str          # pending | in_progress | completed | failed
    content: list[dict] = ...
    locations: list[dict] = ...

@dataclass
class PermissionView:
    request_key: str
    tool_title: str
    options: list[tuple[str, str, str]]   # (option_id, name, kind)
    answered: str | None = None

class TranscriptModel:
    """Folds the session/update stream into a list of Entry.

    Chunks sharing a message_id are stitched into a single entry —
    otherwise the feed turns into a hundred one-letter paragraphs.
    tool_call_update finds the entry by tool_call_id and patches only the
    fields that arrived (None = "unchanged"). plan replaces the previous
    plan wholesale (the protocol sends the full list).
    """
    def append_user(self, text: str) -> Entry
    def apply_chunk(self, message_id: str, text: str, *, thought: bool = False) -> Entry
    def apply_tool_call(self, call) -> Entry
    def apply_tool_update(self, update) -> Entry | None
    def apply_plan(self, entries) -> Entry
    def apply_permission(self, view: PermissionView) -> Entry
    def resolve_permission(self, request_key: str, option_id: str | None) -> Entry | None
    def append_error(self, text: str) -> Entry
    def entries(self) -> list[Entry]
```

---

## 9. `updates.py`, `announcements.py`, `telemetry.py`

```python
# updates.py
PYPI_URL = "https://pypi.org/pypi/{name}/json"

@dataclass(frozen=True)
class Update:
    kind: str        # "agent" | "panel" | "fx"
    target: str      # agent_id or the package name
    label: str       # what to show the human
    current: str
    latest: str

def is_newer(latest: str, current: str) -> bool
    """PEP 440-based comparison, falling back to segment-by-segment numeric
    comparison. Garbage in a version — False: silence beats a false banner."""
def pypi_latest(name: str, *, fetch: Fetcher | None = None) -> str | None
def check(*, settings: Settings, entries: list[AgentEntry],
          force: bool = False, fetch: Fetcher | None = None) -> list[Update]
    """No more than once a day; result and timestamp live in <cache>/updates.json.
    settings.check_updates=False — [] and NOT A SINGLE request."""

# announcements.py
FEED_URL = "https://raw.githubusercontent.com/MAY4VFX/houdini-agent-panel/main/feed/announcements.json"

@dataclass(frozen=True)
class Button:
    label: str
    url: str = ""

@dataclass(frozen=True)
class Announcement:
    id: str
    severity: str          # "info" | "blocking"
    title: str
    body: str = ""
    buttons: tuple[Button, ...] = ()
    panel_versions: str = ""    # a PEP 440-style specifier, "" — everyone
    expires: str = ""           # ISO 8601, "" — never expires

def parse_feed(payload) -> list[Announcement]
def applicable(items, *, panel_version: str, seen: Collection[str],
               now: datetime | None = None) -> list[Announcement]
def check(*, settings: Settings, panel_version: str, force: bool = False,
          fetch: Fetcher | None = None) -> list[Announcement]

# telemetry.py
def is_enabled(settings: Settings) -> bool
def build_payload(settings, *, event: str, **extra) -> dict
    """Only: panel/fx/agent versions, OS, Houdini version, the fact of a crash and
    its exception type. Never: paths, scene contents, prompt text, agent session
    ids. Checked by a test against forbidden keys."""
def send(event: str, *, settings: Settings, **extra) -> None
    """Disabled or no endpoint set — a no-op with zero network calls.
    Network errors are swallowed: telemetry is not allowed to break anything."""
```

The same daily network trip serves both updates and announcements —
`refresh.py::daily_refresh()`. With both toggles off, we go nowhere, and
that's verified by counting `Fetcher` calls, not by the absence of an
exception.

---

## 10. UI

The widget tree. Each file is one public class; no one reaches into a
neighbor's private attributes, everyone communicates via signals.

```
AgentPanel (ui/panel.py)                 root QWidget, returned by onCreateInterface()
├── HeaderBar (ui/chips.py)              agent chip · $HIP chip · session picker · "+" · gear
├── NoticeStrip (ui/announcement.py)     quiet update/announcement banner
├── QStackedWidget
│   ├── TranscriptView (ui/transcript.py)   the feed
│   ├── AgentsView (ui/agents.py)           the "Agents" screen
│   ├── SettingsView (ui/settings_view.py)  the settings screen
│   └── AuthView (ui/auth_view.py)          the login screen, built from authMethods
└── Composer (ui/composer.py)            input, "+", microphone, mode chip, counter, send/stop
    └── BlockingNotice (ui/announcement.py) popup ABOVE the input field
```

```python
# ui/panel.py
class AgentPanel(QtWidgets.QWidget):
    def __init__(self, parent=None) -> None
    def shutdown(self) -> None      # called from onDeactivateInterface

# ui/chips.py
class HeaderBar(QtWidgets.QWidget):
    agent_clicked = Signal()
    session_selected = Signal(str)
    new_session_clicked = Signal()
    settings_clicked = Signal()
    def set_agent(self, name: str, icon: QtGui.QIcon | None) -> None
    def set_cwd(self, path: str) -> None
    def set_sessions(self, states: list[SessionState], current: str | None) -> None

class ModeChip(QtWidgets.QWidget):      # lives inside Composer
    mode_selected = Signal(str)
    def set_modes(self, modes: list[SessionMode], current_id: str | None) -> None
        """An empty list hides the whole widget. The agent doesn't support it —
        there's no control."""

# ui/transcript.py
class TranscriptView(QtWidgets.QScrollArea):
    permission_answered = Signal(str, str)     # request_key, option_id ("" = cancelled)
    def set_model(self, model: TranscriptModel) -> None
    def refresh(self, entry_id: str | None = None) -> None
        """entry_id=None — redraw everything (session switch). Otherwise — only one
        entry: redrawing the whole feed on every streamed chunk is visibly janky."""

# ui/permissions.py
class PermissionRow(QtWidgets.QWidget):
    answered = Signal(str, str)
    def __init__(self, view: PermissionView, parent=None) -> None
        """Buttons are built strictly from view.options. Order is whatever the
        agent sent. We never add our own buttons."""

# ui/composer.py
class Composer(QtWidgets.QWidget):
    submitted = Signal(list)      # list[dict] — ready-made ACP content blocks
    cancelled = Signal()
    mode_selected = Signal(str)
    def set_capabilities(self, info: AgentInfo | None, whisper: str) -> None
    def set_busy(self, busy: bool) -> None        # send button ↔ stop
    def set_commands(self, commands: list[AvailableCommand]) -> None
    def set_modes(self, modes: list[SessionMode], current_id: str | None) -> None
    def set_usage(self, usage) -> None
    def block_input(self, reason: str) -> None    # a blocking announcement
    def unblock_input(self) -> None
    def is_input_blocked(self) -> bool
        """Public, because "an announcement blocks input but not the feed" is a
        requirement that needs to be checkable by a test, without reaching into a
        neighboring widget's private attributes."""

# ui/agents.py
class AgentsView(QtWidgets.QWidget):
    agent_chosen = Signal(str)
    closed = Signal()

# ui/settings_view.py
class SettingsView(QtWidgets.QWidget):
    changed = Signal()
    closed = Signal()

# ui/auth_view.py
class AuthView(QtWidgets.QWidget):
    method_chosen = Signal(str)
    logout_requested = Signal()
    def set_methods(self, methods: list[AuthMethod], *, can_logout: bool) -> None

# ui/announcement.py
class NoticeStrip(QtWidgets.QWidget):
    action_clicked = Signal(str, str)   # announcement_id, url
    dismissed = Signal(str)
    def show_notice(self, ann: Announcement) -> None
    def show_update(self, update: Update) -> None

class BlockingNotice(QtWidgets.QWidget):
    action_clicked = Signal(str, str)
    def show_notice(self, ann: Announcement) -> None
```

Styling — only through Houdini's Qt palette (widgets inherit it on their
own) and targeted `setStyleSheet` calls on individual widgets. We don't
touch the application's global style: this is someone else's window, and
the rest of Houdini lives in it.

Every colour a `setStyleSheet` call needs as a literal — not just a
`palette(...)` reference — comes from `ui/theme.py`, never a `#rrggbb` or a
numeric `QColor(r, g, b)` written by hand (two tests enforce this by
grepping `ui/*.py`). `theme.py` carries the panel's one accent
(`accent_color()`) and the shared recipe for every flat popup surface
(`popup_stylesheet()`, `popup_background()`/`popup_border()`/
`popup_hover_background()`).

**`QApplication.palette()` is the only source `color()`/`accent_color()`
read — `hou.qt.getColor` isn't consulted first any more, and for most roles
not at all.** Got backwards once, worth spelling out why, and worth being
precise about what part of that reasoning is an observed fact versus an
open question this project genuinely cannot settle by itself.

Certain: Houdini fills the live Qt palette from whatever theme is active,
on every version the panel supports, with no version-specific code needed.
Colour themes as an artist-facing feature (52 presets, each an HSV triple,
in `$HFS/houdini/config/Themes/default.theme.json`) are new in Houdini 22 —
20.5 and 21 have no such file, and a preset recolouring the live palette is
the only way `QApplication.palette()` could ever show one of them at all —
that part isn't speculative.

Not established: whether `hou.qt.getColor("SomeSchemeName")` follows a
preset the same way, or keeps answering from the static `.hcs` file
underneath it. Checking that needs a GUI session with a preset active, and
`hou.qt` doesn't exist even in `hython` on either 20.5.445 or 22.0.368
(that part IS confirmed, by running it) — so this project has never been
able to observe what the scheme-name lookup does under a preset, in either
direction. Believing it stays stale is a plausible, well-motivated guess
(Plumtree's `highlight: [356, 30, 50]` computes to `#7f595b`, nothing like
`SelectedTextBG`'s `SELECTION_BASE`, `HSV 40 0.825 0.725` — the stock
amber; the owner's screenshot showed the mode chip and status dot amber
under Plumtree), not a measurement of the lookup itself — at the time of
that screenshot the panel was reading a hardcoded `#dfa047`, not
`SelectedTextBG`, so the screenshot alone says nothing about what the
lookup would have returned. The palette needing no such guess, on any
version, preset active or not, is reason enough to read it first on its
own — the open question doesn't have to resolve either way for that to
hold.

`_HOU_COLOR_NAMES` (the table `color()` used to consult first, for
`Window`/`Base`/`AlternateBase`/`Text`/`WindowText`/`ButtonText`/
`Highlight`/`Mid`) is gone entirely, not just reordered: every role in it
already has a direct `QPalette` equivalent, so once the palette leads,
nothing was left for that table to do — keeping it "for symmetry" would
have been a mapping that looked like a working mechanism while doing
nothing. `color()` is now exactly `palette().color(group, role)`.
`accent_color()` keeps one narrow `.hcs` fallback of its own, reached only
if the palette has no usable `Highlight` at all (not observed) — see its
docstring in `ui/theme.py`.

Colours are read fresh at the point of use — in `__init__` and again in
`showEvent` — never cached in a module-level constant, so a widget built
under one Houdini colour scheme and later shown again still reflects
whatever's active. There's no known Houdini signal for "the scheme changed
while this pane is already open and visible"; picking that up means hiding
and reshowing the pane (or opening a new tab), not a background poll.

---

## 11. Tests

`pytest`, the entire network mocked via `Fetcher`, the entire disk via
`HAP_DATA_DIR` pointed at `tmp_path`. No test opens Houdini or reaches the
internet.

- `tests/fake_agent.py` — a minimal ACP agent built on `acp.run_agent`, run
  as a separate process. The real `AcpClient` is verified against it:
  streaming, permissions, modes, `auth_required`, cancellation. This is the
  only honest way to test the protocol layer, and it's cheap.
- UI tests — a `QApplication` from `PySide6`, `qWait` instead of `sleep`.
- `tests/test_no_network.py` — with the toggles off, `Fetcher` is never
  called (item 17 from Verification in design.md).

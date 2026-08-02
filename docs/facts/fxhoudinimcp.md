# fxhoudinimcp 2.10.0 — an API reference for reuse

Source: the package installed at
`/private/tmp/claude-501/.../scratchpad/venv/lib/python3.14/site-packages/fxhoudinimcp/`
(pip install fxhoudinimcp==2.10.0). Every path below is relative to the package
root, `fxhoudinimcp/`, unless stated otherwise. Line numbers reflect the files'
state at the time they were read.

`Requires-Python: >=3.10` (METADATA). Classifiers: 3.10, 3.11, 3.12.
`Requires-Dist: httpx>=0.27.0`, `mcp<3,>=1.14.0`, `pydantic>=2.0.0`.
The plugin inside Houdini drops `uiready.py` into 4 Python-lib versions:
`python3.9libs/`, `python3.10libs/`, `python3.11libs/`, `python3.13libs/` —
meaning it covers the Houdini range from Python 3.9 (H19.5) through 3.13 (H21+),
**including 3.11** (H20.5, our target version).

---

## 1. `install.py` — public functions

### Constant
```python
SERVER_NAME = "fxhoudini"   # install.py:54 — the name the server
                              # registers itself under with the MCP client
```

### `client_command() -> list[str]`  (install.py:57-66)
```python
def client_command() -> list[str]:
    return [sys.executable, "-m", "fxhoudinimcp"]
```
Always `sys.executable` (an absolute path), never a bare `python` — because
Claude Desktop/Code launches MCP servers without the user's environment, and
a bare name might resolve to the wrong interpreter.

### `desktop_config_path() -> Path | None`  (install.py:69-85)
```python
def desktop_config_path() -> Path | None:
    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA")
        if not base:
            return None
        return Path(base) / "Claude" / "claude_desktop_config.json"
    if system == "Darwin":
        return (Path.home() / "Library" / "Application Support"
                 / "Claude" / "claude_desktop_config.json")
    return Path.home() / ".config" / "Claude" / "claude_desktop_config.json"
```
Returns the path **regardless of whether the file exists**. On macOS this is
`~/Library/Application Support/Claude/claude_desktop_config.json` — not
directly relevant to houdini-agent-panel (this is about Claude Desktop, not
Houdini), but useful as a reference for the format.

### `claude_code_available() -> bool`  (install.py:88-89)
`shutil.which("claude") is not None`.

### `claude_code_add_argv(scope: str = "user") -> list[str]`  (install.py:92-103)
```python
return ["claude", "mcp", "add", "--scope", scope, SERVER_NAME,
        "--", *client_command()]
```
So the actual registration command is:
`claude mcp add --scope user fxhoudini -- <sys.executable> -m fxhoudinimcp`.

### `claude_code_remove_argv(scope: str = "user") -> list[str]`  (install.py:106-113)
```python
return ["claude", "mcp", "remove", SERVER_NAME, "-s", scope]
```

### `printable_argv(argv: list[str]) -> str`  (install.py:116-118)
```python
def printable_argv(argv: list[str]) -> str:
    return " ".join(f'"{part}"' if " " in part else part for part in argv)
```
Just joins argv into a string, wrapping parts with spaces in quotes
(e.g. a path like `~/Library/.../python` with no spaces stays
unquoted). There's no proper shell-escaping logic here — it's purely for
showing to a human.

### `resolve_houdini_dirs(explicit: str | None) -> tuple[list[Path], str]`  (install.py:121-152)
```python
def resolve_houdini_dirs(explicit):
    if explicit:
        return [Path(explicit).expanduser()], "given on the command line"
    candidates = candidate_package_dirs()   # from houdini_package.py
    if not candidates:
        return [], "no Houdini packages directory exists yet"
    if len(candidates) == 1:
        return candidates, "the only candidate on this machine"
    return candidates, f"every candidate on this machine ({len(candidates)})"
```
Returns a **list** of directories (there can be several Houdini versions on
the machine) plus a reason for the log. An empty list means "there isn't a
single Houdini preferences directory with a `packages/` folder yet." It
never creates directories itself — it only looks at what's already on disk.

### Other install.py functions (for completeness)
- `_merge_desktop_config(existing: dict, command: list[str]) -> dict` (155-175) —
  merges `mcpServers.fxhoudini.{command,args}` into the existing Claude
  Desktop JSON config, without touching other keys (important: someone
  else's `env` with `HOUDINI_HOST`/`HOUDINI_PORT` is preserved).
- `pinned_port_warning(entry) -> list[str]` (178-195) — warns if the config
  pins `HOUDINI_PORT`, which disables the port auto-scan.
- `install_desktop(config, command, dry_run) -> list[str]` (198-244) — writes
  Claude Desktop's config file, makes a `.bak` backup before overwriting.
- `claude_code_current_command() -> str | None` (247-266) — parses the output
  of `claude mcp get fxhoudini`, looking for a `Command:` line.
- `repoint_claude_code() -> list[str]` (304-345) — does a
  `claude mcp remove` + `claude mcp add`, since `claude mcp add` can't
  update an existing entry.
- `install_claude_code(dry_run) -> list[str]` (348-379) — calls
  `claude_code_add_argv()` via `subprocess.run`.
- CLI: `build_parser()` (381-416) with flags `--houdini-dir`, `--client
  {auto,claude-code,claude-desktop,both,none}`, `--client-only`, `--dry-run`.
  `main(argv)` (419-465) calls `_install_plugin_half` (writes
  `fxhoudinimcp.json` into every directory from `resolve_houdini_dirs`) and
  `_install_client_half` (registers with the MCP client according to `--client`).

**Important for houdini-agent-panel**: install.py as a whole is built
specifically around Claude Code/Claude Desktop as clients. Our panel is its
own ACP client, so directly reusing the `install_*` functions doesn't fit
(they write `claude_desktop_config.json` or call `claude mcp add`). What is
reusable is the idea and the code behind `resolve_houdini_dirs`/`desktop_config_path`
as a pattern, and the functions from `houdini_package.py` (see below) — they
aren't tied to a specific client.

---

## 2. `houdini_package.py` — how the plugin's package json is built

```python
PACKAGE_NAME = "fxhoudinimcp.json"                      # :31
CLI = "python -m fxhoudinimcp"                          # :38
```

### `plugin_path() -> Path`  (:41-54)
```python
def plugin_path() -> Path:
    here = Path(__file__).resolve().parent
    packaged = here / "houdini"
    if packaged.is_dir():
        return packaged
    return here.parents[1] / "houdini"
```
In the installed wheel package, the plugin lives at
`<site-packages>/fxhoudinimcp/houdini/` (confirmed in this
instance — `houdini/` really is there).

### `package_json(path: Path | None = None) -> str`  (:57-64)
```python
def package_json(path=None) -> str:
    target = (path or plugin_path()).as_posix()
    return json.dumps(
        {"env": [{"FXHOUDINIMCP": target}], "path": "$FXHOUDINIMCP"},
        indent=4,
    ) + "\n"
```
Returns the JSON string for Houdini's package file. Forward slashes on
every OS.

The actual reference file baked into the plugin distribution
(`houdini/fxhoudinimcp.json`, a template, not generated on the fly — used
as a sample/default inside the plugin itself):
```json
{
    "env": [
        {"FXHOUDINIMCP": "/absolute/path/to/fxhoudinimcp/houdini"},
        {"FXHOUDINIMCP_PORT": "8100"},
        {"FXHOUDINIMCP_BIND": "127.0.0.1"},
        {"FXHOUDINIMCP_AUTOSTART": "1"},
        {"FXHOUDINIMCP_AUTO_LAYOUT": "1"}
    ],
    "path": "$FXHOUDINIMCP"
}
```
This differs from what `write_package()` actually writes — that one only
writes `{"env": [{"FXHOUDINIMCP": <path>}], "path": "$FXHOUDINIMCP"}` (without
the other four variables). The remaining `FXHOUDINIMCP_*` variables
apparently get read with defaults right in the code (`os.environ.get(..., "8100")`
and the like), and this file is just a sample/documentation of the
package-file format for anyone who wants to set them explicitly.

### `candidate_package_dirs() -> list[Path]`  (:67-100)
```python
def candidate_package_dirs() -> list[Path]:
    home = Path.home()
    roots: list[Path] = []
    system = platform.system()
    if system == "Windows":
        roots += [home / "Documents", home / "OneDrive" / "Documents", home]
    elif system == "Darwin":
        roots += [home / "Library" / "Preferences" / "houdini"]
    else:
        roots += [home]
    found = []
    for root in roots:
        if not root.is_dir():
            continue
        for entry in sorted(root.glob("houdini*")):
            packages = entry / "packages"
            if entry.is_dir() and packages.is_dir():
                found.append(packages)
    return found
```
On macOS it looks for `~/Library/Preferences/houdini/houdini*/packages` —
matching what's already documented in the project's CLAUDE.md
(`~/Library/Preferences/houdini/20.5/`). It only returns already
**existing** `packages/` directories — it creates nothing.

### `existing_packages(exclude=None) -> list[tuple[Path, str]]`  (:118-160)
Looks for already-written `fxhoudinimcp.json` files in the candidate
directories (excluding any given), parses their `env[].FXHOUDINIMCP`, and
returns a list of `(file_path, what_it_points_to)`. Needed for the "multiple
package files, the last one wins" warning.

### `write_package(destination: Path, path: Path | None = None) -> Path`  (:163-178)
```python
def write_package(destination: Path, path: Path | None = None) -> Path:
    if not destination.is_dir():
        raise NotADirectoryError(destination)
    target = destination / PACKAGE_NAME
    target.write_text(package_json(path), encoding="utf-8", newline="\n")
    return target
```
Writes **without a BOM** (`encoding="utf-8"`, not `utf-8-sig`) — a comment
states explicitly that a BOM breaks Houdini's JSON parser and the file gets
silently ignored (issue #11 in their repo).

`main(argv)` (:181-263) — the CLI `fxhoudinimcp houdini-package [--write DIR]
[--path-only]`, a thin wrapper around everything above.

**Takeaway for houdini-agent-panel**: if the panel installs ITS OWN Houdini
plugin (rather than reusing fxhoudinimcp's), `candidate_package_dirs()`,
`write_package()`/`package_json()` are worth copying 1:1 as a pattern (the
same pitfalls apply: BOM, several packages directories, "don't guess the
directory"). The functions themselves are tied to this specific package's
`plugin_path()`, so it's copy-paste of the logic, not importing the module.

---

## 3. `server.py` + `bridge.py` — port selection and finding a live server

### Environment variables (client side — the MCP server process
launched via `python -m fxhoudinimcp`)
```python
host = os.getenv("HOUDINI_HOST", "localhost")   # server.py:57
pinned = os.getenv("HOUDINI_PORT")              # server.py:58
port = int(pinned) if pinned else 8100          # server.py:59
```
If `HOUDINI_PORT` is **not set explicitly**, the client scans a port range:
```python
if not pinned:
    servers = await find_servers(host, port)    # server.py:66, base=8100
    if servers:
        port = servers[0]["port"]                # takes the first (lowest) live one
```
`find_servers` (bridge.py:39-67):
```python
PORT_SEARCH_RANGE = 16   # bridge.py:36 — i.e. the range 8100..8115 inclusive

async def find_servers(host, base, max_tries=PORT_SEARCH_RANGE, timeout=1.0):
    found = []
    async with httpx.AsyncClient(timeout=timeout) as client:
        for port in range(base, base + max_tries):
            try:
                response = await client.post(
                    f"http://{host}:{port}/api",
                    data=_rpc_body("mcp.health"),
                )
                response.raise_for_status()
                payload = response.json()
            except Exception:
                continue
            if isinstance(payload, dict) and payload.get("status") == "ok":
                found.append({**payload, "port": port})
    return found
```
So **there's no file or other API for discovering the port** — it's a plain
sequential HTTP probe: `POST http://<host>:<port>/api` with an
`mcp.health` body, for every port in the base..base+15 range. If several
Houdini sessions respond, the first one (lowest port) is used, the rest are
logged as a warning (server.py:69-78). The only way to
"find out from inside a Houdini process which port its server actually
came up on" is also this same HTTP probe of `mcp.health`; inside Houdini
this is done by `fxhoudinimcp_server.startup.get_port()` (see section 5) —
but that's an in-process variable of the plugin, unreachable from outside
except via HTTP.

No file with the port is ever written to disk (neither in `$TMPDIR` nor in
preferences) — discovery is purely by HTTP scan.

### Environment variables — the plugin side inside Houdini (startup.py)
```python
base = port or int(os.environ.get("FXHOUDINIMCP_PORT", "8100"))   # startup.py:175
```
```python
address = os.environ.get("FXHOUDINIMCP_BIND", "127.0.0.1")        # startup.py:128
```
```python
if os.environ.get("FXHOUDINIMCP_AUTOSTART", "1") == "1":          # uiready.py:12
```
```python
value = hou.getenv("FXHOUDINIMCP_AUTO_LAYOUT")                    # config.py (houdini-side) :22
if value is None:
    value = os.environ.get("FXHOUDINIMCP_AUTO_LAYOUT", "1")
```

Full list of the package's env variables:
| Variable | Read where | Default | Meaning |
|---|---|---|---|
| `HOUDINI_HOST` | server.py:57 (MCP client) | `localhost` | which host to talk to |
| `HOUDINI_PORT` | server.py:58 (MCP client) | none (then auto-scan) | pins a specific port, **disables the auto-scan** |
| `FXHOUDINIMCP_PORT` | startup.py:175 (plugin in Houdini) | `8100` | base for picking a free port |
| `FXHOUDINIMCP_BIND` | startup.py:128 (plugin) | `127.0.0.1` | hwebserver's bind address (loopback only by default — deliberately, since the endpoint executes arbitrary Python with no authentication) |
| `FXHOUDINIMCP_AUTOSTART` | uiready.py:12 (plugin) | `1` | auto-start the server once Houdini's UI is ready |
| `FXHOUDINIMCP_AUTO_LAYOUT` | config.py:14-25 (both, client and plugin) | `1` | let tools auto-lay-out nodes in the network editor |
| `MCP_TRANSPORT` | __main__.py:135 (client) | `stdio` | the MCP server's transport |
| `LOG_LEVEL` | __main__.py:124 (client) | `INFO` | logging level |

If a given Houdini session's real port differs from the base one (e.g. a
second open Houdini took 8100 and the first one ended up on 8101), the
plugin logs it to the Houdini console (startup.py:180-184) and in the UI
menu item "Connect a Client..." (MainMenuCommon.xml:85-90); the client on
its side finds this automatically via the scan, as long as `HOUDINI_PORT`
isn't pinned.

---

## 4. `bridge.py` — the HTTP protocol and how the MCP server starts for the client

Houdini's `hwebserver` is RPC-style:
```
POST /api
Content-Type: application/x-www-form-urlencoded
Body: json=["namespace.function", [positional_args], {keyword_args}]
```
(bridge.py:1-10). Implemented via `_rpc_body(func_name, **kwargs)`
(bridge.py:29-31), which wraps it as `{"json": json.dumps([func_name, [], kwargs])}`.

`HoudiniBridge.execute(command, params, timeout)` (bridge.py:123-207) sends
`mcp.execute` with `{"command": ..., "params": ..., "request_id": uuid4()}`,
parses the `status: success|error` in the reply, converts Houdini errors into
`HoudiniCommandError`/`ConnectionError` (errors.py). There's a retry on
`httpx.RemoteProtocolError` (recreates the connection pool) — relevant after
a Houdini restart (bridge.py:98-121).

`HoudiniBridge.health_check()` (bridge.py:209-232) → `mcp.health`.
`HoudiniBridge.list_commands()` (bridge.py:234-252) → `mcp.list_commands`.

### The command that launches the MCP server for the client (the exact argv)

From `install.py`:
```python
command = "auto"           # sys.executable, an absolute path to Python
args = ["-m", "fxhoudinimcp"]
```
i.e. the final object for `mcpServers`:
```json
{
  "fxhoudini": {
    "command": "/absolute/path/to/python",
    "args": ["-m", "fxhoudinimcp"]
  }
}
```
(This is exactly what `_merge_desktop_config()` writes, install.py:168-175, and what
`claude mcp add` sends via `claude_code_add_argv()`, install.py:92-103.)
`env` isn't set automatically by install.py — if it isn't set explicitly, the
process inherits the parent's (client's) environment, and
`HOUDINI_HOST`/`HOUDINI_PORT` fall back to `localhost`/an auto-scan over
8100-8115. A port/host can be pinned explicitly by adding to `env` by hand:
```json
{
  "fxhoudini": {
    "command": "/absolute/path/to/python",
    "args": ["-m", "fxhoudinimcp"],
    "env": {"HOUDINI_HOST": "localhost", "HOUDINI_PORT": "8101"}
  }
}
```
Install.py warns (`pinned_port_warning`, install.py:178-195) that setting
`HOUDINI_PORT` disables the auto-scan for *other* Houdini sessions.

**For houdini-agent-panel**: the panel should build `mcpServers[0]` in
exactly this shape — `{name: "fxhoudini", command: <python>, args: ["-m",
"fxhoudinimcp"], env: {...optional...}}`. `<python>` is the path to the
interpreter that **has the fxhoudinimcp package installed** (not necessarily
the panel's own `sys.executable` — Houdini usually has its own Python, and
fxhoudinimcp is either installed into the user's system/venv Python, calling
it separately as an MCP stdio server, or via `pip install` into the same
Python the panel itself runs under, if the panel does that itself. It's
worth checking exactly where the panel installs its dependencies — that's
outside the scope of this file).

---

## 5. `node_versions.py` — IMPORTANT: this is NOT about Node.js!

The `node_versions.py` file has nothing to do with the JS runtime Node or
installing/downloading it. The name comes from **Houdini node types**
(node types in a network, e.g. `colorcorrect`, `layout`) — the module tracks
**which Houdini versions have which nodes**, in order to warn if instructions
for the LLM are out of date.

```python
_TABLE = Path(__file__).parent / "data" / "sampled_versions.json"   # :29

@lru_cache(maxsize=1)
def load_table() -> dict: ...        # reads a JSON {"builds": {...}, "series": [...]}

def series_of(version: str | None) -> str | None: ...   # "22.0.368" -> "22.0"

def sampled_series() -> list[str]: ...   # list of minor Houdini series
                                          # covered by data, sorted

def staleness_warning(version: str | None) -> str | None:
    # None if the version is covered by data; otherwise a warning string
    # "older/newer than anything in the sample table"
```
There's no logic anywhere in the package for finding/downloading a real
Node.js (JavaScript runtime), and the phrase "download node" doesn't appear
in a single file — only "node types" in the context of Houdini SOP/LOP/etc.
nodes. If the team needs logic for finding/downloading Node.js for the panel
(e.g. because an ACP agent is a Node process), that functionality does
**not** exist in this package — look elsewhere.

---

## 6. `_loader.py`, `houdini/` — how the plugin starts up inside Houdini

### `_loader.py` (top-level, for the MCP server client, not the plugin)
```python
_MD_DIR = Path(__file__).parent / "prompts" / "markdown"

@cache
def _read(name: str) -> str: ...      # reads a markdown file once, caches it

def load_markdown(name: str, **kwargs: str) -> str: ...
```
Loads and caches markdown instruction/prompt files
(`prompts/markdown/instructions/…`, `workflows/…`, `shared/…`). Has nothing
to do with starting the server inside Houdini — it's for MCP's
`instructions=` and `@mcp.prompt()`.

### The `houdini/` structure (the plugin, installed into Houdini packages)
```
houdini/
├── fxhoudinimcp.json           # a template package file (see section 2)
├── MainMenuCommon.xml           # MCP > Start/Stop/Connect/Status menu items
├── python3.9libs/uiready.py     # identical code for every Houdini Python version
├── python3.10libs/uiready.py
├── python3.11libs/uiready.py    # ← current for Houdini 20.5
├── python3.13libs/uiready.py
└── scripts/python/fxhoudinimcp_server/
    ├── __init__.py
    ├── startup.py                # starting/stopping hwebserver, port selection, readiness poll
    ├── config.py                 # auto_layout_enabled() via hou.getenv
    ├── dispatcher.py             # command -> handler routing
    ├── errors.py
    ├── serialize.py              # json_default for non-serializable HOM objects
    ├── outputs.py
    ├── ui.py
    ├── hwebserver_app.py          # registers HTTP endpoints (mcp.execute/health/...)
    └── handlers/*.py              # ~20 files, the real logic grouped by category
```

### Auto-start: `uiready.py` (identical across every `python*libs/`)
```python
# fxhoudinimcp/houdini/python3.11libs/uiready.py — in full:
import os

if os.environ.get("FXHOUDINIMCP_AUTOSTART", "1") == "1":
    try:
        import fxhoudinimcp_server.startup
        fxhoudinimcp_server.startup.ensure_running(wait=False)
    except Exception as e:
        print(f"[fxhoudinimcp] Auto-start failed: {e}")
```
`uiready.py` is a special file that Houdini picks up and runs on its own
**once, after UI initialization** (a comment in the file clarifies: "unlike
`scripts/456.py`, this stacks correctly with other packages that also
define `uiready.py`" — i.e. this is a Houdini-native mechanism, not
something homegrown). `wait=False` means the readiness poll runs on a
separate thread, without blocking Houdini's UI.

### `startup.py` — the server's lifecycle (the whole module is key)
- `_pick_free_port(base, probe=None, my_pid=None, max_tries=16)` (81-113):
  walks up from `base`, skipping ports that respond as **someone else's**
  pid; a port that responds as **its own** pid is returned as-is (idempotent
  on restart); a port with no response is free.
- `_bind_localhost_only(hwebserver)` (116-137): strictly restricts the bind
  address to `127.0.0.1` (or `FXHOUDINIMCP_BIND`) **before** starting —
  `hwebserver.setSettingsForPort({"ADDRESS": address}, "main")` (argument
  order matters: the dict first, then the port name "main").
- `start(port=None, background=None, wait=True)` (140-239): imports
  `hou`, `hwebserver`, registers handlers via
  `from fxhoudinimcp_server import handlers, hwebserver_app`, calls
  `hwebserver.run(_port, debug=False, in_background=background)`.
  `background` defaults to `hou.isUIAvailable()`.
- `ensure_running(wait=True)` (317-332): an idempotent start, used both by
  the auto-start (`wait=False`) and by the "Start Server" menu item (`wait=True`,
  implicitly via `mcp.start()` in MainMenuCommon.xml).
- `get_port() -> int` (307-309), `is_running() -> bool` (302-304),
  `is_starting() -> bool` (312-314) — public status, used by the "Server
  Status..." menu item and could be used by the panel too, **but only from
  inside the Houdini process** (via `mcp__fxhoudini__execute_python` or
  `hou.session`, not from outside).

### `hwebserver_app.py` — registering HTTP endpoints
```python
@_api_function("mcp")
def execute(request, command="", params=None, request_id=""):
    result = dispatcher.dispatch(command, params)
    result["request_id"] = request_id
    return _json_response(result)

@_api_function("mcp")
def health(request):
    return {"status": "ok", "pid": os.getpid(),
            "houdini_version": os.environ.get("HOUDINI_VERSION", "unknown")}

@_api_function("mcp")
def session_info(request):
    return _json_response(dispatcher.dispatch("scene.get_scene_info", {}))

@_api_function("mcp")
def list_commands(request):
    return {"commands": dispatcher.list_commands()}
```
`health` **deliberately never touches `hou.*`** (a comment explains: the
main thread might be busy with the startup's own readiness loop —
reaching into HOM from a worker thread at that moment would deadlock the
process). That's why `health` has no `hip_file` — getting it requires a
separate call to `scene.get_scene_info` (via the `session_info` endpoint or
via a plain `mcp.execute`).

---

## 7. `compat.py`, `config.py` — config and compatibility (NOT a user data dir)

**The package has no user config/data directory** (no
`platformdirs`/`appdirs`, no `~/.fxhoudinimcp/` or anything like that was
found). The only "config" files on disk are:
1. Houdini's package file `fxhoudinimcp.json` in `<houdini-prefs>/packages/`
   (sections 2/3) — the path to the plugin plus optional extra env variables.
2. The client's config (`claude_desktop_config.json` and the like) — belongs
   to the MCP client, not to fxhoudinimcp itself.

### `compat.py` (top-level — the MCP client side)
Compares the list of commands the plugin actually registered
(`bridge.list_commands()`) against the list of commands the server needs
(`data/required_commands.json`, generated from the `execute()` call sites):
```python
def missing_commands(available: list[str] | None) -> list[str]:
    required = required_commands()
    if not required or not available:
        return []
    ...
    return sorted(required - set(available))

def compatibility_warning(available) -> str | None:
    # None if nothing's missing, otherwise text listing the command names
```
Used in `server.py`'s lifespan (the server warns on connect if the plugin
is older than the server) and in `tools/scene.py:get_houdini_connection_status`.

### `config.py` (top-level, client side)
```python
_FALSY = {"0", "false", "off", "no"}

def auto_layout_enabled() -> bool:
    value = os.getenv("FXHOUDINIMCP_AUTO_LAYOUT", "1")
    return value.strip().lower() not in _FALSY
```
Duplicated (adjusted for `hou.getenv`) in
`houdini/scripts/python/fxhoudinimcp_server/config.py` — on the plugin side.

---

## 8. Liveness check: the health endpoint

`mcp.health` (hwebserver_app.py:97-115):
```json
{"status": "ok", "pid": 12345, "houdini_version": "20.5.584"}
```
**Does NOT contain `hip_file`** — deliberately (see section 6, the comment
about the main-thread deadlock). Getting `hip_file` needs a separate
request — a ready-made example is in
`tools/scene.py:get_houdini_connection_status`
(lines 20-95, a top-level MCP tool, not a plugin endpoint):
```python
health = await bridge.health_check()          # mcp.health, no hip_file
...
if "hip_file" not in health:
    scene = await bridge.execute("scene.get_scene_info", timeout=5.0)
    for key in ("hip_file", "houdini_version"):
        if scene.get(key) is not None:
            health.setdefault(key, scene[key])
```
`scene.get_scene_info` is implemented in
`houdini/scripts/python/fxhoudinimcp_server/handlers/scene_handlers.py:70,89`:
```python
hip_path = hou.hipFile.path()
...
"hip_file": hip_path,
```
This is exactly what's mentioned in the houdini-agent-panel project's
`CLAUDE.md`: "fx's `get_houdini_connection_status` reports `connected:
true`" — this is the `fxhoudinimcp/tools/scene.py` MCP tool, available
through the MCP server already installed in this session as
`mcp__fxhoudini__get_houdini_connection_status`.

`houdini_version` is taken from the `HOUDINI_VERSION` environment variable
that Houdini itself exports — it isn't computed by the plugin's own code
(hwebserver_app.py:114).

---

## Summary (what to reuse in houdini-agent-panel)

1. **The mcpServers entry for the agent** — the exact shape:
   `{"fxhoudini": {"command": "<python with fxhoudinimcp installed>",
   "args": ["-m", "fxhoudinimcp"], "env": {optionally HOUDINI_HOST/PORT}}}`
   (install.py:57-66, 92-103, 168-175).
2. **Finding a live Houdini** — only an HTTP scan of `POST /api` with
   `mcp.health` across ports 8100..8115 (bridge.py:36,39-67; server.py:57-80).
   There's no file/API for "find out the port from outside" other than this
   scan. The range and timeout (`1.0s` per port) are the only parameters.
3. **Health/liveness** — `mcp.health` gives `status/pid/houdini_version`, WITHOUT
   `hip_file`; getting `hip_file` requires a separate `scene.get_scene_info`
   call (see `get_houdini_connection_status` as a ready-made pattern, tools/scene.py:20-95).
4. **Installing Houdini's package file** — the pattern from `houdini_package.py`
   (candidate directories only for macOS `~/Library/Preferences/houdini/
   houdini*/packages`, write without a BOM, write to every candidate, don't
   guess) is reused as *logic*, not as importable code (it's tied to this
   package's `plugin_path()`).
5. **`node_versions.py` is not about Node.js** — if the panel needs to
   find/install a Node.js runtime, that functionality doesn't exist in
   fxhoudinimcp at all.
6. **Plugin auto-start** — a Houdini-native `uiready.py` (not our code,
   Houdini itself picks it up after UI initialization) — if the panel
   installs ITS OWN Houdini plugin, this is a ready-to-copy pattern for
   auto-starting without blocking the UI (a thread + `wait=False`).
7. **No user config/data dir** in the package — if the panel needs to store
   its own settings, fxhoudinimcp's approach is to write config next to the
   MCP client (the Desktop config) or use the Houdini package's env, not a
   file in the user's home directory.

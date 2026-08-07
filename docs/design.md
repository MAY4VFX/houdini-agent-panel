# Houdini Agent Panel — an agent panel inside Houdini

## Context

To use an agent against a Houdini scene today you need an open terminal running `claude` and MCP set up by hand through config files and ports. For an artist that's a non-starter: they won't open a terminal and won't edit `~/.claude.json`.

SideFX hasn't shipped an official solution. The APEX Script MCP shown at the Houdini 22 keynote is a reference tool for one language aimed at riggers — it doesn't drive the scene and isn't published anywhere. The commercial Houdini AI Assistant ($129) solves the problem with its own BYO-key client, an overcomplicated UI (an eight-field dialog just to log in, a magic `ACPY:` prefix for action mode), and, going by reviews, a weak scene-manipulation layer.

Task: a layer on top of **fxhoudinimcp** (189 tools, the official `hwebserver` under the hood) that gives Houdini a native chat panel. Installed by an installer, the agent comes up on its own, the artist never sees ports or config files. UI modeled on Claude Code: minimal buttons, slash commands, a mode chip.

**We don't write the agent or the Houdini tools.** We write the ACP client and installer that tie together existing, already-built code.

## Approach

A separate repository, `MAY4VFX/houdini-agent-panel`, MIT-licensed. Package `houdini-agent-panel` with a hard dependency on `fxhoudinimcp`.

### Four layers

| Layer | Responsibility | Doesn't know about |
|---|---|---|
| Panel (`.pypanel`, Qt) | feed, input, chips, update banner | ACP, Houdini |
| ACP client | sessions, streaming, permissions, modes, login | Houdini |
| Registry/runtime | download the agent and Node, verify sha256 | UI |
| Scene bridge | — not our code, this is fxhoudinimcp as-is | — |

### v1 agents

From the official ACP registry:

| Agent | License | Distribution | Note |
|---|---|---|---|
| Claude Agent | proprietary | npx | |
| Codex | Apache-2.0 | npx | |
| Gemini CLI | Apache-2.0 | npx | |
| Grok Build | proprietary | npx | this is Grok CLI |
| Kimi CLI | MIT | binary | **no darwin-x86_64 build** — the panel must state the reason, not hide the agent |
| OpenCode | MIT | binary | every platform; the path for local and remote custom models |

Plus **"Custom Agent"** — a field for a command and arguments, speaking ACP with whatever's already installed on the machine. No download, no versions. Covers everything the registry doesn't.

**Installed selectively, not in bulk.** By default the installer doesn't install a single one: it either asks, or accepts `--agents claude,codex`. The same "Agents" screen lives permanently inside the panel — install, update, or remove any registry agent at any time. We download exactly one chosen agent, not six just in case.

**Node is mandatory**: 4 of 6 agents install via npx, and without vendoring Node the panel would be nearly empty.

**Custom and remote models (Hermes and similar)** plug in not as an agent but as a model inside OpenCode or goose: ACP has no concept of a remote agent, but the agent itself is a local process, while the model's endpoint can be anywhere.

### Verified facts the design rests on

- **ACP is stdio only.** The client spawns the agent's process and talks over stdin/stdout. The protocol has no notion of remote agents.
- **Many sessions over one connection** — stated directly in ACP's architecture docs.
- **Panels**: `.pypanel` is XML with an `<interface>` and a `<script>`, where `onCreateInterface()` returns a Qt widget. H22 ships 60 of these, it's a standard mechanism.
- **`hutil.PySide`** — Houdini's own shim: on H22 it hands back PySide6, on 20.5 it hands back PySide2. One codebase for both versions. Confirmed during recon: the shim didn't appear right at the start of the 20.5.x line — it's missing in 20.5.278 (only `hutil.Qt` exists there), present by 20.5.445. That's why imports go through our own `ui/qt.py` module: `hutil.PySide` → PySide6 → PySide2.
- **H21 compatibility** — verified on 21.0.792: Python 3.11.7 and PySide6/Qt
  6.5.3. It reuses the `py3.11` dependency tree from H20.5 while taking the
  Qt6 path in `ui/qt.py`; the real GUI render/lifecycle probe passes.
- **Qt in H22**: `QtWidgets`, `QtNetwork`, `QtWebSockets`, `QtWebEngineWidgets` are all available.
- **ACP SDK**: `agent-client-protocol` 0.12.0 on PyPI, `>=3.10,<3.15` — covers H20.5 (3.11) and H22 (3.13).
- **Registry**: `https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json`. An entry carries `version` and `distribution`: either `npx.package` or `binary.<platform>` with `archive`, `cmd`, `args`, `sha256`.
- **`session/new`**: `cwd` (absolute, required) + `mcpServers` as a list of `{name, command, args, env}`.
- **Login**: `initialize` returns `authMethods` (`id`, `name`, `description`); the client calls `authenticate(methodId)`; working without logging in yields `auth_required`; `logout` exists if `agentCapabilities.auth.logout` is declared.
- **Permissions**: `session/request_permission` with an `options` array, each with `optionId`, `name`, `kind` ∈ {`allow_once`, `allow_always`, `reject_once`, `reject_always`}.
- **Modes**: `availableModes` + `currentModeId`, switched via `session/set_mode`, notified via `current_mode_update`.
- **Slash commands**: `available_commands_update`, invoked as plain text.
- **Attachments**: content blocks `text`, `image` (cap `image`), `audio` (cap `audio`), embedded resource (cap `embeddedContext`), `resource_link`, @-mentions.
- **The feed**: `session/update` delivers `agent_message_chunk`, `plan`, `tool_call`, `tool_call_update`, `usage_update`. Call kinds: `read`/`edit`/`delete`/`move`/`search`/`execute`/`think`/`fetch`/`other`. Statuses: `pending`/`in_progress`/`completed`/`failed`.
- **The fx port floats**: the server takes the first free one out of 8100..8115, the bridge searches "the first live one bottom to top." Pinning it via `HOUDINI_PORT` disables the scan (`fxhoudinimcp/server.py:58`). Clarified during recon: the panel lives in the same process as the server, so it doesn't scan for its own port — it reads it from `fxhoudinimcp_server.startup.get_port()`. The scan remains a fallback and can find someone else's Houdini rather than its own.
- **Installed inside Houdini, not onto `PYTHONPATH`.** Found during recon, and it changes the install plan: `pydantic` carries a compiled `pydantic_core`, and Python 3.11 (H20.5) and 3.13 (H22) have different ABIs, so putting the installer Python's site-packages onto Houdini's `PYTHONPATH` isn't an option. `hython` on both versions ships a working pip (verified), so the installer installs the panel and all its dependencies into a separate tree per Python version. Details in [`architecture.md`](architecture.md) §0.
- **The Houdini process's cwd is the home folder**, not the project. Hence `$HIP`.

### Key decisions

**We invent nothing on top of the agent.** Login, modes, permissions, slash commands, attachments — everything arrives as data from the agent. The rule: *the agent doesn't support it — the control doesn't get drawn.* This is what keeps the panel from turning into an eight-field dialog.

**Bound to its own scene.** The panel knows the fx server's actual port in its own process and passes the agent `mcpServers[0].env = {HOUDINI_HOST, HOUDINI_PORT}`. Two open Houdinis — each panel works against its own scene.

**One agent process per agent id, many sessions.** Not one process per Houdini — that was the original decision (see the note below) and it broke: an artist opening a second panel tab on a *different* agent had that tab's switch silently stop and clear the first tab's own conversation and connection, because there was only ever one shared client and one shared session pool for the whole process, regardless of which agent any given tab was actually talking to. Two tabs on the SAME agent still share one process and one session list, independently by `sessionId`; a tab on a DIFFERENT agent gets its own of both. Which agent a given tab is attached to is a fact about that tab (`AgentPanel._agent_id`), not about the process — see `docs/architecture.md` §7.

**We vendor Node.** A suitable system one — use it; none — download the official archive from nodejs.org, verify it against `SHASUMS256.txt`, extract it into our own directory. The system is never touched. Precedent: Houdini itself, which ships its own Python.

**cwd = `$HIP`**, shown as a string with no picker. Access to files outside that folder is gated by the standard permission request.

**Updates via version comparison.** Agents: `version` from the registry against what's installed. The panel and fx: `pypi.org/pypi/<name>/json`. Cached, but not for a flat day anymore — that stopped being right once this project started shipping several releases in an hour and a day-old cache kept saying nothing about them (`updates.py::_FRESH_START_MAX_AGE`/`_SESSION_MAX_AGE`): a panel that just opened trusts the cache for minutes, one that's already been running trusts it for a couple of hours, via a recurring re-check (`ui/panel.py::_on_session_refresh_due`) so a panel left open all day still notices without a restart. A quiet "Update available X → Update" banner, not a modal.

**Telemetry — anonymous, with explicit consent.** Asked at first launch, off by default. Only panel/fx/agent versions, OS, and crash facts. Never scene contents, prompts, or paths. Can be turned off at any time; needs a short policy page in the repository.

**Announcements — a communication channel to users.** A static JSON file at a fixed address, fetched by the same daily request as version checks. A message has: `id`, severity, title, text, buttons with links, version targeting, an expiration date. Shown `id`s are remembered locally.

In the normal case it behaves like a desktop client's update notification — a quiet banner. For anything important — a **popup over the input field**: the feed stays readable, the panel can be closed, Houdini keeps working, but messaging the agent is blocked until the button in the popup is pressed. This also lays the groundwork for future monetization (limits, "buy me a coffee") and any urgent communication.

A limitation worth understanding: there's no way to verify whether the link was actually followed — only the fact that the button that opened it was pressed gets recorded.

We will never block Houdini itself: an error in the feed must not stop someone else's work on their scene.

**Panel settings** (a minimal set, its own screen): default agent; auto-start the agent when the panel opens; check for updates; show announcements; telemetry; the data folder with an "Open" button; a local whisper endpoint for voice on agents lacking the `audio` capability; "Copy diagnostics" for bug reports.

**Asynchrony.** The SDK is async, Qt is synchronous. The ACP client lives on its own asyncio loop on a worker QThread, and hands things to the UI via Qt signals. `hou` is never touched from that thread: scene work goes through the separate fx process. No `qasync`. This is the part most at risk of bugs — look here for UI stalls and races.

### Installation

Local only:
```
pip install houdini-agent-panel
python -m houdini_agent_panel install     # → $HOUDINI_USER_PREF_DIR/packages
```

We don't hardcode paths anywhere, so a networked mode can be added later without a rewrite. A TD can still drop the tree onto `$HSITE` by hand — that's a standard Houdini mechanism, we just don't automate or test that path in v1.

Why the networked mode is deferred: **logging in is always personal.** A network install removes the "install" step but not the "log in" one — everyone has their own Claude/Gemini/Grok account. Plus pre-populating a share for every OS means hauling three copies of Node and three `node_modules` trees there, versioning and updating them. That's a distribution's job, not the panel installer's, and doing it blind, without real studio users, is premature.

### UI

One column, three zones.

**Top** — an agent chip (icon from the registry plus a name, clicking it switches and jumps to the "Agents" screen), a working-folder chip for `$HIP`, session picker on the right, a "+" and a settings gear. Quiet announcements and the update banner slot in here as a row.

**Middle** — the feed. Messages with no borders. A tool call is a collapsible row with an icon by `kind` and a live status. The agent's plan is a block with a list of steps. A permission request is a row with buttons built from the agent's `options`.

**Bottom** — a growing input field. On the left: a "+" for files (only with `image`/`embeddedContext`), a microphone (only with `audio` or a configured local whisper), a mode chip built from `availableModes`. On the right — a counter from `usage_update` and a send button that turns into a stop button.

Slash commands — a popup over the field on typing `/`, a list from `available_commands_update`.

A blocking announcement sits **above the input field**: the feed stays readable, input is blocked until the message's button is pressed.

## Files

```
python/houdini_agent_panel/
  __main__.py          # CLI: install / uninstall / houdini-package
  install.py           # package json into user prefs
  registry.py          # ACP registry, picking the distribution for the platform
  runtime.py           # download + sha256 + extract: agents and portable Node
  client.py            # ACP on top of agent-client-protocol, asyncio on a QThread
  sessions.py          # session pool per agent id, over that agent's connection
  auth.py              # authMethods / authenticate / logout
  updates.py           # versions from the registry and PyPI, cached (window depends on fresh_start)
  announcements.py     # announcements feed, version targeting, seen-id memory
  settings.py          # reading/writing panel settings
  telemetry.py         # optional, off by default
  ui/
    panel.py           # root widget
    transcript.py      # rendering by kind/status, plan, messages
    permissions.py     # buttons from the agent's options
    composer.py         # input, slash popup, attachments, microphone
    chips.py            # agent, mode, folder, session, tokens
    agents.py           # install/update/remove screen for agents
    settings_view.py    # settings screen
    announcement.py      # banner and blocking popup over input
houdini/
  python3.11libs/uiready.py
  python3.13libs/uiready.py
  python_panels/houdini_agent.pypanel
tests/
```

Qt imports — only through `hutil.PySide`. Reuse from `fxhoudinimcp/install.py`: `resolve_houdini_dirs`, `desktop_config_path`, `printable_argv` — don't duplicate the definition of Houdini's paths across three OSes.

## Verification

1. `python -m houdini_agent_panel install --dry-run` — prints the plan, changes nothing. Without `--agents`, no agent gets downloaded.
2. Install, restart Houdini 22 → the panel is in the panels menu, fx comes up on its own, the agent picker shows on first open.
3. Pick OpenCode (a binary, no Node) → downloads, hash verifies, starts; the reply streams in.
4. Pick Claude Agent (npx) on a machine **without** Node → the panel installs a portable Node and starts the agent.
5. An agent with no login → `auth_required`, the panel shows the methods from `authMethods`, logging in works.
6. Ask it to create a node → the feed shows an fx tool call moving through statuses, the node shows up in the scene.
7. Ask for a shell command → permission buttons from the agent; `reject_once` cancels it.
8. Switch the mode chip → `session/set_mode`, behavior changes.
9. Open a second panel on the SAME agent → a new session, the first conversation is still alive, there's one agent process (verify with `ps`). Open a second panel on a DIFFERENT agent instead → its own process, and switching it around must not disturb the first panel's conversation or connection at all.
10. A second Houdini with a different scene → its panel works against its own scene (cross-check `hip_file` in fx's health).
11. "Custom Agent" with an arbitrary command → the connection comes up, the session works.
12. Fake a version in the cache → a banner appears, the button updates it.
13. Install a second agent from the "Agents" screen → shows up in the list, switching works; removing it wipes its data folder.
14. Drop a normal-severity announcement into the feed → a quiet banner, dismissible, doesn't show a second time.
15. Drop a blocking announcement → a popup over the input, the agent can't be messaged, the feed is readable, the panel can be closed, Houdini isn't blocked; pressing the button unblocks input.
16. An announcement targeted at a different version → doesn't show.
17. Turn off announcements and telemetry in settings → no network requests to the feed or telemetry.
18. `pytest` — unit tests for registry/runtime/updates/announcements/sessions/settings with the network mocked.

## Deferred

- A networked studio install with runtimes pre-populated for chosen OSes.
- Choosing a working folder and adding roots from `hou.fileReferences()` (API verified, works).
- History across restarts via the protocol's own `session/load` (an optional
  capability) — what shipped instead is our own read-only
  `conversations_store.py`, replaying a saved transcript rather than
  resuming a live agent session; `session/load` itself is still unused.
- Upstreaming the panel into fx via a PR.

Done since, no longer deferred: **several different agents at once inside
one Houdini** — one process per agent id, not per Houdini process (see "One
agent process per agent id, many sessions" above).

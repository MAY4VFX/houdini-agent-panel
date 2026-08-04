<!-- hub-kit identity block (added by /project-register; do not remove) -->
# houdini-agent-panel

Guidance for AI agents working in this repository. Everything below applies to
anyone contributing, human or otherwise.

The maintainer's own workflow adds a private role file (`.dept.md`, a symlink
into a separate HQ repository) — it is deliberately not tracked here, since it
would be a dangling link in every clone. Nothing in this repository depends on
it.

<!-- Everything below is local project rules. They OVERRIDE role rules. Sections below
     this marker are NEVER touched by project-register --update. -->
<!-- /hub-kit identity block -->

## Deployment (where it lives)

- Hosting: not deployed (local development + distribution via PyPI)
- DNS: no public domain
- Rollout: `pip install houdini-agent-panel` + `python -m houdini_agent_panel install`
- Liveness check: the panel opens in Houdini, fx's `get_houdini_connection_status` reports `connected: true`

## What this is

A chat panel inside Houdini, giving an artist an ACP agent (Claude, Codex, Gemini, Grok,
Kimi, OpenCode) on top of [fxhoudinimcp](https://github.com/healkeiser/fxhoudinimcp) —
189 tools for working with the scene.

**We don't write the agent or the Houdini tools.** We write the ACP client and installer.

Full design: [`docs/design.md`](docs/design.md). Read it in full before your first
task — it records verified facts about the protocol and Houdini, no need to
rediscover them.

## Project rules

- **Qt only through `hutil.PySide`** — Houdini's shim, hands out PySide6 on H22 and PySide2 on
  20.5. Direct `import PySide6` is forbidden.
- **We never touch `hou` from the worker thread.** All scene work goes through a separate
  fx process over MCP. In the panel, `hou` is used only on the main thread and only for `$HIP`.
- **UI rule: the agent doesn't support it — the control doesn't get drawn.** Login, modes, permissions,
  slash commands, attachments all arrive as data from the agent. We never decide anything on its
  behalf and never invent anything on top of the protocol.
- **Houdini is never blocked.** Even a critical announcement only blocks the panel's
  input field.
- Don't duplicate code from `fxhoudinimcp/install.py` — reuse
  `resolve_houdini_dirs`, `desktop_config_path`, `printable_argv`.
- **Any script that imports `houdini_agent_panel` outside pytest must set
  `HAP_DATA_DIR` to a throwaway directory before that import — not after.**
  `tests/conftest.py`'s `data_dir` fixture does this automatically for every
  test; a hand-written hython/manual verification script gets none of that
  for free. A real machine's `settings.json` can have `autostart_agent=True`,
  and `AgentPanel.__init__` schedules `_boot()` on a `QTimer.singleShot(0,
  ...)` — it can fire on the very first `app.processEvents()` and launch a
  real agent subprocess against the real API, on the owner's own account,
  before you've written a single assertion. This happened for real once: a
  script without this guard left a real agent running for 30+ minutes and
  overwrote real `installed_agents`/manifest records on the developer's
  machine (see commit `b5f7932`'s message for the full account). Do it like
  this, before any `from houdini_agent_panel import ...`:
  ```python
  import os, tempfile
  os.environ["HAP_DATA_DIR"] = tempfile.mkdtemp(prefix="hap-verify-")
  ```
  `tests/e2e/run_e2e.py` is the one deliberate exception — it exists to drive
  the real installed build against real agents, and says so in its own
  docstring; it is not a template for a quick one-off check.

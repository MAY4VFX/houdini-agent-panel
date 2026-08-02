<!-- hub-kit identity block (added by /project-register; do not remove) -->
# houdini-agent-panel — a may-hub system project

You are the agent for the role named in `.dept.md` (a symlink to the role layer in HQ).
HQ: MAY4VFX/may-hub (~/Github/may-hub).

**Required before starting work** (if your runner hasn't already loaded this):
1. Read `.dept.md` at this repo's root — the rules for your active role.
2. `git -C ~/Github/may-hub pull`, then HQ's HQ.md and your role's open issues for
   this project.

At the end of the session: `/sync` (no skills — manually: work-record comments on
affected issues, board statuses, push).

@./.dept.md

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

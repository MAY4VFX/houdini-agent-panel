# houdini-agent-panel

A chat panel for an AI agent inside SideFX Houdini. Installs with one
command, the agent comes up on its own — no terminal, no ports, no config
files to edit.

> Status: v1 in progress. Design — [`docs/design.md`](docs/design.md), module contract —
> [`docs/architecture.md`](docs/architecture.md), verified facts about external APIs —
> [`docs/facts/`](docs/facts/).

## Why

To use an agent against a Houdini scene today, you need an open terminal
running `claude` and MCP set up by hand. An artist isn't going to do that.

The panel is a layer on top of
[fxhoudinimcp](https://github.com/healkeiser/fxhoudinimcp) (189 tools for
working with the scene, on top of Houdini's official `hwebserver`). We
don't write the agent or the tools — only the ACP client and installer that
tie the existing pieces together.

## Agents

From the official [ACP registry](https://github.com/agentclientprotocol/registry):
Claude Agent, Codex, Gemini CLI, Grok Build, Kimi CLI, OpenCode — plus
"Custom Agent" for everything else. Only the one you pick gets installed,
not all of them at once. Node, if needed, comes bundled with the panel.

Your own or remote models plug in as a model inside OpenCode: ACP only
works over stdio, so the agent is always local, while the model's endpoint
can be anywhere.

### What the panel installs, and what it doesn't

Two different things arrive under the same six names. Claude Agent and Codex
are ACP *adapters* — `@agentclientprotocol/claude-agent-acp` and
`codex-acp`, published by the protocol project, not by us — which speak ACP
outward and drive the vendor's own SDK inward. You do not need the Claude
Code CLI installed for the first one to work. The other four are the CLIs
themselves, which have an ACP mode built in: Gemini CLI and Grok Build come
over `npx`, Kimi and OpenCode as binaries.

The panel downloads whichever you pick, plus a portable Node if your machine
has none, into its own data folder. It writes nothing to system directories
and installs nothing globally.

**It does not configure the agent, and that is deliberate.** Providers, API
keys, and MCP servers are the agent's own files — `~/.config/opencode` for
OpenCode, `~/.claude` for Claude, and so on. The panel never reads or edits
them. Zed draws the same line for the same reason: "Claude Agent owns its own
authentication and billing." An agent you already use in a terminal will work
in the panel with no further setup, because it is the same configuration.

### Signing in

Agents authenticate themselves, and they do it in the conversation:

1. Open the panel and pick an agent. It installs and connects.
2. If it needs credentials, type `/login` and follow it. Most agents put you
   through a browser and come back signed in.
3. That's it — the panel stores nothing and asks for nothing.

Some agents advertise their sign-in methods over the protocol, and the panel
then shows a sign-in screen with buttons instead. Some — measured on a
machine where nothing had ever been configured — advertise none at all, and
simply go quiet. When that happens the panel says so and offers `/login`
rather than pretending the agent is merely busy.

OpenCode is the exception worth knowing about: it has no login flow. It
reads a provider and a key from `~/.config/opencode/opencode.json`, and until
that file names one, it will connect and answer nothing. Its own
documentation covers the format.

### MCP servers

The panel gives every agent one MCP server it does not have to configure:
the bridge to the running Houdini, which is the whole point. The agent may
also load its own — from its native config, exactly as it does in a
terminal. If a tool you expect is missing, check both.

## Install

One command, the same on macOS, Linux, and Windows:

```bash
uvx --from houdini-agent-panel python -m houdini_agent_panel install --agents opencode
```

It finds every installed Houdini, installs the panel into each one's own
Python, writes the package file, and downloads the chosen agent. Nothing
ends up in the system: `uvx` runs the installer and leaves.

Don't have `uv`? It installs with one command too, and needs no root:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh          # macOS, Linux
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"  # Windows
```

The repository has `scripts/install.sh` and `scripts/install.ps1` — they do
both steps at once and will be useful once the repository goes public and
they can be invoked via `curl … | sh`.

Restart Houdini → the panel shows up in the panels menu (Tab → Python Panels → Agent).

The installer finds every installed Houdini on the machine and installs
the panel **into each one's own Python**. That's not a whim: `pydantic`
carries a compiled core, and Houdini 20.5 has Python 3.11 inside while
Houdini 22 has 3.13, so a single shared dependency tree for both versions
is physically impossible. Details in
[`docs/architecture.md`](docs/architecture.md) §0.

Useful commands:

```bash
python -m houdini_agent_panel install --dry-run   # show the plan without changing anything
python -m houdini_agent_panel doctor              # what was found and what's broken
python -m houdini_agent_panel uninstall --purge   # remove along with the data folder
```

Install from a locally built wheel — `--find-links dist`. Fully offline —
add `--offline` and put every dependency's wheel in that same folder.

## Privacy

Telemetry is off by default and only turns on explicitly. What is and
isn't ever collected — [`docs/privacy.md`](docs/privacy.md).

## Development

```bash
uv venv --python 3.11 .venv            # 3.11 is the lowest supported version (Houdini 20.5)
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest -q
```

Tests never reach the network and never write outside `tmp_path` — that's
enforced by the autouse fixtures in `tests/conftest.py`, not by discipline.

The UI can be developed without launching Houdini. The preview uses the
panel's real Qt widgets, a fake session, and restarts itself automatically
whenever a Python file is saved:

```bash
.venv/bin/python -m houdini_agent_panel.dev_preview --watch
```

In the window you can expand custom dropdowns/tool rows, answer a
permission card, and send messages: the fake turn shows a spinner, a buddy
action, and finishes with `Worked for…`.

## License

MIT

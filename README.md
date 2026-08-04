# 🐉 houdini-agent-panel

**An AI agent inside Houdini. One command to install, no terminal to keep open.**

![The panel](docs/images/panel.png)

Ask for what you want in the scene. The agent has 189 Houdini tools through
[fxhoudinimcp](https://github.com/healkeiser/fxhoudinimcp) and works on the
`.hip` you have open — no ports to pick, no config to write.

---

## ⚡ Install

**macOS · Linux**

```sh
curl -fsSL https://raw.githubusercontent.com/MAY4VFX/houdini-agent-panel/main/scripts/install.sh | sh
```

**Windows** (PowerShell)

```powershell
irm https://raw.githubusercontent.com/MAY4VFX/houdini-agent-panel/main/scripts/install.ps1 | iex
```

**Already have `uv`?**

```sh
uvx --from houdini-agent-panel python -m houdini_agent_panel install
```

Restart Houdini → **Tab → Python Panels → Agent**. 🎉

The installer finds every Houdini on the machine and installs into each one's
own Python — 20.5 ships 3.11, 22 ships 3.13, and `pydantic`'s compiled core
means one shared tree for both is impossible.

## 🤖 Agents

Claude Agent · Codex · Gemini CLI · Grok Build · Kimi CLI · OpenCode

Pick one in the panel; only that one gets installed. Node comes bundled if
your machine hasn't got it.

Two of them — Claude Agent and Codex — are ACP *adapters* published by the
protocol project; the other four are the CLIs themselves with ACP built in.

**The panel installs agents but never configures them.** Providers, API keys
and MCP servers stay in the agent's own files, exactly as in a terminal. An
agent you already use will work here with nothing further to do.

## 🔑 Signing in

1. Pick an agent — it installs and connects.
2. Type `/login` and follow it. 🌐
3. That's it. The panel stores no credentials and asks for none.

Some agents advertise their sign-in methods and get a proper sign-in screen
instead. OpenCode has no login at all: it reads a provider and a key from
`~/.config/opencode/opencode.json`.

## ⚙️ Settings

![Settings](docs/images/settings.png)

Install and update agents, pick what starts with the panel, point it at a
studio proxy and a corporate CA. Blank proxy fields inherit whatever the
machine already exports; `localhost` is never proxied.

## 🔒 Privacy

Telemetry is **off** by default. Scenes, prompts and paths are never
collected — [`docs/privacy.md`](docs/privacy.md).

## 🛠 Development

```sh
uv venv --python 3.11 .venv
uv pip install --python .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest -q
```

Tests never touch the network and never write outside `tmp_path` — enforced
by fixtures, not by discipline.

The UI runs without Houdini, with real widgets and a fake session, and
restarts itself on every save:

```sh
.venv/bin/python -m houdini_agent_panel.dev_preview --watch
```

More commands:

```sh
python -m houdini_agent_panel install --dry-run   # show the plan, change nothing
python -m houdini_agent_panel doctor              # what was found, what's broken
python -m houdini_agent_panel uninstall --purge   # remove, data folder included
```

## 📚 Docs

[Design](docs/design.md) · [Architecture](docs/architecture.md) ·
[Verified facts about Houdini, ACP and fx](docs/facts/)

## License

MIT

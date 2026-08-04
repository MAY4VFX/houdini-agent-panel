# Routing agent traffic through a studio proxy

**TL;DR:** there is no library to adopt — "AI gateway" products (LiteLLM, Portkey, Cloudflare, Anthropic's own) are servers a studio runs, not something a panel embeds.
Every agent we ship already speaks the same dialect: `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY` plus a CA-bundle variable, so the panel's whole job is to put those variables into the child process and into `npx`.
Right now it can't: `acp.default_environment()` strips them, so a studio-wide proxy never reaches the agent — that's the actual bug to fix, and it's about 30 lines.

Date verified: 2026-08-03.

> **Update, 2026-08-04.** The premise above is no longer the whole picture.
> `shellenv.py` was added for an unrelated report (Gemini could not
> authenticate from the panel because `GEMINI_API_KEY` never reached it) and
> it captures the artist's login shell wholesale — `HTTPS_PROXY` and
> `NO_PROXY` among everything else. So the common case this document opens
> with, "the studio already exports the proxy in the profile", is closed as a
> side effect, by a change written for a different reason.
>
> What it does NOT close, and what the rest of this document is still about:
> the panel's own downloads (registry, updates, portable Node) still use
> `urllib` with no proxy handling; a corporate CA bundle still has to be
> trusted explicitly (`network.ssl_context`, `NODE_EXTRA_CA_CERTS`); `npx`
> installs still run with whatever environment we hand them; there is still
> no Network section in settings for an artist whose proxy is NOT in their
> profile; and `NO_PROXY` still needs localhost protected so the fx bridge
> is not sent through the office proxy to reach 127.0.0.1.
>
> The scope narrows, it does not vanish. Kept rather than rewritten so the
> reasoning that produced it stays legible.

## Two problems that get called "proxy"

They need different answers, and a studio usually has only one of them.

**A. Transport proxy.** The office firewall drops direct egress; everything must go out
through `proxy.studio.local:8080`, often with TLS inspection (Zscaler, Netskope, CrowdStrike,
a corporate Squid). Nothing about the LLM changes — the bytes just take a detour. This is the
common case in VFX studios, and it is solved entirely with environment variables.

**B. LLM gateway.** The studio wants the provider key to stay server-side, wants per-artist
budgets, and wants an audit log of every prompt that left the building. This is a reverse
proxy speaking the provider's API, and the client is pointed at it with a *base URL*, not a
proxy variable. This is a different product class and a different config surface.

The panel should solve A properly and stay out of B's way (B is per-agent config the artist
or the studio already writes into the agent's own config file).

## What the agents we ship actually support

Our featured six, with the distribution the ACP registry hands us today
(`registry.py:39-46`, registry fetched 2026-08-03):

| Agent | Distribution | Transport proxy | CA / TLS inspection | Gateway (base URL) |
|---|---|---|---|---|
| `claude-acp` | npx `@agentclientprotocol/claude-agent-acp` | `HTTPS_PROXY`, `HTTP_PROXY`, `NO_PROXY`, lowercase variants; **no SOCKS** | trusts the OS store by default (`CLAUDE_CODE_CERT_STORE=bundled,system`), plus `NODE_EXTRA_CA_CERTS`; mTLS via `CLAUDE_CODE_CLIENT_CERT` / `_KEY` / `_KEY_PASSPHRASE` | `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` |
| `codex-acp` | npx `@agentclientprotocol/codex-acp` | `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY` honored by the reqwest clients; individual call sites have historically missed them (openai/codex#4242, #6060) | OS store / `SSL_CERT_FILE` (reqwest + rustls-native-certs) | `model_providers.*.base_url` in `config.toml` |
| `gemini` | npx `@google/gemini-cli --acp` | `--proxy` > `general.proxy` in `settings.json` > `HTTPS_PROXY`/`HTTP_PROXY`/`ALL_PROXY`; `no_proxy` honored via `EnvHttpProxyAgent` | `NODE_EXTRA_CA_CERTS` (Node) | `GOOGLE_GEMINI_BASE_URL` / Vertex config |
| `grok-build` | npx `@xai-official/grok agent stdio` | standard proxy variables | OS trust store | `--base-url` |
| `opencode` | GitHub binary, `opencode acp` | documented as respecting the standard variables, but there are open reports of the binary bypassing them (anomalyco/opencode#6953) and no explicit config field yet (#24981) | self-signed CA problems reported (#10227) | provider config in `opencode.json` |
| `kimi` | GitHub binary, `kimi acp` | not documented; assume the standard variables and verify | not documented | base URL in config |

Two conclusions from the table. First, a single set of variables covers all six — there is
nothing agent-specific for the panel to invent. Second, coverage is uneven at the tail
(opencode, kimi), so the panel must make it *visible* which variables it exported; when a
studio hits agent-side breakage, the fix is upstream, not in our code.

Sources: [Claude Code enterprise network configuration](https://code.claude.com/docs/en/network-config),
[Claude Code LLM gateways](https://code.claude.com/docs/en/llm-gateway),
[openai/codex#4242](https://github.com/openai/codex/issues/4242),
[openai/codex#6060](https://github.com/openai/codex/issues/6060),
[gemini-cli configuration](https://github.com/google-gemini/gemini-cli/blob/main/docs/reference/configuration.md),
[gemini-cli#2649](https://github.com/google-gemini/gemini-cli/pull/2649),
[gemini-cli#4100](https://github.com/google-gemini/gemini-cli/pull/4100),
[opencode#6953](https://github.com/anomalyco/opencode/issues/6953),
[opencode#24981](https://github.com/anomalyco/opencode/issues/24981),
[opencode#10227](https://github.com/anomalyco/opencode/issues/10227).

## Why it doesn't work in the panel today

`client.py:364` builds the child environment as `dict(acp.default_environment())` plus
`spec.env`. `default_environment()` (acp SDK, `acp/transports.py:13-44`) is an allow-list:

```python
["HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER"]          # POSIX
["APPDATA", "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "PATH",  # Windows
 "PATHEXT", "PROCESSOR_ARCHITECTURE", "SYSTEMDRIVE", "SYSTEMROOT",
 "TEMP", "USERNAME", "USERPROFILE"]
```

`HTTPS_PROXY` is not on either list. So even the studio that already exports the proxy
globally — in `houdini.env`, in a wrapper script, machine-wide — gets an agent that ignores
it and dies on connect. The panel silently un-configures the machine. This is a bug
independent of any settings UI, and it is the reason a proxy field is cheap: the plumbing
has to be written anyway.

`spec.env` is the whole mechanism needed — `LaunchSpec.env` is already "added to the process
environment, not a replacement for it" (`runtime.py:40`) and already carries a doctored
`PATH` for the bundled Node (`runtime.py:295-297`).

The panel's *own* traffic (registry, PyPI, nodejs.org, agent archives, announcements) is a
separate path: `network.py` uses `urllib`, whose default opener includes a `ProxyHandler`
built from `getproxies()`, so it already honors `*_proxy` env vars and macOS system proxy
settings. What it does *not* survive is TLS inspection — `ssl_context()` pins the `certifi`
bundle and a MITM certificate fails verification. There is already an escape hatch for that,
`HAP_CA_BUNDLE` (`network.py:31`), but it's an env var nobody knows about and it is not wired
to any setting.

## Ready-made solutions: what exists, and what is actually adoptable

Surveyed because the question was "is there something ready" — the honest answer is that the
mature products all live on the studio's side of the wire, not in our process.

**Self-hosted AI gateways** — [LiteLLM](https://docs.litellm.ai/) (MIT, OpenAI-compatible
front for 100+ providers, virtual keys, per-team budgets, Anthropic passthrough at
`/anthropic`), [Portkey Gateway](https://portkey.ai/) (Apache-2.0 gateway, proprietary
managed platform), Envoy AI Gateway, TrueFoundry. These solve problem B, and a studio that
already runs one needs nothing from us beyond the ability to set `ANTHROPIC_BASE_URL` — which
`CustomAgent.env` and the agent's own config already allow.

**Managed gateways** — Cloudflare AI Gateway, OpenRouter, Vercel. Zero ops, but traffic
leaves the studio's boundary, which is usually the exact thing the security team is trying to
prevent. Wrong tool for a closed-network studio.

**Anthropic's own** — [Claude apps gateway](https://code.claude.com/docs/en/claude-apps-gateway),
self-hosted, SSO sign-in, OTLP telemetry. Claude-only, so it can't be the panel's answer for
six agents.

**Classic forward proxies** — Squid, and for NTLM/Kerberos-authenticated corporate proxies
the local shims [px](https://github.com/genotrance/px) and cntlm. Relevant because none of
the agents implement NTLM or Kerberos; Anthropic's own docs punt on this and suggest a
gateway instead. If a studio has an NTLM proxy, the supported answer is "run px locally and
point the panel at `http://127.0.0.1:3128`" — which the same settings field covers.

Nothing here is a dependency to add. What we ship is the last 30 lines: getting the studio's
choice into six child processes and one `npx`.

## Proposed shape for the panel

Settings (`settings.py`, new fields, all empty by default — an empty value must mean "don't
set the variable", never "set it to empty"):

```python
proxy_url: str = ""        # http://user:pass@proxy.studio.local:8080
no_proxy: str = ""         # extra bypass entries, appended to our defaults
ca_bundle: str = ""        # PEM path for a TLS-inspecting proxy
```

Rules worth writing down before the code:

1. **Export the full family.** `HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY` and their lowercase
   twins. Agents disagree about which they read (Claude Code prefers `https_proxy` first;
   Gemini and Codex also read `ALL_PROXY`), and setting all six is cheaper than tracking it.
2. **Never proxy localhost.** `NO_PROXY` must always contain `localhost,127.0.0.1,::1` before
   the artist's additions. The fx MCP server is on localhost (`scene.py`), and opencode's TUI
   talks to a local HTTP server — routing those through a corporate proxy is a hang, not an
   error. Claude Code accepts space- or comma-separated; comma is safe for all six.
3. **CA bundle goes out under several names.** `NODE_EXTRA_CA_CERTS` for the three npx/Node
   agents, `SSL_CERT_FILE` for the Rust/Go binaries, and the same path into `HAP_CA_BUNDLE`
   so `network.py:55` picks it up for the panel's own downloads.
4. **`npx` needs it too.** Installing an agent runs npm, which reads `HTTPS_PROXY`/`NO_PROXY`
   from the environment and `cafile` from config — exporting the same variables for the
   install subprocess covers it, with `NODE_EXTRA_CA_CERTS` as the fallback for the runtime
   half. ([npm config](https://docs.npmjs.com/cli/v11/using-npm/config))
5. **Inherit before overriding.** If the artist leaves the field blank but the machine already
   exports `HTTPS_PROXY`, pass the machine's value through. Today it's dropped; that alone
   fixes the studios that are already correctly configured.
6. **Don't invent an agent-side UI.** Per the project rule, the panel doesn't draw controls
   the agent doesn't support. A proxy is *transport* — it belongs to the panel, not to the
   agent's capability set, so a settings field is legitimate. A "gateway base URL" field is
   not: that's per-agent, and `CustomAgent.env` already covers it.
7. **Make it visible in diagnostics.** `settings.diagnostics()` should print
   `proxy: <host:port or —>` and `ca bundle: <path or —>` with credentials stripped. Without
   it, "the agent won't start" behind a proxy is undebuggable from a bug report.

Known limits to state in the UI rather than paper over: no SOCKS for Claude Code; no NTLM or
Kerberos anywhere (point at px); and a password typed into the proxy URL lands in
`settings.json` in plaintext, so the field should say so and prefer an unauthenticated or
IP-allowlisted proxy.

## Open questions

- Kimi CLI proxy support is undocumented — needs an actual test behind a proxy before we
  claim the panel supports it.
- opencode #6953 (binary bypassing the env proxy) needs a version check against the
  `1.18.11` the registry currently pins; if it's still live, the panel should say so instead
  of letting the artist debug a silent direct connection.
- Whether Houdini's own `houdini.env` is a better distribution channel for studio-wide
  defaults than the panel's settings file — a pipeline TD sets it once for everyone, and
  rule 5 would make it just work.

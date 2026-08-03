# Studio proxy support — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: use `superpowers:subagent-driven-development`
> (recommended) or `superpowers:executing-plans` to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** an artist behind a corporate firewall types a proxy address (and, if the proxy
inspects TLS, a CA bundle path) into the panel's settings, and every agent, every `npx`
install, and every download the panel makes goes through it.

**Architecture:** one pure module, `proxy.py`, turns `Settings` into a dict of environment
variables. `runtime.py` merges that dict into `LaunchSpec.env`, which already flows into the
spawned agent (and therefore into the `npx` that fetches its package). `network.py` gets the
same values through an explicit `configure()` call, because `urllib` can't see the settings
file. Nothing mutates Houdini's own `os.environ`.

**Tech stack:** Python 3.11/3.13 (Houdini's), stdlib `urllib`/`ssl`, PySide via `hutil.PySide`,
pytest.

## Global constraints

- Qt only through `ui/qt.py` (Houdini's `hutil.PySide` shim). Direct `import PySide6` is forbidden.
- No `hou` outside the main thread; `proxy.py` must not import `hou`, Qt, or `network`.
- An empty settings field means "don't set this variable" — never "set it to empty string".
- Houdini is never blocked, and a proxy change must never require restarting Houdini.
- Repo language is English everywhere except user-facing UI strings, which are Russian.
- A proxy password may be stored in `settings.json` in plaintext (decided 2026-08-03), but it
  must never appear in diagnostics, the logbook, or any error message.

## Background

Read `docs/2026-08-03-proxy-support.md` first. The two facts that drive every task:

1. `client.py:364` builds the agent's environment from `acp.default_environment()`, an
   allow-list that does not include any proxy variable (`acp/transports.py:13-30`). A studio
   that exports `HTTPS_PROXY` machine-wide still gets an agent that ignores it.
2. `network.py:36` pins the `certifi` CA bundle, so a TLS-inspecting proxy breaks the panel's
   own downloads. The `HAP_CA_BUNDLE` escape hatch exists but is wired to nothing.

## File structure

- **Create** `python/houdini_agent_panel/proxy.py` — pure settings→env translation. No I/O, no
  Qt, no `hou`. The one place that knows which variable names exist.
- **Create** `tests/test_proxy.py`.
- **Modify** `python/houdini_agent_panel/settings.py` — three fields, plus diagnostics lines.
- **Modify** `python/houdini_agent_panel/runtime.py:380-412` — merge proxy env into both
  `launch_spec` and `custom_launch_spec`.
- **Modify** `python/houdini_agent_panel/network.py` — `configure()`, a proxy-aware opener,
  and cache invalidation for `ssl_context()`.
- **Modify** `python/houdini_agent_panel/ui/settings_view.py` — a "Сеть" section.
- **Modify** `python/houdini_agent_panel/ui/panel.py` — apply on change, offer an agent restart.
- **Modify** `docs/design.md`, `README.md` — document the fields and their limits.

---

### Task 1: `proxy.py` and the settings fields

**Files:**
- Create: `python/houdini_agent_panel/proxy.py`
- Create: `tests/test_proxy.py`
- Modify: `python/houdini_agent_panel/settings.py:51-63` (fields), `:159-201` (diagnostics)

**Interfaces:**
- Consumes: `settings.Settings`.
- Produces:
  - `proxy.effective_proxy(settings, environ=None) -> str`
  - `proxy.effective_ca_bundle(settings, environ=None) -> str`
  - `proxy.no_proxy_value(settings, environ=None) -> str`
  - `proxy.child_env(settings, environ=None) -> dict[str, str]`
  - `proxy.sanitize(url: str) -> str`
  - `Settings.proxy_url`, `Settings.no_proxy`, `Settings.ca_bundle` — all `str`, default `""`

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_proxy.py
from houdini_agent_panel import proxy
from houdini_agent_panel.settings import Settings


def test_no_proxy_anywhere_sets_nothing():
    assert proxy.child_env(Settings(), environ={}) == {}


def test_settings_proxy_wins_over_environment():
    settings = Settings(proxy_url="http://studio:8080")
    env = proxy.child_env(settings, environ={"HTTPS_PROXY": "http://stale:3128"})
    assert env["HTTPS_PROXY"] == "http://studio:8080"
    assert env["https_proxy"] == "http://studio:8080"
    assert env["ALL_PROXY"] == "http://studio:8080"


def test_machine_proxy_is_inherited_when_the_field_is_empty():
    # The bug this whole feature starts from: a correctly configured machine
    # must not be un-configured by the panel.
    env = proxy.child_env(Settings(), environ={"HTTPS_PROXY": "http://studio:8080"})
    assert env["HTTPS_PROXY"] == "http://studio:8080"


def test_localhost_always_bypasses_the_proxy():
    # fx MCP and opencode's own TUI server live on localhost; proxying them
    # is a hang, not an error.
    env = proxy.child_env(Settings(proxy_url="http://studio:8080"), environ={})
    parts = env["NO_PROXY"].split(",")
    assert parts[:3] == ["localhost", "127.0.0.1", "::1"]
    assert env["no_proxy"] == env["NO_PROXY"]


def test_no_proxy_merges_machine_and_settings_without_duplicates():
    settings = Settings(proxy_url="http://studio:8080", no_proxy="render.local, localhost")
    env = proxy.child_env(settings, environ={"NO_PROXY": "vault.local"})
    assert env["NO_PROXY"] == "localhost,127.0.0.1,::1,vault.local,render.local"


def test_ca_bundle_goes_out_under_every_name_a_runtime_reads():
    settings = Settings(ca_bundle="/etc/ssl/studio.pem")
    env = proxy.child_env(settings, environ={})
    for name in ("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "HAP_CA_BUNDLE"):
        assert env[name] == "/etc/ssl/studio.pem"


def test_empty_fields_never_produce_empty_variables():
    # An empty NODE_EXTRA_CA_CERTS is worse than an absent one: Node treats it
    # as a path and fails.
    assert proxy.child_env(Settings(), environ={}) == {}


def test_sanitize_strips_the_password():
    assert proxy.sanitize("http://bob:hunter2@studio:8080") == "http://bob:***@studio:8080"
    assert proxy.sanitize("http://studio:8080") == "http://studio:8080"
    assert proxy.sanitize("not a url") == "not a url"
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `.venv/bin/pytest tests/test_proxy.py -v`
Expected: `ModuleNotFoundError: No module named 'houdini_agent_panel.proxy'`

- [ ] **Step 3: Write `proxy.py`**

```python
"""Settings to environment variables for a studio proxy.

One module owns the variable names because there are eighteen of them and
six agents that each read a different subset. Everything here is pure: a
`Settings` in, a dict out, so a test can check the whole matrix without a
process, a network, or Houdini.
"""

from __future__ import annotations

import os
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

#: Every spelling an agent might read. Claude Code prefers `https_proxy`
#: over `HTTPS_PROXY`; Codex and Gemini also read `ALL_PROXY`. Setting all
#: six is cheaper than tracking who reads which.
PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")

#: `NODE_EXTRA_CA_CERTS` for the npx agents, `SSL_CERT_FILE` for the Rust and
#: Go binaries, `REQUESTS_CA_BUNDLE` for anything Python they shell out to,
#: and `HAP_CA_BUNDLE` so `network.py` sees the same file.
CA_VARS = ("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "HAP_CA_BUNDLE")

#: Never proxied. The fx MCP server is on localhost (`scene.py`) and
#: opencode's TUI talks to its own local HTTP server: sending those through
#: a corporate proxy hangs instead of failing.
LOCAL_BYPASS = ("localhost", "127.0.0.1", "::1")


def _environ(environ: "Mapping[str, str] | None") -> "Mapping[str, str]":
    return os.environ if environ is None else environ


def effective_proxy(settings, environ: "Mapping[str, str] | None" = None) -> str:
    """The proxy in force: the artist's field, else whatever the machine says.

    The inheritance half is the important one. A studio that already exports
    `HTTPS_PROXY` machine-wide is correctly configured, and the panel's job
    is to pass that through, not to blank it.
    """
    typed = (getattr(settings, "proxy_url", "") or "").strip()
    if typed:
        return typed
    env = _environ(environ)
    for name in PROXY_VARS:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return ""


def effective_ca_bundle(settings, environ: "Mapping[str, str] | None" = None) -> str:
    typed = (getattr(settings, "ca_bundle", "") or "").strip()
    if typed:
        return typed
    env = _environ(environ)
    for name in CA_VARS:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return ""


def _split(text: str) -> list[str]:
    """Split a NO_PROXY value. Comma or whitespace, both are in the wild."""
    return [item.strip() for item in text.replace(" ", ",").split(",") if item.strip()]


def no_proxy_value(settings, environ: "Mapping[str, str] | None" = None) -> str:
    env = _environ(environ)
    parts: list[str] = list(LOCAL_BYPASS)
    inherited = env.get("NO_PROXY") or env.get("no_proxy") or ""
    for chunk in (inherited, getattr(settings, "no_proxy", "") or ""):
        for item in _split(chunk):
            if item not in parts:
                parts.append(item)
    return ",".join(parts)


def child_env(settings, environ: "Mapping[str, str] | None" = None) -> dict[str, str]:
    """Environment additions for a spawned agent, npx, or npm.

    Empty in, empty out: a machine with no proxy and no custom CA gets no
    variables at all, because an empty `NODE_EXTRA_CA_CERTS` is a path Node
    tries to read and fails on.
    """
    env: dict[str, str] = {}

    address = effective_proxy(settings, environ)
    if address:
        for name in PROXY_VARS:
            env[name] = address
        bypass = no_proxy_value(settings, environ)
        env["NO_PROXY"] = bypass
        env["no_proxy"] = bypass

    bundle = effective_ca_bundle(settings, environ)
    if bundle:
        for name in CA_VARS:
            env[name] = bundle

    return env


def sanitize(url: str) -> str:
    """A proxy URL safe to print. The password becomes `***`.

    Diagnostics get pasted into bug reports and the logbook goes to disk;
    neither is a place for the studio's proxy password.
    """
    try:
        parts = urlsplit(url)
        if not parts.password:
            return url
        userinfo = f"{parts.username or ''}:***"
        netloc = f"{userinfo}@{parts.hostname or ''}"
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except ValueError:
        return url


__all__ = [
    "CA_VARS",
    "LOCAL_BYPASS",
    "PROXY_VARS",
    "child_env",
    "effective_ca_bundle",
    "effective_proxy",
    "no_proxy_value",
    "sanitize",
]
```

- [ ] **Step 4: Add the three settings fields**

In `settings.py`, inside `@dataclass class Settings`, after `whisper_endpoint`:

```python
    #: Studio proxy, e.g. "http://proxy.studio.local:8080". Empty means
    #: "whatever the machine already exports" — see `proxy.effective_proxy`.
    proxy_url: str = ""
    #: Extra bypass entries. `localhost`/`127.0.0.1`/`::1` are always added.
    no_proxy: str = ""
    #: PEM bundle for a TLS-inspecting proxy.
    ca_bundle: str = ""
```

No change to `from_dict` is needed: the `isinstance(getattr(settings, name), str)` branch
at `settings.py:118` already handles plain string fields.

- [ ] **Step 5: Add the diagnostics lines**

In `settings.diagnostics()`, before the `telemetry:` line:

```python
    from . import proxy as proxy_module

    address = proxy_module.effective_proxy(settings)
    lines.append(f"proxy: {proxy_module.sanitize(address) if address else '—'}")
    lines.append(f"ca bundle: {proxy_module.effective_ca_bundle(settings) or '—'}")
```

- [ ] **Step 6: Test that diagnostics never leaks the password**

```python
# tests/test_proxy.py
from houdini_agent_panel import settings as settings_module


def test_diagnostics_hides_the_proxy_password():
    text = settings_module.diagnostics(Settings(proxy_url="http://bob:hunter2@studio:8080"))
    assert "hunter2" not in text
    assert "proxy: http://bob:***@studio:8080" in text
```

- [ ] **Step 7: Run the whole file**

Run: `.venv/bin/pytest tests/test_proxy.py -v`
Expected: PASS, every test.

- [ ] **Step 8: Commit**

```bash
git add python/houdini_agent_panel/proxy.py python/houdini_agent_panel/settings.py tests/test_proxy.py
git commit -m "Proxy settings and the module that turns them into variables"
```

---

### Task 2: the agent (and its npx) actually goes through the proxy

**Files:**
- Modify: `python/houdini_agent_panel/runtime.py:380-412`
- Test: `tests/test_runtime.py` (add to the existing file)

**Interfaces:**
- Consumes: `proxy.child_env`, `runtime.LaunchSpec`.
- Produces: `launch_spec(entry, settings=None)` and `custom_launch_spec(agent, settings=None)` —
  both grow one optional keyword argument, defaulting to `settings.load()`, so existing
  callers keep working.

This is the task that fixes the actual bug. `LaunchSpec.env` is already merged over
`acp.default_environment()` at `client.py:364-365`, and the npx agents run `npx` at launch
(`runtime._npx_launch_spec`), so the same dict covers both npm's package download and the
agent's own requests. One change, two problems.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_runtime.py
from houdini_agent_panel import runtime
from houdini_agent_panel.settings import CustomAgent, Settings


def test_custom_agent_launch_carries_the_proxy(monkeypatch):
    monkeypatch.setattr(os, "environ", {})
    agent = CustomAgent(id="mine", name="Mine", command="/bin/echo")
    spec = runtime.custom_launch_spec(agent, settings=Settings(proxy_url="http://studio:8080"))
    assert spec.env["HTTPS_PROXY"] == "http://studio:8080"
    assert "localhost" in spec.env["NO_PROXY"]


def test_agent_env_wins_over_the_studio_proxy(monkeypatch):
    # An artist who set HTTPS_PROXY on one custom agent meant it for that
    # agent. The global setting is a default, not an override.
    monkeypatch.setattr(os, "environ", {})
    agent = CustomAgent(
        id="mine", name="Mine", command="/bin/echo", env={"HTTPS_PROXY": "http://mine:9000"}
    )
    spec = runtime.custom_launch_spec(agent, settings=Settings(proxy_url="http://studio:8080"))
    assert spec.env["HTTPS_PROXY"] == "http://mine:9000"


def test_no_proxy_configured_adds_no_variables(monkeypatch):
    monkeypatch.setattr(os, "environ", {})
    agent = CustomAgent(id="mine", name="Mine", command="/bin/echo")
    spec = runtime.custom_launch_spec(agent, settings=Settings())
    assert spec.env == {}
```

- [ ] **Step 2: Run them and watch them fail**

Run: `.venv/bin/pytest tests/test_runtime.py -k proxy -v`
Expected: FAIL — `custom_launch_spec() got an unexpected keyword argument 'settings'`

- [ ] **Step 3: Implement**

In `runtime.py`, add near the top of the launch section:

```python
def _with_proxy(env: dict[str, str], settings_obj) -> dict[str, str]:
    """Studio proxy underneath, the agent's own env on top.

    Order matters: a per-agent `HTTPS_PROXY` is a deliberate choice about
    that agent, and the panel-wide setting is only a default for agents
    that said nothing.
    """
    from . import proxy as proxy_module
    from . import settings as settings_module

    resolved = settings_module.load() if settings_obj is None else settings_obj
    merged = proxy_module.child_env(resolved)
    merged.update(env)
    return merged
```

Then in `launch_spec(entry)` (`runtime.py:380`) and `custom_launch_spec(agent)`
(`runtime.py:410`), add the `settings=None` keyword and wrap the env at every `return
LaunchSpec(...)` — `_npx_launch_spec` and `_binary_launch_spec` keep returning bare specs,
and the two public entry points do the merging:

```python
def launch_spec(entry: AgentEntry, *, settings=None) -> LaunchSpec:
    ...
    spec = ...  # unchanged body
    return LaunchSpec(command=spec.command, args=spec.args, env=_with_proxy(spec.env, settings))


def custom_launch_spec(agent: CustomAgent, *, settings=None) -> LaunchSpec:
    return LaunchSpec(
        command=agent.command,
        args=list(agent.args),
        env=_with_proxy(dict(agent.env), settings),
    )
```

- [ ] **Step 4: Run the tests**

Run: `.venv/bin/pytest tests/test_runtime.py -v`
Expected: PASS, including the pre-existing tests.

- [ ] **Step 5: Log what was applied**

In `client.do_start` (`client.py:364`), after the env is assembled, add one line so a bug
report shows whether the proxy reached the child:

```python
            from . import proxy as proxy_module

            address = env.get("HTTPS_PROXY", "")
            log.info("agent proxy: %s", proxy_module.sanitize(address) if address else "none")
```

- [ ] **Step 6: Run the full suite and commit**

```bash
.venv/bin/pytest -q
git add python/houdini_agent_panel/runtime.py python/houdini_agent_panel/client.py tests/test_runtime.py
git commit -m "Agent and its npx go through the studio proxy"
```

---

### Task 3: the panel's own downloads go through the proxy

**Files:**
- Modify: `python/houdini_agent_panel/network.py:31-66`, `:82-127`
- Test: `tests/test_network.py` (add to the existing file)

**Interfaces:**
- Consumes: `proxy.effective_proxy`, `proxy.effective_ca_bundle`.
- Produces: `network.configure(*, proxy: str = "", ca_bundle: str = "") -> None` and
  `network.opener() -> urllib.request.OpenerDirector`.

`urllib` already honors `*_proxy` environment variables through the default `ProxyHandler`,
so a machine-configured proxy works today. What doesn't is the *settings* value, which
`urllib` can't see, and the `certifi` pin, which a TLS-inspecting proxy fails. Both are fixed
by building the opener ourselves.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_network.py
import urllib.request

from houdini_agent_panel import network


def test_configure_puts_the_proxy_in_the_opener():
    network.configure(proxy="http://studio:8080")
    try:
        handlers = network.opener().handlers
        proxies = [h for h in handlers if isinstance(h, urllib.request.ProxyHandler)]
        assert proxies and proxies[0].proxies["https"] == "http://studio:8080"
    finally:
        network.configure()


def test_configure_resets_the_cached_ssl_context(tmp_path):
    # The context is a module global; without invalidation a CA change would
    # need a Houdini restart, and it must not.
    first = network.ssl_context()
    bundle = tmp_path / "ca.pem"
    bundle.write_text("")
    network.configure(ca_bundle=str(bundle))
    try:
        assert network.ssl_context() is not first
    finally:
        network.configure()


def test_configure_with_nothing_falls_back_to_the_environment():
    network.configure()
    handlers = network.opener().handlers
    assert any(isinstance(h, urllib.request.ProxyHandler) for h in handlers)
```

- [ ] **Step 2: Run and watch them fail**

Run: `.venv/bin/pytest tests/test_network.py -v`
Expected: FAIL — `module 'houdini_agent_panel.network' has no attribute 'configure'`

- [ ] **Step 3: Implement**

Replace the module-global caching in `network.py`:

```python
_ssl_context: ssl.SSLContext | None = None
_opener: urllib.request.OpenerDirector | None = None
_proxy: str = ""
_ca_bundle: str = ""


def configure(*, proxy: str = "", ca_bundle: str = "") -> None:
    """Point the panel's own requests at the studio proxy.

    Called on startup and whenever settings change. Both caches are dropped,
    which is what keeps a proxy change from needing a Houdini restart.
    """
    global _proxy, _ca_bundle, _ssl_context, _opener
    _proxy = proxy or ""
    _ca_bundle = ca_bundle or ""
    _ssl_context = None
    _opener = None


def opener() -> urllib.request.OpenerDirector:
    global _opener
    if _opener is not None:
        return _opener
    # No configured proxy means an empty ProxyHandler, which reads the
    # environment and the macOS system settings on its own — exactly what
    # urlopen did before this existed.
    proxies = {scheme: _proxy for scheme in ("http", "https")} if _proxy else None
    _opener = urllib.request.build_opener(
        urllib.request.ProxyHandler(proxies),
        urllib.request.HTTPSHandler(context=ssl_context()),
    )
    return _opener
```

and make `ssl_context()` prefer the configured bundle over `HAP_CA_BUNDLE`:

```python
    override = _ca_bundle or os.environ.get(CA_BUNDLE_ENV) or ""
    if override and os.path.exists(override):
        _ssl_context = ssl.create_default_context(cafile=override)
        return _ssl_context
```

Then in `urlopen_fetch` and `stream_fetch`, swap
`urllib.request.urlopen(request, timeout=timeout, context=ssl_context())` for
`opener().open(request, timeout=timeout)` — the context now lives in the opener's HTTPS
handler.

- [ ] **Step 4: Call it on startup**

In `ui/panel.py`, where the panel loads settings on construction, add:

```python
        network.configure(
            proxy=proxy_mod.effective_proxy(self._settings),
            ca_bundle=proxy_mod.effective_ca_bundle(self._settings),
        )
```

- [ ] **Step 5: Run the tests and commit**

```bash
.venv/bin/pytest tests/test_network.py -v
git add python/houdini_agent_panel/network.py python/houdini_agent_panel/ui/panel.py tests/test_network.py
git commit -m "Panel downloads honour the studio proxy and its CA"
```

---

### Task 4: the settings section, and an honest restart notice

**Files:**
- Modify: `python/houdini_agent_panel/ui/settings_view.py:150-205`, `:258-299`
- Modify: `python/houdini_agent_panel/ui/panel.py:1100-1115`
- Test: `tests/test_ui_settings.py`

**Interfaces:**
- Consumes: `Settings.proxy_url`, `Settings.no_proxy`, `Settings.ca_bundle`.
- Produces: `SettingsView.proxy_changed` — a `Signal()`, emitted only when one of the three
  network fields changed, so the panel can offer an agent restart without doing it on every
  unrelated checkbox.

**Houdini does not need restarting, and the UI must not claim otherwise.** The agent reads
its environment once, at spawn — so a proxy change takes effect on the next agent start, and
the panel can do that itself: `panel.py:1130-1148` already stops the client and starts the
agent while keeping the conversation. The `network.configure()` call from Task 3 drops the
SSL and opener caches in-process. Nothing in this feature outlives a `_start_agent()`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ui_settings.py
from houdini_agent_panel import settings as settings_module
from houdini_agent_panel.ui.settings_view import SettingsView


def test_typing_a_proxy_saves_it_and_announces_a_restart(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(settings_module.paths, "settings_path", lambda: tmp_path / "s.json")
    view = SettingsView()
    qtbot.addWidget(view)
    with qtbot.waitSignal(view.proxy_changed):
        view._proxy_edit.setText("http://studio:8080")
    assert settings_module.load().proxy_url == "http://studio:8080"


def test_an_unrelated_field_does_not_announce_a_restart(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(settings_module.paths, "settings_path", lambda: tmp_path / "s.json")
    view = SettingsView()
    qtbot.addWidget(view)
    with qtbot.assertNotEmitted(view.proxy_changed):
        view._telemetry_checkbox.setChecked(True)
```

- [ ] **Step 2: Run and watch them fail**

Run: `.venv/bin/pytest tests/test_ui_settings.py -v`
Expected: FAIL — `SettingsView` has no attribute `proxy_changed`

- [ ] **Step 3: Add the section**

In `SettingsView.__init__`, next to the other field widgets:

```python
        self._proxy_edit = QtWidgets.QLineEdit()
        self._proxy_edit.setPlaceholderText("http://proxy.studio.local:8080")
        self._proxy_edit.textChanged.connect(self._on_field_changed)

        self._no_proxy_edit = QtWidgets.QLineEdit()
        self._no_proxy_edit.setPlaceholderText("render.local, .studio.local")
        self._no_proxy_edit.textChanged.connect(self._on_field_changed)

        self._ca_bundle_edit = QtWidgets.QLineEdit()
        self._ca_bundle_edit.setPlaceholderText("/path/to/studio-ca.pem")
        self._ca_bundle_edit.textChanged.connect(self._on_field_changed)

        proxy_hint = QtWidgets.QLabel(
            "Пусто — панель возьмёт настройки прокси у самой машины.\n"
            "Пароль в адресе сохраняется в settings.json открытым текстом:\n"
            "лучше прокси без авторизации или с доступом по IP.\n"
            "localhost никогда не идёт через прокси."
        )
        proxy_hint.setWordWrap(True)
        proxy_hint.setEnabled(False)

        network_section = _Section("Сеть", expanded=False)
        network_section.add_row("Прокси", self._proxy_edit)
        network_section.add_row("Без прокси", self._no_proxy_edit)
        network_section.add_row("Корневой сертификат", self._ca_bundle_edit)
        network_section.add_row(proxy_hint)
```

Add `network_section` to the `for section in (...)` tuple, between `voice_section` and
`privacy_section`. Declare `proxy_changed = Signal()` next to `changed` and `closed`.

- [ ] **Step 4: Save the fields and emit the signal**

In `reload()`:

```python
            self._proxy_edit.setText(current.proxy_url)
            self._no_proxy_edit.setText(current.no_proxy)
            self._ca_bundle_edit.setText(current.ca_bundle)
```

In `_on_field_changed`, before `settings_module.save(current)`:

```python
        before = (current.proxy_url, current.no_proxy, current.ca_bundle)
        current.proxy_url = self._proxy_edit.text().strip()
        current.no_proxy = self._no_proxy_edit.text().strip()
        current.ca_bundle = self._ca_bundle_edit.text().strip()
        network_changed = before != (current.proxy_url, current.no_proxy, current.ca_bundle)
```

and after `self.changed.emit()`:

```python
        if network_changed:
            self.proxy_changed.emit()
```

- [ ] **Step 5: Apply it in the panel**

Wire `self._settings_view.proxy_changed` to a new handler in `panel.py`:

```python
    def _on_proxy_changed(self) -> None:
        """A proxy change reaches the panel now and the agent on its next start.

        Houdini is not involved: the agent reads its environment once, when we
        spawn it, and we can spawn it again ourselves.
        """
        self._settings = settings_mod.load()
        network.configure(
            proxy=proxy_mod.effective_proxy(self._settings),
            ca_bundle=proxy_mod.effective_ca_bundle(self._settings),
        )
        client = shared_client()
        if not client.is_running():
            return
        self._announce(
            "Настройки сети изменились. Агент подхватит их после перезапуска.",
            action=("Перезапустить агента", self._restart_agent),
        )
```

Reuse whatever inline-notice mechanism the panel already has for this (`ui/announcement.py`);
`_restart_agent` is `client.stop()` followed by `self._start_agent(self._settings.default_agent)`,
which is `_on_agent_chosen` minus the id change — factor the shared tail out of
`_on_agent_chosen` rather than duplicating it.

- [ ] **Step 6: Run the tests and commit**

```bash
.venv/bin/pytest tests/test_ui_settings.py tests/test_ui_panel.py -v
git add python/houdini_agent_panel/ui/settings_view.py python/houdini_agent_panel/ui/panel.py tests/test_ui_settings.py
git commit -m "Network settings section, applied without restarting Houdini"
```

---

### Task 5: verify the two unknowns, and write down what we support

**Files:**
- Create: `docs/facts/proxy.md`
- Modify: `docs/design.md`, `README.md`

Four of the six agents document proxy support. Two do not, and the panel must not claim
support it hasn't seen work.

- [ ] **Step 1: Stand up a local proxy**

```bash
uvx mitmproxy --listen-port 8080 --set block_global=false
```

- [ ] **Step 2: Test each agent behind it**

For each of `claude-acp`, `codex-acp`, `gemini`, `grok-build`, `opencode`, `kimi`: set the
proxy in the panel's settings, restart the agent, send one message, and check mitmproxy's
flow list for the agent's API host. Record pass/fail per agent, with the version tested.

Watch for the two known failure modes: opencode connecting straight to `:443` while the
proxy sits idle ([#6953](https://github.com/anomalyco/opencode/issues/6953)), and Codex
login going direct while normal requests are proxied
([#4242](https://github.com/openai/codex/issues/4242)).

- [ ] **Step 3: Repeat with TLS inspection on**

Run mitmproxy with its own CA, point `ca_bundle` at `~/.mitmproxy/mitmproxy-ca-cert.pem`, and
repeat. This is what catches an agent that ignores `NODE_EXTRA_CA_CERTS`.

- [ ] **Step 4: Write `docs/facts/proxy.md`**

Same shape as the other files in `docs/facts/`: what was tested, on which versions, on which
date, what worked and what didn't. This is the file that stops the next person re-deriving it.

- [ ] **Step 5: Document the fields for users**

A short section in `README.md` (the three fields, and that localhost is never proxied) and a
paragraph in `docs/design.md` recording the design decision: the panel handles *transport*
proxying only; a per-agent LLM gateway base URL stays in the agent's own config and in
`CustomAgent.env`, because the panel doesn't draw controls for things the protocol doesn't
own.

- [ ] **Step 6: Commit**

```bash
git add docs/facts/proxy.md docs/design.md README.md
git commit -m "Verified proxy behaviour per agent, documented what we support"
```

---

## Out of scope

Stated so nobody adds them mid-flight:

- **SOCKS.** Claude Code doesn't support it; a mixed answer is worse than a clear no.
- **NTLM / Kerberos.** No agent implements either. The answer for such a studio is a local
  shim (px, cntlm) and `http://127.0.0.1:3128` in the same field.
- **A gateway base URL field.** Per-agent, not panel-wide; `CustomAgent.env` covers it.
- **A domain allow-list.** That's the studio's proxy admin's job, not the panel's.
- **Keychain storage for the proxy password.** Decided against on 2026-08-03: three platforms
  and a new dependency, for a secret the agents themselves keep in plaintext env vars anyway.

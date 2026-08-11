"""Tests for `scene.py` — no Houdini involved: `hou`/`fxhoudinimcp_server` are
replaced with fake modules in `sys.modules`, exactly how the lazy import was
designed to work.
"""

from __future__ import annotations

import sys
import types

import pytest

from houdini_agent_panel import scene


@pytest.fixture(autouse=True)
def _clean_fake_modules():
    """In case the previous test forgot to clean up after itself."""
    for name in ("hou", "fxhoudinimcp_server", "fxhoudinimcp_server.startup"):
        sys.modules.pop(name, None)
    yield
    for name in ("hou", "fxhoudinimcp_server", "fxhoudinimcp_server.startup"):
        sys.modules.pop(name, None)


def _install_fake_startup(*, running: bool, port: int, starting: bool = False) -> None:
    package = types.ModuleType("fxhoudinimcp_server")
    startup = types.ModuleType("fxhoudinimcp_server.startup")
    startup.is_running = lambda: running
    startup.get_port = lambda: port
    startup.is_starting = lambda: starting
    package.startup = startup
    sys.modules["fxhoudinimcp_server"] = package
    sys.modules["fxhoudinimcp_server.startup"] = startup


def _install_fake_hou(*, is_new_file: bool, path: str) -> None:
    hou = types.ModuleType("hou")
    hip_file = types.SimpleNamespace(isNewFile=lambda: is_new_file, path=lambda: path)
    hou.hipFile = hip_file
    hou.applicationVersion = lambda: (20, 5, 445)
    sys.modules["hou"] = hou


def _install_fake_hip_file_events():
    """A `hou.hipFile` that actually records/removes callbacks, the way
    `watch_hip_dir_changes`/`unwatch_hip_dir_changes` expect — real
    Houdini's `addEventCallback` takes one positional arg (the callback)
    and calls it with an event-type argument; `removeEventCallback` takes
    back the exact object `addEventCallback` was given."""
    registered: list = []
    hip_file = types.SimpleNamespace(
        addEventCallback=lambda cb: registered.append(cb),
        removeEventCallback=lambda cb: registered.remove(cb),
    )
    hou = types.ModuleType("hou")
    hou.hipFile = hip_file
    sys.modules["hou"] = hou
    return registered


# --- fx_port -----------------------------------------------------------


def test_fx_port_reads_from_startup_module_when_running():
    _install_fake_startup(running=True, port=8103)
    assert scene.fx_port() == 8103


def test_fx_port_none_when_startup_module_says_not_running():
    _install_fake_startup(running=False, port=8100)
    assert scene.fx_port() is None


def test_fx_port_falls_back_to_http_scan_when_plugin_not_loaded(monkeypatch):
    # fxhoudinimcp_server is entirely absent (guaranteed by the fixture
    # above) — so fx_port must fall into the degraded path, not raise ImportError.
    monkeypatch.setattr(scene, "_probe_health", lambda port: port == 8105)
    assert scene.fx_port() == 8105


def test_fx_port_scan_exhausted_returns_none(monkeypatch):
    monkeypatch.setattr(scene, "_probe_health", lambda port: False)
    assert scene.fx_port() is None


def test_fx_port_scan_finds_a_server_that_comes_up_after_a_failed_scan(monkeypatch):
    """The real incident: fxhoudinimcp's own auto-start is asynchronous
    (`uiready.py` polls readiness on a worker thread, up to ~15s) and the
    panel's very first scan can easily land before that poll finishes. A
    "no port" answer from that first scan must not be remembered for the
    rest of the Houdini session — the very next call has to notice the
    server came up in the meantime, not repeat the stale "no port" forever.
    """
    monkeypatch.setattr(scene, "_probe_health", lambda port: False)
    assert scene.fx_port() is None

    monkeypatch.setattr(scene, "_probe_health", lambda port: port == 8107)
    assert scene.fx_port() == 8107


def test_fx_port_scan_does_not_rescan_once_a_port_is_found(monkeypatch):
    """The flip side of the fix above: a POSITIVE scan is still cached for
    the life of the process — otherwise every call to `fx_port()` (a diagnostics
    click, a new conversation, the boot log) would re-pay the up-to-1s scan
    cost even once the answer is already known, on Houdini's main thread."""
    calls: list[int] = []

    def fake_scan() -> int | None:
        calls.append(1)
        return 8103

    monkeypatch.setattr(scene, "_scan_for_any_fx_port", fake_scan)

    assert scene.fx_port() == 8103
    assert scene.fx_port() == 8103
    assert len(calls) == 1


# --- fx_pending ------------------------------------------------------------


def test_fx_pending_true_while_the_readiness_poll_is_in_flight():
    """The exact race the first-session bug is about: the plugin is loaded,
    auto-start already kicked off `uiready.py`'s async worker thread, and
    that thread hasn't confirmed readiness yet. Worth waiting for — the
    port is likely to appear within `fxhoudinimcp_server.startup`'s own
    15s ceiling."""
    _install_fake_startup(running=False, port=8100, starting=True)
    assert scene.fx_pending() is True


def test_fx_pending_false_once_the_server_is_up():
    """Nothing left to wait for — `fx_port()` already has an answer."""
    _install_fake_startup(running=True, port=8100, starting=False)
    assert scene.fx_pending() is False


def test_fx_pending_false_when_the_poll_never_started():
    """Autostart off, or the plugin loaded too late to have kicked off its
    worker thread yet — `is_starting()` is False and so is `is_running()`.
    There is no poll in flight to wait on, so waiting here would just be a
    fixed delay for no reason."""
    _install_fake_startup(running=False, port=8100, starting=False)
    assert scene.fx_pending() is False


def test_fx_pending_false_when_plugin_not_loaded():
    """No `fxhoudinimcp_server` in this process at all (fixture guarantees
    it's absent) — the HTTP-scan fallback path, not the in-process one.
    Nothing here will ever set `is_starting()`, so there's nothing to wait
    for."""
    assert scene.fx_pending() is False


# --- mcp_servers ---------------------------------------------------------


def test_mcp_servers_pins_port_when_known(monkeypatch):
    monkeypatch.setattr(scene, "fx_port", lambda: 8101)
    monkeypatch.setattr(scene, "fx_python", lambda: "/opt/python3.12")
    monkeypatch.delenv("HAP_MCP_PATH", raising=False)

    servers = scene.mcp_servers()

    assert servers == [
        {
            "name": "fxhoudini",
            "command": "/opt/python3.12",
            "args": ["-c", scene.FX_BOOTSTRAP],
            "env": [
                {"name": "HOUDINI_HOST", "value": "127.0.0.1"},
                {"name": "HOUDINI_PORT", "value": "8101"},
                {"name": "PYTHONPATH", "value": ""},
            ],
        }
    ]


def test_mcp_servers_clears_pythonpath_when_no_mcp_path_is_recorded(monkeypatch):
    """The belt-and-suspenders half of the `shellenv.py` fix: even if some
    other leak someday hands PYTHONPATH to the process this server is
    spawned under, an explicit empty override here neutralizes it for THIS
    child specifically — an interpreter with `HAP_MCP_PATH` unset is
    assumed to carry its own matching `fxhoudinimcp` (`install.py::
    _mcp_python`), and a leaked, differently-versioned PYTHONPATH is
    exactly what breaks that assumption."""
    monkeypatch.setattr(scene, "fx_port", lambda: 8100)
    monkeypatch.delenv("HAP_MCP_PATH", raising=False)

    entry = scene.mcp_servers()[0]

    pythonpaths = [item["value"] for item in entry["env"] if item["name"] == "PYTHONPATH"]
    assert pythonpaths == [""]


def test_mcp_servers_still_sets_hap_mcp_path_when_recorded(monkeypatch):
    """The override above must not shadow the real, intentional case: an
    interpreter that DOES need telling (Houdini's own plain CPython) still
    gets its actual deps tree, not the empty override."""
    monkeypatch.setattr(scene, "fx_port", lambda: 8100)
    monkeypatch.setenv("HAP_MCP_PATH", "/deps/py3.13")

    entry = scene.mcp_servers()[0]

    pythonpaths = [item["value"] for item in entry["env"] if item["name"] == "PYTHONPATH"]
    assert pythonpaths == ["/deps/py3.13"]


def test_mcp_servers_env_is_a_list_not_a_dict(monkeypatch):
    """`McpServerStdio.env` is a `list[EnvVariable]`, not a dict (facts/acp-sdk.md §4)."""
    monkeypatch.setattr(scene, "fx_port", lambda: 8100)
    entry = scene.mcp_servers()[0]
    assert isinstance(entry["env"], list)
    assert all({"name", "value"} == set(item) for item in entry["env"])


def test_mcp_servers_without_port_degrades_without_pin(monkeypatch):
    monkeypatch.setattr(scene, "fx_port", lambda: None)
    entry = scene.mcp_servers()[0]
    names = [item["name"] for item in entry["env"]]
    assert "HOUDINI_PORT" not in names
    assert "HOUDINI_HOST" in names


def test_mcp_servers_logs_the_port_it_actually_pinned(monkeypatch, caplog):
    """What port (or its absence) went into THIS session's mcpServers must be
    visible in the log — the previous single boot-time "fx port None" line
    gave no way to tell a one-off race from a permanently dead server."""
    monkeypatch.setattr(scene, "fx_port", lambda: 8104)
    with caplog.at_level("INFO", logger="houdini_agent_panel.scene"):
        scene.mcp_servers()
    assert "8104" in caplog.text


def test_mcp_servers_logs_the_missing_port(monkeypatch, caplog):
    monkeypatch.setattr(scene, "fx_port", lambda: None)
    with caplog.at_level("WARNING", logger="houdini_agent_panel.scene"):
        scene.mcp_servers()
    assert "fx server" in caplog.text.lower()


# --- fx_python -------------------------------------------------------------


def test_fx_python_prefers_hap_python(monkeypatch):
    monkeypatch.setenv("HAP_PYTHON", "/opt/homebrew/bin/python3.12")
    assert scene.fx_python() == "/opt/homebrew/bin/python3.12"


def test_fx_python_falls_back_to_sys_executable(monkeypatch):
    monkeypatch.delenv("HAP_PYTHON", raising=False)
    assert scene.fx_python() == sys.executable


# --- mcp_python_status -------------------------------------------------


def test_mcp_python_status_none_when_hap_python_unset(monkeypatch):
    """Absence of HAP_PYTHON is not flagged — routine outside an installed
    panel (dev mode, tests, a hand-built package json)."""
    monkeypatch.delenv("HAP_PYTHON", raising=False)
    assert scene.mcp_python_status() is None


def test_mcp_python_status_none_when_the_recorded_interpreter_exists(monkeypatch, tmp_path):
    python = tmp_path / "python3.11"
    python.touch()
    monkeypatch.setenv("HAP_PYTHON", str(python))
    assert scene.mcp_python_status() is None


def test_mcp_python_status_flags_a_vanished_interpreter(monkeypatch, tmp_path):
    """The recorded interpreter existed at install time and does not any
    more — a pruned uv cache, a recreated venv, a Houdini reinstalled to a
    new path."""
    gone = tmp_path / "no-longer-here" / "python3.11"
    monkeypatch.setenv("HAP_PYTHON", str(gone))

    status = scene.mcp_python_status()

    assert status is not None
    assert str(gone) in status


def test_mcp_servers_logs_a_vanished_interpreter(monkeypatch, tmp_path, caplog):
    gone = tmp_path / "no-longer-here" / "python3.11"
    monkeypatch.setenv("HAP_PYTHON", str(gone))
    monkeypatch.setattr(scene, "fx_port", lambda: 8100)

    with caplog.at_level("ERROR", logger="houdini_agent_panel.scene"):
        scene.mcp_servers()

    assert str(gone) in caplog.text


# --- hip_dir ---------------------------------------------------------------


def test_hip_dir_new_file_is_home(tmp_path):
    _install_fake_hou(is_new_file=True, path=str(tmp_path / "untitled.hip"))
    from pathlib import Path

    assert scene.hip_dir() == str(Path.home())


def test_hip_dir_existing_scene_is_its_directory(tmp_path):
    hip_path = tmp_path / "shots" / "shot010.hip"
    hip_path.parent.mkdir(parents=True)
    _install_fake_hou(is_new_file=False, path=str(hip_path))

    assert scene.hip_dir() == str(hip_path.parent)


def test_hip_dir_missing_directory_falls_back_to_home(tmp_path):
    from pathlib import Path

    missing = tmp_path / "gone" / "shot.hip"
    _install_fake_hou(is_new_file=False, path=str(missing))

    assert scene.hip_dir() == str(Path.home())


# --- real_hip_dir ------------------------------------------------------------


def test_real_hip_dir_new_file_is_none(tmp_path):
    _install_fake_hou(is_new_file=True, path=str(tmp_path / "untitled.hip"))
    assert scene.real_hip_dir() is None


def test_real_hip_dir_existing_scene_is_its_directory(tmp_path):
    hip_path = tmp_path / "shots" / "shot010.hip"
    hip_path.parent.mkdir(parents=True)
    _install_fake_hou(is_new_file=False, path=str(hip_path))

    assert scene.real_hip_dir() == str(hip_path.parent)


def test_real_hip_dir_missing_directory_is_none(tmp_path):
    missing = tmp_path / "gone" / "shot.hip"
    _install_fake_hou(is_new_file=False, path=str(missing))

    assert scene.real_hip_dir() is None


def test_hip_dir_falls_back_to_home_exactly_when_real_hip_dir_is_none(tmp_path, monkeypatch):
    """`hip_dir()` must not drift from `real_hip_dir()` — it's a thin
    wrapper that only supplies the `$HOME` fallback."""
    from pathlib import Path

    monkeypatch.setattr(scene, "real_hip_dir", lambda: None)
    assert scene.hip_dir() == str(Path.home())

    monkeypatch.setattr(scene, "real_hip_dir", lambda: str(tmp_path))
    assert scene.hip_dir() == str(tmp_path)


# --- watch_hip_dir_changes --------------------------------------------------


def test_watch_hip_dir_changes_registers_with_hip_file():
    registered = _install_fake_hip_file_events()
    scene.watch_hip_dir_changes(lambda: None)
    assert len(registered) == 1


def test_watch_hip_dir_changes_callback_forwards_to_ours():
    _install_fake_hip_file_events()
    calls = []
    handle = scene.watch_hip_dir_changes(lambda: calls.append(1))
    # Houdini calls the registered callback with an event-type argument —
    # `hipFileEventType` in real use — which `watch_hip_dir_changes` must
    # swallow rather than pass through to a caller that only wants "the
    # scene may have moved," not which of nine event kinds did it.
    handle("AfterLoad")
    assert calls == [1]


def test_unwatch_hip_dir_changes_removes_the_same_callback():
    registered = _install_fake_hip_file_events()
    handle = scene.watch_hip_dir_changes(lambda: None)
    assert registered  # sanity: something was actually added
    scene.unwatch_hip_dir_changes(handle)
    assert registered == []


def test_unwatch_hip_dir_changes_on_an_unknown_handle_does_not_raise():
    """`shutdown()` calls this unconditionally once `_boot()` has run; a
    handle Houdini no longer recognises must not turn closing a tab into
    a crash."""
    _install_fake_hip_file_events()
    scene.unwatch_hip_dir_changes(object())


# --- houdini_version / is_fx_available -------------------------------------


def test_houdini_version_from_env(monkeypatch):
    monkeypatch.setenv("HOUDINI_VERSION", "20.5.445")
    assert scene.houdini_version() == "20.5.445"


def test_houdini_version_falls_back_to_hou(monkeypatch):
    monkeypatch.delenv("HOUDINI_VERSION", raising=False)
    _install_fake_hou(is_new_file=True, path="/tmp/untitled.hip")
    assert scene.houdini_version() == "20.5.445"


def test_is_fx_available_true_when_port_known(monkeypatch):
    monkeypatch.setattr(scene, "fx_port", lambda: 8100)
    assert scene.is_fx_available() is True


def test_is_fx_available_false_when_no_port(monkeypatch):
    monkeypatch.setattr(scene, "fx_port", lambda: None)
    assert scene.is_fx_available() is False


# --- haio ------------------------------------------------------------------


def _run_bootstrap(tmp_path, *, policy_module: str) -> dict:
    """Run `FX_BOOTSTRAP` for real, in a child interpreter.

    Not `exec()` in-process: the bootstrap's own `import sys, asyncio,
    runpy` rebinds those names, so any stub handed to `exec` is discarded —
    the first attempt at this test imported the REAL fxhoudinimcp and hung
    on a live server. A child process with a fake `fxhoudinimcp` ahead of it
    on `sys.path` measures the actual code instead.
    """
    import json
    import subprocess
    import sys

    package = tmp_path / "fxhoudinimcp"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "__main__.py").write_text(
        "import sys, asyncio, json\n"
        "print(json.dumps({'argv': sys.argv,"
        " 'policy': type(asyncio.get_event_loop_policy()).__module__}))\n"
    )

    preamble = (
        "import asyncio\n"
        "class _Policy(asyncio.DefaultEventLoopPolicy): pass\n"
        f"_Policy.__module__ = {policy_module!r}\n"
        "asyncio.set_event_loop_policy(_Policy())\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", preamble + scene.FX_BOOTSTRAP],
        capture_output=True,
        text=True,
        cwd=tmp_path,
        env={"PYTHONPATH": str(tmp_path), "PATH": "/usr/bin:/bin"},
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_fx_bootstrap_replaces_the_haio_policy(tmp_path):
    """`hython` installs `haio.HoudiniEventLoopPolicy`, whose loop raises
    `NotImplementedError` from `get_task_factory`. anyio calls exactly that
    while opening its task group, so `mcp` — and with it the fx server —
    dies during startup, before reading a byte of the protocol. Measured on
    Houdini 22.0.368 (Python 3.13) and 20.5.445 (3.11): both crash, both
    start cleanly once the stock policy is restored.

    Reported as Codex showing `mcp__fxhoudini__startup ✗ failed`. Claude
    failed the same way and said nothing about it.
    """
    seen = _run_bootstrap(tmp_path, policy_module="haio")

    assert seen["policy"].split(".")[0] == "asyncio", (
        f"the haio policy was left in place: {seen['policy']}"
    )


def test_fx_bootstrap_leaves_an_ordinary_python_alone(tmp_path):
    """Under a normal interpreter there is no haio and nothing to repair."""
    seen = _run_bootstrap(tmp_path, policy_module="somewhere_else")

    assert seen["policy"] == "somewhere_else"


def test_fx_bootstrap_fixes_argv_for_the_server(tmp_path):
    """fxhoudinimcp parses `sys.argv` and rejects `-c` — measured:
    "unknown command: -m", followed by its usage text and no server."""
    seen = _run_bootstrap(tmp_path, policy_module="haio")

    assert seen["argv"] == ["fxhoudinimcp"]

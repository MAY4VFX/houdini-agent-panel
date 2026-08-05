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


def _install_fake_startup(*, running: bool, port: int) -> None:
    package = types.ModuleType("fxhoudinimcp_server")
    startup = types.ModuleType("fxhoudinimcp_server.startup")
    startup.is_running = lambda: running
    startup.get_port = lambda: port
    package.startup = startup
    sys.modules["fxhoudinimcp_server"] = package
    sys.modules["fxhoudinimcp_server.startup"] = startup


def _install_fake_hou(*, is_new_file: bool, path: str) -> None:
    hou = types.ModuleType("hou")
    hip_file = types.SimpleNamespace(isNewFile=lambda: is_new_file, path=lambda: path)
    hou.hipFile = hip_file
    hou.applicationVersion = lambda: (20, 5, 445)
    sys.modules["hou"] = hou


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


# --- mcp_servers ---------------------------------------------------------


def test_mcp_servers_pins_port_when_known(monkeypatch):
    monkeypatch.setattr(scene, "fx_port", lambda: 8101)
    monkeypatch.setattr(scene, "fx_python", lambda: "/opt/python3.12")

    servers = scene.mcp_servers()

    assert servers == [
        {
            "name": "fxhoudini",
            "command": "/opt/python3.12",
            "args": ["-c", scene.FX_BOOTSTRAP],
            "env": [
                {"name": "HOUDINI_HOST", "value": "127.0.0.1"},
                {"name": "HOUDINI_PORT", "value": "8101"},
            ],
        }
    ]


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


# --- fx_python -------------------------------------------------------------


def test_fx_python_prefers_hap_python(monkeypatch):
    monkeypatch.setenv("HAP_PYTHON", "/opt/homebrew/bin/python3.12")
    assert scene.fx_python() == "/opt/homebrew/bin/python3.12"


def test_fx_python_falls_back_to_sys_executable(monkeypatch):
    monkeypatch.delenv("HAP_PYTHON", raising=False)
    assert scene.fx_python() == sys.executable


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

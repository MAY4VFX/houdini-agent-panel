"""Тесты `scene.py` — без Houdini: `hou`/`fxhoudinimcp_server` подставляются
фейковыми модулями в `sys.modules`, ровно так, как задумана ленивость импорта.
"""

from __future__ import annotations

import sys
import types

import pytest

from houdini_agent_panel import scene


@pytest.fixture(autouse=True)
def _clean_fake_modules():
    """На случай, если предыдущий тест забыл прибрать за собой."""
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
    # fxhoudinimcp_server отсутствует вовсе (гарантировано fixture'ой выше) —
    # значит fx_port должен уйти в деградацию, а не упасть с ImportError.
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
            "args": ["-m", "fxhoudinimcp"],
            "env": [
                {"name": "HOUDINI_HOST", "value": "127.0.0.1"},
                {"name": "HOUDINI_PORT", "value": "8101"},
            ],
        }
    ]


def test_mcp_servers_env_is_a_list_not_a_dict(monkeypatch):
    """`McpServerStdio.env` — `list[EnvVariable]`, не словарь (facts/acp-sdk.md §4)."""
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

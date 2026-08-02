"""Tests for generating the package json and locating Houdini directories on disk."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from houdini_agent_panel import houdini_package


def test_plugin_path_is_houdini_subdir_next_to_module():
    path = houdini_package.plugin_path()
    assert path == Path(houdini_package.__file__).resolve().parent / "houdini"


def test_package_json_matches_architecture_format():
    """The structure must match architecture.md §0 letter for letter."""
    deps = Path("/Users/x/Library/Application Support/HoudiniAgentPanel/deps/py3.11")
    payload = json.loads(
        houdini_package.package_json(deps=deps, installer_python="/opt/homebrew/bin/python3.12")
    )
    assert payload == {
        "env": [
            {"HAP_DEPS": deps.as_posix()},
            {"HAP_PYTHON": "/opt/homebrew/bin/python3.12"},
            {"PYTHONPATH": {"value": "$HAP_DEPS", "method": "prepend"}},
        ],
        "path": "$HAP_DEPS/houdini_agent_panel/houdini",
    }


def test_package_json_uses_posix_separators_even_if_given_windows_style(tmp_path):
    deps = tmp_path / "deps" / "py3.13"
    payload = json.loads(
        houdini_package.package_json(deps=deps, installer_python="C:/Python312/python.exe")
    )
    assert payload["env"][0]["HAP_DEPS"] == deps.as_posix()
    assert "\\" not in payload["env"][0]["HAP_DEPS"]


def test_package_json_with_explicit_plugin_overrides_path(tmp_path):
    plugin_dir = tmp_path / "src" / "houdini"
    payload = json.loads(
        houdini_package.package_json(
            deps=tmp_path / "deps",
            installer_python="/usr/bin/python3",
            plugin=plugin_dir,
        )
    )
    assert payload["path"] == plugin_dir.as_posix()


def test_package_json_ends_with_trailing_newline():
    text = houdini_package.package_json(deps=Path("/x"), installer_python="/usr/bin/python3")
    assert text.endswith("\n")


def test_houdini_version_of_bare_version_dir():
    assert houdini_package.houdini_version_of(Path("/whatever/20.5")) == "20.5"


def test_houdini_version_of_prefixed_dir():
    assert houdini_package.houdini_version_of(Path("/home/user/houdini22.0")) == "22.0"


@pytest.mark.parametrize("name", ["packages", "20.5beta", "houdini", "houdini-20.5", ""])
def test_houdini_version_of_rejects_non_version_names(name):
    assert houdini_package.houdini_version_of(Path("/whatever") / name) is None


def test_candidate_package_dirs_macos_only_existing_prefs(tmp_path, monkeypatch):
    monkeypatch.setattr(houdini_package.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(houdini_package.Path, "home", staticmethod(lambda: tmp_path))

    root = tmp_path / "Library" / "Preferences" / "houdini"
    (root / "20.5").mkdir(parents=True)
    (root / "22.0").mkdir(parents=True)
    (root / "not_a_version").mkdir(parents=True)

    found = houdini_package.candidate_package_dirs()

    assert sorted(p.parent.name for p in found) == ["20.5", "22.0"]
    for packages in found:
        assert packages.name == "packages"
        assert packages.is_dir()  # candidate_package_dirs may create it itself


def test_candidate_package_dirs_macos_no_root_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(houdini_package.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(houdini_package.Path, "home", staticmethod(lambda: tmp_path))

    assert houdini_package.candidate_package_dirs() == []


def test_candidate_package_dirs_linux_prefixed_names(tmp_path, monkeypatch):
    monkeypatch.setattr(houdini_package.platform, "system", lambda: "Linux")
    monkeypatch.setattr(houdini_package.Path, "home", staticmethod(lambda: tmp_path))

    (tmp_path / "houdini20.5").mkdir()
    (tmp_path / "houdini22.0").mkdir()
    (tmp_path / "not-houdini").mkdir()

    found = houdini_package.candidate_package_dirs()

    assert sorted(p.parent.name for p in found) == ["houdini20.5", "houdini22.0"]


def test_candidate_package_dirs_windows_documents(tmp_path, monkeypatch):
    monkeypatch.setattr(houdini_package.platform, "system", lambda: "Windows")
    monkeypatch.setattr(houdini_package.Path, "home", staticmethod(lambda: tmp_path))

    documents = tmp_path / "Documents"
    (documents / "houdini20.5").mkdir(parents=True)

    found = houdini_package.candidate_package_dirs()

    assert len(found) == 1
    assert found[0].parent.name == "houdini20.5"
    assert found[0].parent == documents / "houdini20.5"


def test_package_name_constant():
    assert houdini_package.PACKAGE_NAME == "houdini_agent_panel.json"

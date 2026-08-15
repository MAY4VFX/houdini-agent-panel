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


def test_houdini_version_of_houdini_21_dir():
    assert houdini_package.houdini_version_of(Path("/whatever/21.0")) == "21.0"


@pytest.mark.parametrize("name", ["packages", "20.5beta", "houdini", "houdini-20.5", ""])
def test_houdini_version_of_rejects_non_version_names(name):
    assert houdini_package.houdini_version_of(Path("/whatever") / name) is None


def test_candidate_package_dirs_macos_only_existing_prefs(tmp_path, monkeypatch):
    monkeypatch.setattr(houdini_package.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(houdini_package.Path, "home", staticmethod(lambda: tmp_path))

    root = tmp_path / "Library" / "Preferences" / "houdini"
    (root / "20.5").mkdir(parents=True)
    (root / "21.0").mkdir(parents=True)
    (root / "22.0").mkdir(parents=True)
    (root / "not_a_version").mkdir(parents=True)

    found = houdini_package.candidate_package_dirs()

    assert sorted(p.parent.name for p in found) == ["20.5", "21.0", "22.0"]
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
    (tmp_path / "houdini21.0").mkdir()
    (tmp_path / "houdini22.0").mkdir()
    (tmp_path / "not-houdini").mkdir()

    found = houdini_package.candidate_package_dirs()

    assert sorted(p.parent.name for p in found) == [
        "houdini20.5", "houdini21.0", "houdini22.0"
    ]


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


def test_candidate_package_dirs_windows_onedrive_documents(tmp_path, monkeypatch):
    """OneDrive's "back up your Documents" moves the real Documents folder
    to `~/OneDrive/Documents` — on by default on a managed Windows 11. With
    only `~/Documents` looked at, the installer reported "no Houdini
    preferences directory found" on a machine that plainly had one."""
    monkeypatch.setattr(houdini_package.platform, "system", lambda: "Windows")
    monkeypatch.setattr(houdini_package.Path, "home", staticmethod(lambda: tmp_path))

    onedrive = tmp_path / "OneDrive" / "Documents"
    (onedrive / "houdini20.5").mkdir(parents=True)

    found = houdini_package.candidate_package_dirs()

    assert [p.parent for p in found] == [onedrive / "houdini20.5"]


def test_candidate_package_dirs_windows_reports_every_root(tmp_path, monkeypatch):
    """Both places can exist at once, and which one a given Houdini reads
    cannot be told from here — so both are candidates, the same answer
    fxhoudinimcp's own installer gives (docs/facts/fxhoudinimcp.md §2)."""
    monkeypatch.setattr(houdini_package.platform, "system", lambda: "Windows")
    monkeypatch.setattr(houdini_package.Path, "home", staticmethod(lambda: tmp_path))

    documents = tmp_path / "Documents" / "houdini20.5"
    onedrive = tmp_path / "OneDrive" / "Documents" / "houdini22.0"
    documents.mkdir(parents=True)
    onedrive.mkdir(parents=True)

    found = {p.parent for p in houdini_package.candidate_package_dirs()}

    assert found == {documents, onedrive}


def _plugin_with_pypanel(root: Path, body: str = "<pythonPanelDocument/>") -> Path:
    plugin = root / "houdini_agent_panel" / "houdini"
    panels = plugin / "python_panels"
    panels.mkdir(parents=True)
    (panels / houdini_package.PYPANEL_NAME).write_text(body, encoding="utf-8")
    return plugin


def test_install_pypanel_copies_into_prefs(tmp_path):
    """Houdini registers the interface from either place but only offers it in
    the New Pane Tab Type menu from the prefs directory."""
    deps = tmp_path / "deps" / "py3.13"
    plugin = _plugin_with_pypanel(deps, "<pythonPanelDocument>agent</pythonPanelDocument>")
    prefs = tmp_path / "houdini22.0"

    written = houdini_package.install_pypanel(prefs, plugin)

    assert written == prefs / "python_panels" / houdini_package.PYPANEL_NAME
    assert written.read_text(encoding="utf-8") == "<pythonPanelDocument>agent</pythonPanelDocument>"
    assert (plugin / "python_panels" / houdini_package.PYPANEL_NAME).exists()


def test_install_pypanel_serves_every_prefs_dir_sharing_one_deps_tree(tmp_path):
    """20.5 and 21 share the 3.11 dependency tree. Clearing the plugin copy
    inside the loop left the second Houdini with nothing to install -- caught
    on the Windows VM, where two prefs directories share one tree."""
    deps = tmp_path / "deps" / "py3.11"
    plugin = _plugin_with_pypanel(deps)
    first = tmp_path / "houdini20.5"
    second = tmp_path / "houdini21.0"

    assert houdini_package.install_pypanel(first, plugin) is not None
    assert houdini_package.install_pypanel(second, plugin) is not None
    assert (second / "python_panels" / houdini_package.PYPANEL_NAME).is_file()


def test_clear_plugin_pypanel_removes_the_duplicate_inside_deps(tmp_path):
    deps = tmp_path / "deps" / "py3.13"
    plugin = _plugin_with_pypanel(deps)

    cleared = houdini_package.clear_plugin_pypanel(plugin, deps=deps)

    assert cleared == plugin / "python_panels" / houdini_package.PYPANEL_NAME
    assert not cleared.exists()


def test_clear_plugin_pypanel_leaves_a_checkout_alone(tmp_path):
    """Dev mode points Houdini at the maintainer's checkout. Deleting a tracked
    file out of someone's repository is not ours to do."""
    deps = tmp_path / "deps" / "py3.13"
    deps.mkdir(parents=True)
    checkout = _plugin_with_pypanel(tmp_path / "repo" / "python")

    assert houdini_package.clear_plugin_pypanel(checkout, deps=deps) is None
    assert (checkout / "python_panels" / houdini_package.PYPANEL_NAME).exists()


def test_clear_plugin_pypanel_is_idempotent(tmp_path):
    deps = tmp_path / "deps" / "py3.13"
    plugin = _plugin_with_pypanel(deps)

    assert houdini_package.clear_plugin_pypanel(plugin, deps=deps) is not None
    assert houdini_package.clear_plugin_pypanel(plugin, deps=deps) is None


def test_install_pypanel_reports_a_plugin_tree_without_one(tmp_path):
    deps = tmp_path / "deps" / "py3.13"
    plugin = deps / "houdini_agent_panel" / "houdini"
    plugin.mkdir(parents=True)

    assert houdini_package.install_pypanel(tmp_path / "houdini22.0", plugin) is None


def test_install_pypanel_overwrites_a_stale_prefs_copy(tmp_path):
    """pip restores the plugin copy on every install, so this runs again and
    again over its own output."""
    deps = tmp_path / "deps" / "py3.13"
    plugin = _plugin_with_pypanel(deps, "new")
    prefs = tmp_path / "houdini22.0"
    (prefs / "python_panels").mkdir(parents=True)
    (prefs / "python_panels" / houdini_package.PYPANEL_NAME).write_text("old", encoding="utf-8")

    written = houdini_package.install_pypanel(prefs, plugin)

    assert written is not None
    assert written.read_text(encoding="utf-8") == "new"

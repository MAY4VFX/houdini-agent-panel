"""Tests for install/uninstall/doctor orchestration.

Neither hython nor pip actually run here — `deps.find_hython`,
`deps.python_version_of`, `deps.install_deps` are all replaced with mocks, and
Houdini on disk is simulated by the `_fake_houdini` fixture.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

import pytest

from houdini_agent_panel import deps as deps_mod
from houdini_agent_panel import houdini_package
from houdini_agent_panel import install as install_mod
from houdini_agent_panel import mcp_runtime
from houdini_agent_panel import paths


@pytest.fixture
def fake_houdini(tmp_path, monkeypatch):
    """A single Houdini "20.5" with an existing prefs directory."""
    monkeypatch.setattr(houdini_package.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(houdini_package.Path, "home", staticmethod(lambda: tmp_path))
    prefs = tmp_path / "Library" / "Preferences" / "houdini" / "20.5"
    prefs.mkdir(parents=True)
    return prefs


def _stub_hython(monkeypatch, *, version=(3, 11), hython_path="/fake/hfs20.5/bin/hython"):
    hython = Path(hython_path)
    monkeypatch.setattr(deps_mod, "find_hython", lambda houdini_version: hython)
    monkeypatch.setattr(deps_mod, "python_version_of", lambda path: version)
    return hython


def test_install_dry_run_does_not_write_package_json(fake_houdini, monkeypatch):
    hython = _stub_hython(monkeypatch)
    calls = []
    monkeypatch.setattr(
        deps_mod,
        "install_deps",
        lambda *a, **k: calls.append((a, k)) or [],
    )

    code = install_mod.install(dry_run=True, out=lambda *_: None)

    assert code == 0
    assert not (fake_houdini / "packages" / houdini_package.PACKAGE_NAME).exists()
    # install_deps must also receive dry_run=True instead of actually running.
    assert calls[0][1]["dry_run"] is True


def test_install_writes_package_json_with_correct_deps_path(fake_houdini, monkeypatch):
    _stub_hython(monkeypatch, version=(3, 11))
    monkeypatch.setattr(deps_mod, "install_deps", lambda *a, **k: ["ok"])

    code = install_mod.install(out=lambda *_: None)

    assert code == 0
    package_path = fake_houdini / "packages" / houdini_package.PACKAGE_NAME
    assert package_path.exists()
    payload = json.loads(package_path.read_text("utf-8"))
    expected_deps = paths.deps_dir("py3.11").as_posix()
    assert payload["env"][0] == {"HAP_DEPS": expected_deps}
    assert payload["path"] == "$HAP_DEPS/houdini_agent_panel/houdini"

    fx_package_path = fake_houdini / "packages" / "fxhoudinimcp.json"
    assert fx_package_path.exists()
    fx_payload = json.loads(fx_package_path.read_text("utf-8"))
    assert fx_payload == {
        "env": [{"FXHOUDINIMCP": f"{expected_deps}/fxhoudinimcp/houdini"}],
        "path": "$FXHOUDINIMCP",
    }


def test_install_records_the_installers_python_with_a_reason(fake_houdini, monkeypatch):
    """Point 4 of the ephemeral-python fix: what got written into HAP_PYTHON,
    and why, must show up in the installer's own output — the owner's real
    incident was only diagnosed by inspecting a live process's arguments,
    because the log said nothing."""
    _stub_hython(monkeypatch)
    monkeypatch.setattr(deps_mod, "install_deps", lambda *a, **k: ["ok"])
    logged = []

    code = install_mod.install(out=logged.append)

    assert code == 0
    assert any("HAP_PYTHON" in line and sys.executable in line for line in logged)


# --- ephemeral installer python (e.g. `uvx --no-cache`) ---------------


def _ephemeral_path(name: str) -> Path:
    return Path(tempfile.gettempdir()) / name / "archive-v0" / "deadbeef" / "bin" / "python"


def test_install_ephemeral_installer_python_falls_back_to_plain_cpython(
    fake_houdini, monkeypatch
):
    """`uvx --no-cache` unpacks its whole run into a directory under the
    system temp root and deletes it the instant this command exits —
    `sys.executable` inside that run is a path already known to be gone.
    Recording it would leave the panel installed with a HAP_PYTHON that
    never existed by the time Houdini opens. Houdini's own plain CPython —
    the same remedy already used for the `hython` case — is permanent and
    must be preferred instead."""
    _stub_hython(monkeypatch)
    monkeypatch.setattr(deps_mod, "install_deps", lambda *a, **k: ["ok"])
    monkeypatch.setattr(install_mod.sys, "executable", str(_ephemeral_path(".tmpdsyxSk")))
    plain = Path("/fake/hfs20.5/python/bin/python3.11")
    monkeypatch.setattr(mcp_runtime, "find", lambda *a, **k: plain)
    logged = []

    code = install_mod.install(out=logged.append)

    assert code == 0
    package_path = fake_houdini / "packages" / houdini_package.PACKAGE_NAME
    payload = json.loads(package_path.read_text("utf-8"))
    assert {"HAP_PYTHON": plain.as_posix()} in payload["env"]
    assert any("temporary directory" in line for line in logged)


def test_install_ephemeral_installer_python_without_fallback_skips_this_houdini(
    fake_houdini, monkeypatch
):
    """No plain CPython to fall back to, and the installer's own python
    won't survive this command either — the installer must refuse to write
    a package file that records a path already known to be gone, rather
    than report success while leaving the panel without any Houdini tools
    (discovered, in the real incident, only when the agent said it had
    none)."""
    _stub_hython(monkeypatch)
    monkeypatch.setattr(deps_mod, "install_deps", lambda *a, **k: ["ok"])
    monkeypatch.setattr(install_mod.sys, "executable", str(_ephemeral_path(".tmpYYYYYY")))
    monkeypatch.setattr(mcp_runtime, "find", lambda *a, **k: None)
    logged = []

    code = install_mod.install(out=logged.append)

    assert code == 1
    assert not (fake_houdini / "packages" / houdini_package.PACKAGE_NAME).exists()
    assert any("refus" in line.lower() for line in logged)


def test_install_ephemeral_installer_python_dry_run_does_not_probe_for_a_fallback(
    fake_houdini, monkeypatch
):
    """Same as the ordinary hython dry-run path: --dry-run only announces
    intent, it never actually searches disk for Houdini's plain CPython."""
    _stub_hython(monkeypatch)
    monkeypatch.setattr(deps_mod, "install_deps", lambda *a, **k: [])
    monkeypatch.setattr(install_mod.sys, "executable", str(_ephemeral_path(".tmpZZZZZZ")))

    def _boom(*a, **k):
        raise AssertionError("mcp_runtime.find must not run on a dry run")

    monkeypatch.setattr(mcp_runtime, "find", _boom)
    logged = []

    code = install_mod.install(dry_run=True, out=logged.append)

    assert code == 0
    assert not (fake_houdini / "packages" / houdini_package.PACKAGE_NAME).exists()


def test_install_skip_deps_still_writes_package_json_without_installing(fake_houdini, monkeypatch):
    _stub_hython(monkeypatch)

    def explode(*a, **k):
        raise AssertionError("install_deps must not be called when --skip-deps is set")

    monkeypatch.setattr(deps_mod, "install_deps", explode)

    code = install_mod.install(skip_deps=True, out=lambda *_: None)

    assert code == 0
    assert (fake_houdini / "packages" / houdini_package.PACKAGE_NAME).exists()


def test_install_no_houdini_found_returns_error(tmp_path, monkeypatch):
    monkeypatch.setattr(houdini_package.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(houdini_package.Path, "home", staticmethod(lambda: tmp_path))
    logged = []

    code = install_mod.install(out=logged.append)

    assert code == 1
    assert any("Houdini" in line for line in logged)


def test_install_missing_hython_skips_that_houdini_but_reports(fake_houdini, monkeypatch):
    monkeypatch.setattr(deps_mod, "find_hython", lambda houdini_version: None)
    logged = []

    code = install_mod.install(out=logged.append)

    assert code == 1
    assert not (fake_houdini / "packages" / houdini_package.PACKAGE_NAME).exists()
    assert any("hython" in line.lower() for line in logged)


def test_install_explicit_houdini_dir_overrides_autodetect(tmp_path, monkeypatch):
    explicit = tmp_path / "custom" / "packages"
    _stub_hython(monkeypatch)
    monkeypatch.setattr(deps_mod, "install_deps", lambda *a, **k: [])

    # An explicit dir doesn't look like a Houdini version in its parent's name,
    # so the version is unknown and there's nothing to call find_hython with:
    # make sure we still write the package json in that case, since the
    # directory was passed explicitly by a human.
    code = install_mod.install(houdini_dir=str(explicit), out=lambda *_: None)

    assert code == 0
    assert (explicit / houdini_package.PACKAGE_NAME).exists()


def test_install_without_agents_flag_installs_no_agent(fake_houdini, monkeypatch):
    _stub_hython(monkeypatch)
    monkeypatch.setattr(deps_mod, "install_deps", lambda *a, **k: [])

    code = install_mod.install(out=lambda *_: None)

    assert code == 0  # no agents were requested, so no agent module is touched


def test_install_with_agents_but_agent_modules_missing_reports_clear_error(fake_houdini, monkeypatch):
    """Must not depend on whether registry.py/runtime.py currently exist on
    disk — we patch the import point itself rather than relying on the real
    absence of the file (that's unstable: a parallel branch could add
    runtime.py at any moment, and a test tied to the filesystem would become
    flaky)."""
    _stub_hython(monkeypatch)
    monkeypatch.setattr(deps_mod, "install_deps", lambda *a, **k: [])

    def explode():
        raise ImportError("registry/runtime aren't ready yet (simulated for the test)")

    monkeypatch.setattr(install_mod, "_load_agent_modules", explode)
    logged = []

    code = install_mod.install(agents=["claude"], out=logged.append)

    assert code == 1
    assert any("registry" in line.lower() or "runtime" in line.lower() for line in logged)


def test_install_with_agents_opencode_installs_exactly_one_agent(fake_houdini, monkeypatch, fetcher):
    """The real (non-dry-run) --agents path must line up with the actual
    signatures of `registry.fetch_registry(*, force, max_age, fetch)` and
    `runtime.install_agent(entry, *, progress, fetch)` — not with whatever was
    in the architecture contract at the time install.py was written."""
    from houdini_agent_panel import registry as registry_mod

    _stub_hython(monkeypatch)
    monkeypatch.setattr(deps_mod, "install_deps", lambda *a, **k: [])

    registry_payload = {
        "version": "1.0.0",
        "agents": [
            {
                "id": "opencode",
                "name": "OpenCode",
                "version": "1.2.3",
                "distribution": {
                    "binary": {
                        registry_mod.platform_key(): {
                            "archive": "https://example.invalid/opencode.tar.gz",
                            "cmd": "./opencode",
                            "sha256": "deadbeef",
                        }
                    }
                },
            },
            {
                "id": "claude-acp",
                "name": "Claude Agent",
                "version": "9.9.9",
                "distribution": {"npx": {"package": "@zed-industries/claude-code-acp@9.9.9"}},
            },
        ],
    }
    fetcher.add_json(registry_mod.REGISTRY_URL, registry_payload)

    installed_ids = []

    def fake_install_agent(entry, **kwargs):
        installed_ids.append(entry.id)
        assert kwargs.get("fetch") is fetcher

    monkeypatch.setattr(
        "houdini_agent_panel.runtime.install_agent", fake_install_agent
    )

    code = install_mod.install(agents=["opencode"], fetch=fetcher, out=lambda *_: None)

    assert code == 0
    assert installed_ids == ["opencode"]
    assert fetcher.calls == [registry_mod.REGISTRY_URL]


def test_install_with_agents_dry_run_never_imports_runtime(fake_houdini, monkeypatch):
    _stub_hython(monkeypatch)
    monkeypatch.setattr(deps_mod, "install_deps", lambda *a, **k: [])
    logged = []

    code = install_mod.install(agents=["claude", "codex"], dry_run=True, out=logged.append)

    assert code == 0
    assert any("claude" in line for line in logged)
    assert any("codex" in line for line in logged)


# --- uninstall --------------------------------------------------------


def test_uninstall_removes_package_json(fake_houdini):
    packages = fake_houdini / "packages"
    packages.mkdir()
    target = packages / houdini_package.PACKAGE_NAME
    target.write_text("{}")

    code = install_mod.uninstall(out=lambda *_: None)

    assert code == 0
    assert not target.exists()


def test_uninstall_dry_run_keeps_file(fake_houdini):
    packages = fake_houdini / "packages"
    packages.mkdir()
    target = packages / houdini_package.PACKAGE_NAME
    target.write_text("{}")

    code = install_mod.uninstall(dry_run=True, out=lambda *_: None)

    assert code == 0
    assert target.exists()


def test_uninstall_no_houdini_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setattr(houdini_package.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(houdini_package.Path, "home", staticmethod(lambda: tmp_path))

    assert install_mod.uninstall(out=lambda *_: None) == 0


def test_uninstall_purge_removes_data_dir(fake_houdini, monkeypatch, tmp_path):
    data_root = tmp_path / "data_root"
    data_root.mkdir()
    (data_root / "settings.json").write_text("{}")
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(data_root))

    code = install_mod.uninstall(purge=True, out=lambda *_: None)

    assert code == 0
    assert not data_root.exists()


def test_uninstall_purge_dry_run_keeps_data_dir(fake_houdini, monkeypatch, tmp_path):
    data_root = tmp_path / "data_root"
    data_root.mkdir()
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(data_root))

    code = install_mod.uninstall(purge=True, dry_run=True, out=lambda *_: None)

    assert code == 0
    assert data_root.exists()


# --- doctor -------------------------------------------------------------


def test_doctor_reports_missing_houdini(tmp_path, monkeypatch):
    monkeypatch.setattr(houdini_package.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(houdini_package.Path, "home", staticmethod(lambda: tmp_path))
    logged = []

    code = install_mod.doctor(out=logged.append)

    assert code == 0
    # Not "no Houdini found" — detection only looks for the prefs directory,
    # which Houdini itself only creates on first launch, not at install
    # time. The message says so, not "you don't have Houdini".
    assert any("no houdini preferences directory found" in line.lower() for line in logged)


def test_doctor_reports_hython_and_deps_state(fake_houdini, monkeypatch):
    _stub_hython(monkeypatch)
    logged = []

    code = install_mod.doctor(out=logged.append)

    assert code == 0
    joined = "\n".join(logged)
    assert "20.5" in joined
    assert "hython" in joined.lower()
    assert "not installed" in joined.lower()


def test_doctor_reports_deps_ready_when_installed(fake_houdini, monkeypatch):
    _stub_hython(monkeypatch)
    target = paths.deps_dir("py3.11")
    (target / "acp").mkdir(parents=True)
    (target / "houdini_agent_panel").mkdir(parents=True)
    logged = []

    install_mod.doctor(out=logged.append)

    assert any("ready" in line.lower() for line in logged)


# --- updating over ourselves ------------------------------------------------


def test_requirement_is_pinned_when_the_installer_came_from_elsewhere(tmp_path):
    """`uvx --from houdini-agent-panel==0.2.0 …` must put 0.2.0 in Houdini."""
    from houdini_agent_panel.install import _requirement_for

    assert _requirement_for(tmp_path / "deps" / "py3.13", "0.2.0") == (
        "houdini-agent-panel==0.2.0"
    )


def test_requirement_drops_the_pin_when_running_from_the_target_tree(monkeypatch, tmp_path):
    """Houdini's package file puts the deps tree ahead of site-packages for
    `hython` too, so `hython -m houdini_agent_panel install` — the documented
    update command — imports the panel from the tree it is about to
    overwrite. Pinning there asks pip for the version already installed, and
    the update silently does nothing. Seen on the Linux machine: site-packages
    at 0.2.3, deps tree stuck at 0.2.2 across repeated installs."""
    from houdini_agent_panel import install as install_mod

    target = tmp_path / "deps" / "py3.13"
    (target / "houdini_agent_panel").mkdir(parents=True)
    monkeypatch.setattr(
        install_mod, "__file__", str(target / "houdini_agent_panel" / "install.py")
    )

    assert install_mod._requirement_for(target, "0.2.2") == "houdini-agent-panel"


def test_reports_when_the_install_updated_the_version_that_was_running(
    fake_houdini, monkeypatch
):
    """docs/facts/houdini.md §15: `hython -m houdini_agent_panel install`
    runs from inside the very deps tree it is about to overwrite, so
    anything this run decided about the install ITSELF — the MCP
    interpreter, what went into the package file — was decided by the OLD
    version, even though the tree now holds the new one. The artist needs
    to be told to run the command again, in plain words, at the end where
    it will actually be read — not left to notice a stale `HAP_PYTHON`
    days later and assume it's a fresh bug (which is exactly what
    happened: see the team-lead's own report)."""
    _stub_hython(monkeypatch)
    monkeypatch.setattr(deps_mod, "install_deps", lambda *a, **k: ["ok"])
    monkeypatch.setattr(install_mod, "_panel_version", lambda: "0.4.3")
    monkeypatch.setattr(
        deps_mod, "installed_version", lambda target, name: "0.4.4"
    )
    logged = []

    code = install_mod.install(out=logged.append)

    assert code == 0
    notice = "\n".join(logged)
    assert "0.4.3" in notice and "0.4.4" in notice
    assert "again" in notice.lower() or "once more" in notice.lower()


def test_no_self_update_notice_when_installed_matches_running(fake_houdini, monkeypatch):
    """The ordinary case — reinstalling the version already running, or a
    normal update where this process's own `__version__` already reflects
    what just got installed — must stay quiet. A notice on every routine
    reinstall is noise that trains people to ignore it."""
    _stub_hython(monkeypatch)
    monkeypatch.setattr(deps_mod, "install_deps", lambda *a, **k: ["ok"])
    monkeypatch.setattr(install_mod, "_panel_version", lambda: "0.4.4")
    monkeypatch.setattr(
        deps_mod, "installed_version", lambda target, name: "0.4.4"
    )
    logged = []

    code = install_mod.install(out=logged.append)

    assert code == 0
    notice = "\n".join(logged)
    assert "updated itself" not in notice


def test_no_self_update_notice_on_a_dry_run(fake_houdini, monkeypatch):
    """A dry run never touches the target tree, so there is nothing to
    compare — `deps_mod.installed_version` must not even be consulted."""
    _stub_hython(monkeypatch)
    monkeypatch.setattr(deps_mod, "install_deps", lambda *a, **k: [])
    monkeypatch.setattr(install_mod, "_panel_version", lambda: "0.4.3")

    def _boom(target, name):
        raise AssertionError("installed_version must not be called on a dry run")

    monkeypatch.setattr(deps_mod, "installed_version", _boom)
    logged = []

    code = install_mod.install(dry_run=True, out=logged.append)

    assert code == 0
    assert "updated itself" not in "\n".join(logged)


def test_no_self_update_notice_with_skip_deps(fake_houdini, monkeypatch):
    """`--skip-deps` never runs `install_deps`, so nothing was updated and
    there is nothing to report — regardless of what's on disk."""
    _stub_hython(monkeypatch)

    def _boom(target, name):
        raise AssertionError("installed_version must not be called with --skip-deps")

    monkeypatch.setattr(deps_mod, "installed_version", _boom)
    logged = []

    code = install_mod.install(skip_deps=True, out=logged.append)

    assert code == 0
    assert "updated itself" not in "\n".join(logged)


def test_grant_user_access_is_a_no_op_when_not_elevated(monkeypatch, tmp_path):
    monkeypatch.setattr(install_mod, "_running_elevated", lambda: False)

    def explode(*args, **kwargs):  # pragma: no cover - must never be reached
        raise AssertionError("icacls must not run for an ordinary install")

    monkeypatch.setattr(install_mod.childproc, "run", explode)
    install_mod._grant_user_access(tmp_path, out=lambda _: None)


def test_grant_user_access_repairs_an_elevated_install(monkeypatch, tmp_path):
    """An elevated install leaves the tree owned by Administrators, which an
    artist's unelevated Houdini cannot read at all -- it took down Houdini's
    own help server on the Windows 11 VM, not just the panel."""
    monkeypatch.setattr(install_mod, "_running_elevated", lambda: True)
    monkeypatch.setenv("USERNAME", "artist")
    calls = []

    class _Ok:
        returncode = 0
        stderr = ""

    def record(argv, **kwargs):
        calls.append(argv)
        return _Ok()

    monkeypatch.setattr(install_mod.childproc, "run", record)
    lines: list[str] = []
    install_mod._grant_user_access(tmp_path, out=lines.append)

    assert calls == [
        ["icacls", str(tmp_path), "/grant", "artist:(OI)(CI)RX", "/T", "/C", "/Q"]
    ]
    assert any("granting artist" in line for line in lines)


def test_grant_user_access_reports_a_failure_without_failing_the_install(monkeypatch, tmp_path):
    monkeypatch.setattr(install_mod, "_running_elevated", lambda: True)
    monkeypatch.setenv("USERNAME", "artist")

    class _Failed:
        returncode = 1
        stderr = "Access is denied."

    monkeypatch.setattr(install_mod.childproc, "run", lambda argv, **kwargs: _Failed())
    lines: list[str] = []
    install_mod._grant_user_access(tmp_path, out=lines.append)

    assert any("Access is denied." in line for line in lines)
    assert any("non-administrator" in line for line in lines)


def test_grant_user_access_survives_a_missing_icacls(monkeypatch, tmp_path):
    monkeypatch.setattr(install_mod, "_running_elevated", lambda: True)
    monkeypatch.setenv("USERNAME", "artist")

    def missing(argv, **kwargs):
        raise OSError("no icacls here")

    monkeypatch.setattr(install_mod.childproc, "run", missing)
    lines: list[str] = []
    install_mod._grant_user_access(tmp_path, out=lines.append)

    assert any("could not run icacls" in line for line in lines)


def test_running_elevated_is_false_off_windows(monkeypatch):
    monkeypatch.setattr(install_mod.sys, "platform", "darwin")
    assert install_mod._running_elevated() is False


# --- installer python inside uv's cache (the ordinary `uvx` install) ------


def _uv_cache_python() -> Path:
    return Path("/opt/uv-cache/archive-v0/SCnAZuPVQXH2/bin/python")


def test_install_uv_cache_installer_python_prefers_plain_cpython(fake_houdini, monkeypatch):
    """`uvx --from houdini-agent-panel …` — the command the README hands
    out — runs from an unpacked archive in uv's cache. That interpreter
    exists when the install finishes and disappears the next time uv tidies
    up: measured on the owner's machine, an install recorded one and by the
    next Houdini launch the panel was logging "The Houdini MCP server's
    interpreter is gone", with no scene tools for the whole session.
    Houdini's own plain CPython lives inside the Houdini install and is
    preferred, exactly as for the `hython` and temp-directory cases."""
    _stub_hython(monkeypatch)
    monkeypatch.setenv("UV_CACHE_DIR", "/opt/uv-cache")
    monkeypatch.setattr(deps_mod, "install_deps", lambda *a, **k: ["ok"])
    monkeypatch.setattr(install_mod.sys, "executable", str(_uv_cache_python()))
    plain = Path("/fake/hfs20.5/python/bin/python3.11")
    monkeypatch.setattr(mcp_runtime, "find", lambda *a, **k: plain)
    logged = []

    code = install_mod.install(out=logged.append)

    assert code == 0
    payload = json.loads((fake_houdini / "packages" / houdini_package.PACKAGE_NAME).read_text("utf-8"))
    assert {"HAP_PYTHON": plain.as_posix()} in payload["env"]
    assert any("uv" in line and "cache" in line for line in logged)


def test_install_uv_cache_installer_python_is_still_recorded_without_a_fallback(
    fake_houdini, monkeypatch
):
    """Unlike the temp-directory case, this interpreter works TODAY. With no
    plain CPython to prefer, recording it beats skipping the Houdini
    entirely and leaving the artist with no panel at all — the panel
    already detects the interpreter going away later and says to re-run the
    installer."""
    _stub_hython(monkeypatch)
    monkeypatch.setenv("UV_CACHE_DIR", "/opt/uv-cache")
    monkeypatch.setattr(deps_mod, "install_deps", lambda *a, **k: ["ok"])
    monkeypatch.setattr(install_mod.sys, "executable", str(_uv_cache_python()))
    monkeypatch.setattr(mcp_runtime, "find", lambda *a, **k: None)
    logged = []

    code = install_mod.install(out=logged.append)

    assert code == 0
    payload = json.loads((fake_houdini / "packages" / houdini_package.PACKAGE_NAME).read_text("utf-8"))
    assert {"HAP_PYTHON": _uv_cache_python().as_posix()} in payload["env"]
    assert any("re-run" in line.lower() or "run the installer" in line.lower() for line in logged)

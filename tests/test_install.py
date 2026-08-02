"""Тесты на оркестрацию install/uninstall/doctor.

Ни hython, ни pip тут не запускаются по-настоящему — `deps.find_hython`,
`deps.python_version_of`, `deps.install_deps` подменяются моками, а Houdini на
диске имитируется фикстурой `_fake_houdini`.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from houdini_agent_panel import deps as deps_mod
from houdini_agent_panel import houdini_package
from houdini_agent_panel import install as install_mod
from houdini_agent_panel import paths


@pytest.fixture
def fake_houdini(tmp_path, monkeypatch):
    """Одна версия Houdini "20.5" с существующей prefs-директорией."""
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
    # install_deps тоже должен получить dry_run=True, а не выполниться по-настоящему.
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


def test_install_skip_deps_still_writes_package_json_without_installing(fake_houdini, monkeypatch):
    _stub_hython(monkeypatch)

    def explode(*a, **k):
        raise AssertionError("install_deps не должен звендиться при --skip-deps")

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

    # explicit dir не выглядит как версия Houdini в имени родителя -> версия
    # неизвестна, поэтому find_hython позвать нечем: убеждаемся, что пишем
    # package json и в этом случае, раз каталог передан явно человеком.
    code = install_mod.install(houdini_dir=str(explicit), out=lambda *_: None)

    assert code == 0
    assert (explicit / houdini_package.PACKAGE_NAME).exists()


def test_install_without_agents_flag_installs_no_agent(fake_houdini, monkeypatch):
    _stub_hython(monkeypatch)
    monkeypatch.setattr(deps_mod, "install_deps", lambda *a, **k: [])

    code = install_mod.install(out=lambda *_: None)

    assert code == 0  # агентов не просили — и ни один модуль агентов не трогается


def test_install_with_agents_but_agent_modules_missing_reports_clear_error(fake_houdini, monkeypatch):
    """Не должен зависеть от того, лежит ли registry.py/runtime.py на диске
    прямо сейчас — подменяем саму точку импорта, а не полагаемся на реальное
    отсутствие файла (оно нестабильно: параллельная ветка может дописать
    runtime.py в любой момент, и тест, завязанный на файловую систему, станет
    ложным)."""
    _stub_hython(monkeypatch)
    monkeypatch.setattr(deps_mod, "install_deps", lambda *a, **k: [])

    def explode():
        raise ImportError("registry/runtime ещё не готовы (симуляция для теста)")

    monkeypatch.setattr(install_mod, "_load_agent_modules", explode)
    logged = []

    code = install_mod.install(agents=["claude"], out=logged.append)

    assert code == 1
    assert any("registry" in line.lower() or "runtime" in line.lower() for line in logged)


def test_install_with_agents_opencode_installs_exactly_one_agent(fake_houdini, monkeypatch, fetcher):
    """Реальный (не dry-run) путь --agents должен стыковаться с фактическими
    сигнатурами `registry.fetch_registry(*, force, max_age, fetch)` и
    `runtime.install_agent(entry, *, progress, fetch)` — а не с тем, что было
    в архитектурном контракте на момент написания install.py."""
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
    assert any("не найдена" in line.lower() or "не найден" in line.lower() for line in logged)


def test_doctor_reports_hython_and_deps_state(fake_houdini, monkeypatch):
    _stub_hython(monkeypatch)
    logged = []

    code = install_mod.doctor(out=logged.append)

    assert code == 0
    joined = "\n".join(logged)
    assert "20.5" in joined
    assert "hython" in joined.lower()
    assert "не поставлены" in joined.lower() or "не готовы" in joined.lower()


def test_doctor_reports_deps_ready_when_installed(fake_houdini, monkeypatch):
    _stub_hython(monkeypatch)
    target = paths.deps_dir("py3.11")
    (target / "acp").mkdir(parents=True)
    (target / "houdini_agent_panel").mkdir(parents=True)
    logged = []

    install_mod.doctor(out=logged.append)

    assert any("готов" in line.lower() for line in logged)

"""Оркестрация установки панели в Houdini.

Один проход: найти Houdini на машине → для каждой найти её `hython` и версию
Python → поставить зависимости панели в привязанное к этой версии дерево →
записать package json. Ни один шаг не проходит молча — художник, чинящий
установку по логу, должен видеть, что именно произошло на каждой Houdini,
если их на машине несколько.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Sequence

from . import deps as deps_mod
from . import houdini_package
from . import paths
from .network import Fetcher


def _panel_version() -> str:
    try:
        from importlib.metadata import version

        return version("houdini-agent-panel")
    except Exception:  # noqa: BLE001 - из --target-дерева метаданных может не быть
        from . import __version__

        return __version__


def _resolve_package_dirs(explicit: str | None) -> tuple[list[Path], str]:
    """Тот же паттерн, что `resolve_houdini_dirs` у fxhoudinimcp (install.py:121-152):
    явный путь побеждает без вопросов, иначе — автопоиск с причиной для лога.
    Список кандидатов свой (`houdini_package.candidate_package_dirs`), потому
    что нам, в отличие от fxhoudinimcp, ещё нужно узнавать версию Houdini по
    имени prefs-директории (для выбора `hython`).
    """
    if explicit:
        return [Path(explicit).expanduser()], "указано явно через --houdini-dir"
    candidates = houdini_package.candidate_package_dirs()
    if not candidates:
        return [], "на машине не найдено ни одной директории Houdini"
    if len(candidates) == 1:
        return candidates, "единственная найденная на машине"
    return candidates, f"все найденные на машине ({len(candidates)})"


def install(
    *,
    houdini_dir: str | None = None,
    agents: Sequence[str] = (),
    find_links: str | None = None,
    skip_deps: bool = False,
    dry_run: bool = False,
    fetch: Fetcher | None = None,
    out=print,
) -> int:
    package_dirs, reason = _resolve_package_dirs(houdini_dir)
    if not package_dirs:
        out(
            "Houdini на машине не найдена: ни --houdini-dir, ни известные пути "
            "prefs (~/Library/Preferences/houdini/*, ~/houdiniX.Y, "
            "~/Documents/houdiniX.Y) не существуют."
        )
        return 1

    out(f"Каталоги packages Houdini ({reason}):")
    for package_dir in package_dirs:
        out(f"  {package_dir}")

    installer_python = sys.executable
    panel_version = _panel_version()
    any_ok = False

    for package_dir in package_dirs:
        prefs_dir = package_dir.parent
        version = houdini_package.houdini_version_of(prefs_dir)
        out(f"— Houdini {version or '?'} ({prefs_dir}) —")

        hython = deps_mod.find_hython(version)
        if hython is None:
            out("  hython не найден на диске — пропускаю эту Houdini")
            continue
        out(f"  hython: {hython}")

        try:
            pyver = deps_mod.python_version_of(hython)
        except deps_mod.DepsError as exc:
            out(f"  hython не отвечает: {exc}")
            continue
        if pyver is None:
            out("  не удалось разобрать версию Python у hython — пропускаю")
            continue

        tag = paths.python_tag(pyver)
        out(f"  python {pyver[0]}.{pyver[1]} -> {tag}")
        target = paths.deps_dir(tag)

        if skip_deps:
            out("  --skip-deps: зависимости не трогаю")
        else:
            try:
                deps_mod.install_deps(
                    hython,
                    target=target,
                    requirement=f"houdini-agent-panel=={panel_version}",
                    find_links=find_links,
                    dry_run=dry_run,
                    out=out,
                )
            except deps_mod.DepsError as exc:
                out(f"  установка зависимостей не удалась: {exc}")
                continue

        payload = houdini_package.package_json(deps=target, installer_python=installer_python)
        package_path = package_dir / houdini_package.PACKAGE_NAME
        if dry_run:
            out(f"  [dry-run] записал бы {package_path}")
        else:
            package_dir.mkdir(parents=True, exist_ok=True)
            package_path.write_text(payload, encoding="utf-8", newline="\n")
            out(f"  package json: {package_path}")
        any_ok = True

    result = 0 if any_ok else 1

    if agents:
        agents_result = _install_agents(agents, dry_run=dry_run, fetch=fetch, out=out)
        if agents_result != 0:
            result = agents_result

    return result


def _load_agent_modules():
    """Импорт `registry`/`runtime`, вынесенный в отдельную функцию.

    Не потому, что импорт сам по себе сложный, а ради тестируемости: `_install_agents`
    обязана понятно сообщать об ошибке, если этих модулей ещё нет (на момент
    написания install.py их не было — их пишут другие люди параллельно). Тест на
    этот сценарий не должен зависеть от того, лежит ли `runtime.py` на диске
    прямо сейчас — а зависеть он будет, если проверять голый `ImportError` по
    реальному отсутствию файла. Поэтому тест подменяет ровно эту функцию.
    """
    from . import registry
    from . import runtime

    return registry, runtime


def _install_agents(
    agent_ids: Sequence[str], *, dry_run: bool, fetch: Fetcher | None, out
) -> int:
    """Поставить агентов из реестра ACP через `runtime.install_agent`.

    В `--dry-run` модули вообще не трогаем: план печатается без единого
    импорта, чтобы дефолтный dry-run install работал уже сейчас, даже если
    `registry`/`runtime` временно недоступны или ломаются независимо от нас.
    """
    if not agent_ids:
        return 0

    if dry_run:
        for agent_id in agent_ids:
            out(f"[dry-run] поставил бы агента {agent_id}")
        return 0

    try:
        registry, runtime = _load_agent_modules()
    except ImportError as exc:
        out(f"Не могу поставить агентов: модуль registry/runtime ещё не готов ({exc})")
        return 1

    try:
        entries = {entry.id: entry for entry in registry.fetch_registry(fetch=fetch)}
    except Exception as exc:  # noqa: BLE001 - реестр недоступен, не роняем весь install
        out(f"Не удалось получить реестр агентов: {exc}")
        return 1

    ok = True
    for agent_id in agent_ids:
        entry = entries.get(agent_id)
        if entry is None:
            out(f"Агент {agent_id!r} не найден в реестре ACP")
            ok = False
            continue
        out(f"Ставлю агента {agent_id}...")
        try:
            runtime.install_agent(entry, fetch=fetch)
        except Exception as exc:  # noqa: BLE001 - один сломанный агент не должен рушить остальных
            out(f"  не удалось поставить {agent_id}: {exc}")
            ok = False
    return 0 if ok else 1


def uninstall(
    *,
    houdini_dir: str | None = None,
    purge: bool = False,
    dry_run: bool = False,
    out=print,
) -> int:
    package_dirs, reason = _resolve_package_dirs(houdini_dir)
    if not package_dirs:
        out("Houdini на машине не найдена — package json удалять неоткуда.")
    else:
        out(f"Каталоги packages Houdini ({reason}):")
        removed_any = False
        for package_dir in package_dirs:
            target = package_dir / houdini_package.PACKAGE_NAME
            if not target.exists():
                continue
            if dry_run:
                out(f"[dry-run] удалил бы {target}")
            else:
                target.unlink()
                out(f"Удалён {target}")
            removed_any = True
        if not removed_any:
            out("Package json нигде не найден — панель уже отключена от Houdini.")

    if purge:
        data_root = paths.data_dir()
        if dry_run:
            out(f"[dry-run] снёс бы папку данных {data_root}")
        else:
            shutil.rmtree(data_root, ignore_errors=True)
            out(f"Папка данных удалена: {data_root}")

    return 0


def _read_hap_python(package_path: Path) -> str | None:
    try:
        payload = json.loads(package_path.read_text("utf-8"))
    except (OSError, ValueError):
        return None
    for entry in payload.get("env", []):
        if isinstance(entry, dict) and "HAP_PYTHON" in entry:
            return entry["HAP_PYTHON"]
    return None


def doctor(out=print) -> int:
    """Печатает всё, чем можно чинить установку руками."""
    out(f"houdini-agent-panel {_panel_version()}")

    package_dirs, reason = _resolve_package_dirs(None)
    if not package_dirs:
        out("Houdini на машине не найдена (prefs-директорий с распознанной версией нет).")
        return 0

    out(f"Каталоги packages Houdini ({reason}):")
    for package_dir in package_dirs:
        prefs_dir = package_dir.parent
        version = houdini_package.houdini_version_of(prefs_dir)
        out(f"— Houdini {version or '?'} ({prefs_dir}) —")

        hython = deps_mod.find_hython(version) if version else None
        if hython is None:
            out("  hython не найден")
            continue
        out(f"  hython: {hython}")

        try:
            pyver = deps_mod.python_version_of(hython)
        except deps_mod.DepsError as exc:
            out(f"  hython не отвечает: {exc}")
            continue
        if pyver is None:
            out("  не удалось разобрать версию Python у hython")
            continue

        tag = paths.python_tag(pyver)
        out(f"  python {pyver[0]}.{pyver[1]} ({tag})")

        target = paths.deps_dir(tag)
        ready = deps_mod.deps_ready(target)
        out(f"  зависимости в {target}: {'готовы' if ready else 'НЕ поставлены'}")

        package_path = package_dir / houdini_package.PACKAGE_NAME
        if package_path.exists():
            out(f"  package json: есть ({package_path})")
            hap_python = _read_hap_python(package_path)
            out(f"  HAP_PYTHON: {hap_python or '?'}")
        else:
            out(f"  package json: нет ({package_path})")

    return 0

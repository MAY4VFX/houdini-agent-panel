"""CLI инсталлятора: ``python -m houdini_agent_panel <команда>``."""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from . import houdini_package
from . import install as install_mod
from . import paths


def _split_agents(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="houdini_agent_panel",
        description="Установка и диагностика панели-агента для Houdini.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    install_parser = subparsers.add_parser(
        "install", help="Поставить панель в найденные на машине Houdini"
    )
    install_parser.add_argument(
        "--houdini-dir",
        default=None,
        help="Явный путь к <prefs>/packages вместо автопоиска по всем версиям на машине",
    )
    install_parser.add_argument(
        "--agents",
        default=None,
        help="Агенты через запятую, например claude,codex. Без флага не ставится ни один",
    )
    install_parser.add_argument(
        "--find-links",
        default=None,
        help="Локальная директория с колёсами: искать пакеты сначала там",
    )
    install_parser.add_argument(
        "--offline",
        action="store_true",
        help="Не ходить на PyPI вообще (нужен --find-links со всеми зависимостями)",
    )
    install_parser.add_argument(
        "--skip-deps",
        action="store_true",
        help="Не ставить зависимости панели в Houdini-Python, только package json",
    )
    install_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Только показать план, ничего не менять на диске",
    )

    uninstall_parser = subparsers.add_parser("uninstall", help="Убрать панель из Houdini")
    uninstall_parser.add_argument("--houdini-dir", default=None)
    uninstall_parser.add_argument(
        "--purge", action="store_true", help="Также снести папку данных панели целиком"
    )
    uninstall_parser.add_argument("--dry-run", action="store_true")

    subparsers.add_parser(
        "houdini-package", help="Напечатать package json панели и куда его класть"
    )
    subparsers.add_parser("doctor", help="Проверить, что из установки нашлось на машине")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "install":
        return install_mod.install(
            houdini_dir=args.houdini_dir,
            agents=_split_agents(args.agents),
            find_links=args.find_links,
            offline=args.offline,
            skip_deps=args.skip_deps,
            dry_run=args.dry_run,
        )

    if args.command == "uninstall":
        return install_mod.uninstall(
            houdini_dir=args.houdini_dir,
            purge=args.purge,
            dry_run=args.dry_run,
        )

    if args.command == "houdini-package":
        target = paths.deps_dir()
        payload = houdini_package.package_json(deps=target, installer_python=sys.executable)
        print(f"Куда класть: <HOUDINI_USER_PREF_DIR>/packages/{houdini_package.PACKAGE_NAME}")
        print(payload)
        return 0

    if args.command == "doctor":
        return install_mod.doctor()

    parser.error(f"неизвестная команда: {args.command}")
    return 2  # parser.error сам завершает процесс, эта строка — для тестов на моке


if __name__ == "__main__":
    raise SystemExit(main())

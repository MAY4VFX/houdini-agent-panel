"""Installer CLI: ``python -m houdini_agent_panel <command>``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
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
        description="Install and diagnose the agent panel for Houdini.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    install_parser = subparsers.add_parser(
        "install", help="Install the panel into every Houdini install found on this machine"
    )
    install_parser.add_argument(
        "--houdini-dir",
        default=None,
        help="Explicit path to <prefs>/packages instead of auto-detecting all versions on the machine",
    )
    install_parser.add_argument(
        "--agents",
        default=None,
        help="Comma-separated agents, e.g. claude,codex. None are installed without this flag",
    )
    install_parser.add_argument(
        "--find-links",
        default=None,
        help="Local directory with wheels: look for packages there first",
    )
    install_parser.add_argument(
        "--offline",
        action="store_true",
        help="Never reach out to PyPI (requires --find-links with all dependencies)",
    )
    install_parser.add_argument(
        "--skip-deps",
        action="store_true",
        help="Do not install the panel's dependencies into Houdini's Python, only the package json",
    )
    install_parser.add_argument(
        "--dev",
        metavar="REPO",
        nargs="?",
        const=".",
        default=None,
        help=(
            "Point Houdini at a repository checkout instead of the installed package, "
            "so edits show up after reopening the panel tab (defaults to the current directory)"
        ),
    )
    install_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only show the plan, don't change anything on disk",
    )

    uninstall_parser = subparsers.add_parser("uninstall", help="Remove the panel from Houdini")
    uninstall_parser.add_argument("--houdini-dir", default=None)
    uninstall_parser.add_argument(
        "--purge", action="store_true", help="Also wipe the panel's entire data directory"
    )
    uninstall_parser.add_argument("--dry-run", action="store_true")

    subparsers.add_parser(
        "houdini-package", help="Print the panel's package json and where to put it"
    )
    subparsers.add_parser("doctor", help="Check what the installer was able to find on this machine")

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
            source=Path(args.dev).expanduser().resolve() if args.dev else None,
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
        print(f"Where to put it: <HOUDINI_USER_PREF_DIR>/packages/{houdini_package.PACKAGE_NAME}")
        print(payload)
        return 0

    if args.command == "doctor":
        return install_mod.doctor()

    parser.error(f"unknown command: {args.command}")
    return 2  # parser.error terminates the process itself; this line is only for mocked tests


if __name__ == "__main__":
    raise SystemExit(main())

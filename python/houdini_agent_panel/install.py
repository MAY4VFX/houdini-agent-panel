"""Orchestrates installing the panel into Houdini.

One pass: find every Houdini on the machine -> for each, find its `hython`
and Python version -> install the panel's dependencies into the tree bound to
that version -> write the package json. No step happens silently — an artist
fixing their install by reading the log needs to see exactly what happened
for each Houdini, if there's more than one on the machine.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Sequence

from . import deps as deps_mod
from . import mcp_runtime
from . import houdini_package
from . import paths
from .network import Fetcher


def _panel_version() -> str:
    """The version of the code that is running, which is what we reinstall.

    `__version__`, not `importlib.metadata` — the reverse of the obvious
    choice, and it was the obvious choice here first. Metadata describes a
    directory on disk; `__version__` is a line in the module Python actually
    imported. Those agree until a `--target` tree accumulates several
    `dist-info` directories (pip never removes the old ones), and then
    metadata answers with whichever the filesystem happens to list first.
    Measured on a machine updated four times: metadata said 0.1.6, the
    imported code said 0.2.0.

    The consequence was not a wrong number in a log. This value goes
    straight into `houdini-agent-panel==<version>` below, so a stale answer
    made the installer reinstall an OLD panel over a new one — an update
    that silently undid itself. `deps.prune_stale_metadata` now cleans the
    tree as well, but the version a running process reports about itself
    should never have depended on that.
    """
    from . import __version__

    return __version__


_PACKAGE = "houdini-agent-panel"
_FX_PACKAGE = "fxhoudinimcp"


def _requirement_for(target: Path, panel_version: str) -> str:
    """What to hand pip for this deps tree.

    Normally we pin: an installer run as `uvx --from
    houdini-agent-panel==0.2.0 …` should put 0.2.0 inside Houdini, so the
    CLI and the panel are the same build. That is right whenever the
    installer came from somewhere else.

    It is exactly wrong in the case that matters most. Houdini's package
    file puts this deps tree on `sys.path` ahead of site-packages, and that
    applies to `hython` too — so `hython -m houdini_agent_panel install`,
    the documented way to update, imports the panel FROM the tree it is
    about to overwrite. Pinning then asks pip for the version already there,
    and the update is a no-op that reports success. Measured on the Linux
    machine: site-packages had 0.2.3, the deps tree stayed on 0.2.2 through
    repeated installs, each one cheerfully reinstalling 0.2.2.

    So when the running module lives inside the target, we drop the pin and
    let `--upgrade` fetch the newest release. Anywhere else, the pin stands.
    """
    try:
        here = Path(__file__).resolve().parent.parent
    except OSError:
        return f"{_PACKAGE}=={panel_version}"
    try:
        same_tree = here == target.resolve()
    except OSError:
        same_tree = False
    return _PACKAGE if same_tree else f"{_PACKAGE}=={panel_version}"


def _mcp_python(
    hython: Path, pyver: tuple[int, int], target: Path, installer_python: str, *, out, dry_run: bool
) -> tuple[str, Path | None]:
    """The interpreter to record as `HAP_PYTHON`, and where it finds the server.

    The installer's own Python, unless that is Houdini's embedded one.
    `hython` works — `scene.FX_BOOTSTRAP` repairs the asyncio policy it
    installs — but it loads the whole of Houdini before running a line, and
    the MCP server is started once per conversation. Measured on 22.0.368:
    `hython -c pass` costs 8.9-16.5s and the entire server startup costs the
    same 8.5-14.6s, so all of it is the interpreter.

    Every Houdini also ships the stock CPython it is built on, without the
    wrapper — the same version as this deps tree, since that tree was
    installed by this Houdini's pip. On it the same server answers in 1.5s.
    No download, no virtualenv, no dependency on what the artist happens to
    have on PATH.

    Falls back to `hython` if that interpreter is missing or cannot import
    the server: slow beats broken, and the installer says which it chose.
    """
    if not mcp_runtime.is_houdini_python(installer_python):
        return installer_python, None
    if dry_run:
        out("  [dry-run] would look for Houdini's plain CPython for the MCP server")
        return installer_python, None
    found = mcp_runtime.find(hython, pyver, target, out=out)
    if found is None:
        out(
            "  Houdini's plain CPython not found — the MCP server will run on "
            "hython, which adds about 10s to opening a conversation"
        )
        return installer_python, None
    out(f"  MCP server interpreter: {found}")
    return str(found), target


def _resolve_package_dirs(explicit: str | None) -> tuple[list[Path], str]:
    """Same pattern as fxhoudinimcp's `resolve_houdini_dirs` (install.py:121-152):
    an explicit path wins unconditionally, otherwise fall back to auto-detection
    with a reason to log. Our candidate list is our own
    (`houdini_package.candidate_package_dirs`) because, unlike fxhoudinimcp, we
    also need to figure out the Houdini version from the prefs directory name
    (to pick the right `hython`).
    """
    if explicit:
        return [Path(explicit).expanduser()], "explicitly given via --houdini-dir"
    candidates = houdini_package.candidate_package_dirs()
    if not candidates:
        return [], "no Houdini directory found on this machine"
    if len(candidates) == 1:
        return candidates, "the only one found on this machine"
    return candidates, f"all found on this machine ({len(candidates)})"


def install(
    *,
    houdini_dir: str | None = None,
    agents: Sequence[str] = (),
    find_links: str | None = None,
    offline: bool = False,
    skip_deps: bool = False,
    source: Path | None = None,
    dry_run: bool = False,
    fetch: Fetcher | None = None,
    out=print,
) -> int:
    package_dirs, reason = _resolve_package_dirs(houdini_dir)
    if not package_dirs:
        out(
            "No Houdini found on this machine: neither --houdini-dir nor the known "
            "prefs paths (~/Library/Preferences/houdini/*, ~/houdiniX.Y, "
            "~/Documents/houdiniX.Y) exist."
        )
        return 1

    out(f"Houdini packages directories ({reason}):")
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
            out("  hython not found on disk — skipping this Houdini")
            continue
        out(f"  hython: {hython}")

        try:
            pyver = deps_mod.python_version_of(hython)
        except deps_mod.DepsError as exc:
            out(f"  hython did not respond: {exc}")
            continue
        if pyver is None:
            out("  could not parse hython's Python version — skipping")
            continue

        tag = paths.python_tag(pyver)
        out(f"  python {pyver[0]}.{pyver[1]} -> {tag}")
        if source is not None:
            out(f"  dev mode: Houdini will import {source / 'python'}")
        target = paths.deps_dir(tag)

        if skip_deps:
            out("  --skip-deps: not touching dependencies")
        else:
            requirement = _requirement_for(target, panel_version)
            if requirement == _PACKAGE:
                out("  running from this tree — installing the latest release, not a copy of itself")
            try:
                deps_mod.install_deps(
                    hython,
                    target=target,
                    requirement=requirement,
                    find_links=find_links,
                    offline=offline,
                    dry_run=dry_run,
                    out=out,
                )
            except deps_mod.DepsError as exc:
                out(f"  dependency install failed: {exc}")
                continue

        mcp_python, mcp_path = _mcp_python(
            hython, pyver, target, installer_python, out=out, dry_run=dry_run
        )
        payload = houdini_package.package_json(
            deps=target, installer_python=mcp_python, source=source, mcp_path=mcp_path
        )
        package_path = package_dir / houdini_package.PACKAGE_NAME
        if dry_run:
            out(f"  [dry-run] would write {package_path}")
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
    """Import of `registry`/`runtime`, pulled out into its own function.

    Not because the import itself is complicated, but for testability:
    `_install_agents` must report a clear error if these modules don't exist
    yet (at the time install.py was written, they didn't — other people were
    writing them in parallel). The test for this scenario shouldn't depend on
    whether `runtime.py` actually exists on disk right now — but it would, if
    we checked for a bare `ImportError` against the real absence of the file.
    So the test patches this exact function instead.
    """
    from . import registry
    from . import runtime

    return registry, runtime


def _install_agents(
    agent_ids: Sequence[str], *, dry_run: bool, fetch: Fetcher | None, out
) -> int:
    """Install agents from the ACP registry via `runtime.install_agent`.

    In `--dry-run` we don't touch the modules at all: the plan is printed
    without a single import, so that the default dry-run install already
    works today even if `registry`/`runtime` are temporarily unavailable or
    broken independently of this code.
    """
    if not agent_ids:
        return 0

    if dry_run:
        for agent_id in agent_ids:
            out(f"[dry-run] would install agent {agent_id}")
        return 0

    try:
        registry, runtime = _load_agent_modules()
    except ImportError as exc:
        out(f"Cannot install agents: registry/runtime module isn't ready yet ({exc})")
        return 1

    try:
        entries = {entry.id: entry for entry in registry.fetch_registry(fetch=fetch)}
    except Exception as exc:  # noqa: BLE001 - registry unavailable shouldn't sink the whole install
        out(f"Failed to fetch the agent registry: {exc}")
        return 1

    ok = True
    for agent_id in agent_ids:
        entry = entries.get(agent_id)
        if entry is None:
            out(f"Agent {agent_id!r} not found in the ACP registry")
            ok = False
            continue
        out(f"Installing agent {agent_id}...")
        try:
            runtime.install_agent(entry, fetch=fetch)
        except Exception as exc:  # noqa: BLE001 - one broken agent shouldn't take down the rest
            out(f"  failed to install {agent_id}: {exc}")
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
        out("No Houdini found on this machine — nothing to remove the package json from.")
    else:
        out(f"Houdini packages directories ({reason}):")
        removed_any = False
        for package_dir in package_dirs:
            target = package_dir / houdini_package.PACKAGE_NAME
            if not target.exists():
                continue
            if dry_run:
                out(f"[dry-run] would remove {target}")
            else:
                target.unlink()
                out(f"Removed {target}")
            removed_any = True
        if not removed_any:
            out("No package json found anywhere — the panel is already disconnected from Houdini.")

    if purge:
        data_root = paths.data_dir()
        if dry_run:
            out(f"[dry-run] would wipe the data directory {data_root}")
        else:
            shutil.rmtree(data_root, ignore_errors=True)
            out(f"Data directory removed: {data_root}")

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
    """Prints everything needed to fix the install by hand."""
    out(f"houdini-agent-panel {_panel_version()}")

    package_dirs, reason = _resolve_package_dirs(None)
    if not package_dirs:
        out("No Houdini found on this machine (no prefs directories with a recognized version).")
        return 0

    out(f"Houdini packages directories ({reason}):")
    for package_dir in package_dirs:
        prefs_dir = package_dir.parent
        version = houdini_package.houdini_version_of(prefs_dir)
        out(f"— Houdini {version or '?'} ({prefs_dir}) —")

        hython = deps_mod.find_hython(version) if version else None
        if hython is None:
            out("  hython not found")
            continue
        out(f"  hython: {hython}")

        try:
            pyver = deps_mod.python_version_of(hython)
        except deps_mod.DepsError as exc:
            out(f"  hython did not respond: {exc}")
            continue
        if pyver is None:
            out("  could not parse hython's Python version")
            continue

        tag = paths.python_tag(pyver)
        out(f"  python {pyver[0]}.{pyver[1]} ({tag})")

        target = paths.deps_dir(tag)
        ready = deps_mod.deps_ready(target)
        out(f"  dependencies in {target}: {'ready' if ready else 'NOT installed'}")

        package_path = package_dir / houdini_package.PACKAGE_NAME
        if package_path.exists():
            out(f"  package json: present ({package_path})")
            hap_python = _read_hap_python(package_path)
            out(f"  HAP_PYTHON: {hap_python or '?'}")
        else:
            out(f"  package json: missing ({package_path})")

    return 0

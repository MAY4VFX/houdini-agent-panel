"""Portable Node.js: find a system one or download our own.

4 of the 6 v1 agents (see design.md) install via npx, so Node is mandatory.
We never touch the system install — we either use what's already there and
recent enough, or download the official archive from nodejs.org into
`paths.node_dir()`.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence

from . import paths
from . import runtime
from .network import Fetcher, urlopen_fetch
from .runtime import ChecksumError, InstallError, Progress

#: Below this version we consider the system Node unusable (npx too old).
MIN_NODE = (20, 0, 0)
#: What we download if there's no system Node or it's too old.
NODE_VERSION = "22.14.0"

_VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)")


#: In-process cache keyed by `minimum`: whether a system Node exists (and
#: is recent enough) isn't going to change between the dozen times
#: `launch_spec()`/`ensure_node()` ask about it over the life of one Houdini
#: process, but finding out costs a `subprocess.run` each time. A `dict`
#: rather than a bare value so a `None` result (no usable system Node) is
#: distinguishable from "not looked up yet".
_system_node_cache: dict[tuple[int, int, int], Path | None] = {}


def reset_system_node_cache_for_tests() -> None:
    _system_node_cache.clear()


def find_system_node(minimum: tuple[int, int, int] = MIN_NODE) -> Path | None:
    """The system `node`, if it's on PATH and not older than `minimum`.

    Garbage in `node --version`'s output (wrong binary, a broken install) is
    treated as "no system Node", not a crash: the panel isn't obligated to
    understand exactly what's wrong with someone else's Node on disk.
    """
    if minimum in _system_node_cache:
        return _system_node_cache[minimum]
    result = _find_system_node_uncached(minimum)
    _system_node_cache[minimum] = result
    return result


def _find_system_node_uncached(minimum: tuple[int, int, int]) -> Path | None:
    found = shutil.which("node")
    if not found:
        return None
    path = Path(found)
    try:
        result = subprocess.run(
            [str(path), "--version"], capture_output=True, text=True, timeout=5
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    version = _parse_version(result.stdout)
    if version is None or version < minimum:
        return None
    return path


def _parse_version(text: str) -> tuple[int, int, int] | None:
    match = _VERSION_RE.match(text.strip())
    if not match:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def node_platform() -> tuple[str, str]:
    """("darwin", "arm64") — the names nodejs.org uses in its archives."""
    system = platform.system()
    os_name = {"Darwin": "darwin", "Linux": "linux", "Windows": "win"}.get(system)
    if os_name is None:
        raise InstallError(f"unknown platform: {system!r}")
    machine = platform.machine().lower()
    arch = {
        "arm64": "arm64",
        "aarch64": "arm64",
        "x86_64": "x64",
        "amd64": "x64",
    }.get(machine, machine)
    return os_name, arch


def dist_url(version: str = NODE_VERSION) -> str:
    os_name, arch = node_platform()
    ext = "zip" if os_name == "win" else "tar.gz"
    return f"https://nodejs.org/dist/v{version}/node-v{version}-{os_name}-{arch}.{ext}"


def shasums_url(version: str = NODE_VERSION) -> str:
    return f"https://nodejs.org/dist/v{version}/SHASUMS256.txt"


def _find_sha256(shasums_text: str, archive_name: str) -> str | None:
    """SHASUMS256.txt — lines of the form `<hex-sha256>  <filename>`."""
    for line in shasums_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == archive_name:
            return parts[0]
    return None


def _node_bin_path(root: Path, os_name: str | None = None) -> Path:
    """Path to the node binary inside a nodejs.org archive once extracted."""
    os_name = os_name or node_platform()[0]
    if os_name == "win":
        return root / "node.exe"
    return root / "bin" / "node"


def install_node(
    *, version: str = NODE_VERSION, progress: Progress | None = None, fetch: Fetcher | None = None
) -> Path:
    """Download the archive from nodejs.org, verify it against
    SHASUMS256.txt, extract it.

    Idempotent: if the version is already installed at
    `paths.node_dir()/<version>`, we don't touch the network at all. We
    never touch the system install — we only install into our own
    directory.
    """
    target_dir = paths.node_dir() / version
    node_bin = _node_bin_path(target_dir)
    if node_bin.exists():
        return node_bin

    fetch_impl = fetch or urlopen_fetch
    archive_url = dist_url(version)
    archive_name = archive_url.rsplit("/", 1)[-1]

    shasums_text = fetch_impl(shasums_url(version)).decode("utf-8")
    sha256 = _find_sha256(shasums_text, archive_name)
    if sha256 is None:
        raise ChecksumError(f"{archive_name}: no entry in SHASUMS256.txt")

    node_root = paths.node_dir()
    with tempfile.TemporaryDirectory(dir=node_root) as tmp_name:
        tmp_dir = Path(tmp_name)
        archive_path = tmp_dir / archive_name
        runtime.download_and_verify(archive_url, sha256, archive_path, progress=progress, fetch=fetch)

        extract_root = tmp_dir / "extracted"
        extract_root.mkdir()
        runtime.extract_archive(archive_path, extract_root)

        roots = list(extract_root.iterdir())
        if len(roots) != 1 or not roots[0].is_dir():
            raise InstallError(f"{archive_name}: unexpected archive contents")

        if target_dir.exists():
            shutil.rmtree(target_dir)
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(roots[0]), str(target_dir))

    result = _node_bin_path(target_dir)
    if not result.exists():
        raise InstallError(f"node binary not found in {target_dir} after extraction")
    return result


def ensure_node(*, progress: Progress | None = None, fetch: Fetcher | None = None) -> Path:
    """The system Node if it's good enough; otherwise, our own under
    `paths.node_dir()`.

    `fetch` wasn't in the original architecture contract
    (`docs/architecture.md` §5 lists `ensure_node(*, progress=None) -> Path`),
    but without it `install_agent` couldn't thread the test's `FakeFetcher`
    through `ensure_node -> install_node -> network`. The deviation was made
    both here and in every caller (`runtime.install_agent`,
    `runtime.launch_spec`).
    """
    system_node = find_system_node()
    if system_node is not None:
        return system_node
    return install_node(progress=progress, fetch=fetch)


def npx_argv(node_bin: Path, package: str, args: Sequence[str]) -> list[str]:
    """`[<node>, <npx-cli.js>, "--yes", package, *args]`.

    We call `npx-cli.js` directly with our own `node`, rather than the
    shell shim `npx`: the shim looks for `node` on PATH, and our agent
    environment is nearly empty (`docs/facts/acp-sdk.md` —
    `default_environment()`), so PATH might not even lead to our Node at
    all.
    """
    npx_cli = _npx_cli_path(node_bin)
    return [str(node_bin), str(npx_cli), "--yes", package, *args]


class NpxNotFoundError(RuntimeError):
    """There's no npm next to this Node. An explicit error instead of a dead end."""


def npx_cli_candidates(node_bin: Path) -> list[Path]:
    """Where `npx-cli.js` might live relative to a given `node`.

    There used to be a single guess here using `resolve()`, and it fell
    apart on exactly the most common case — Homebrew. `/opt/homebrew/bin/node`
    is a symlink into `Cellar/node/<version>/bin/node`, but Homebrew's npm
    isn't installed there — it's in `/opt/homebrew/lib/node_modules`. So
    `resolve()` led into a tree that has no npm at all, and we'd return a
    nonexistent path as if nothing was wrong.

    So now it's a list: via the symlink, via the real path, and both
    layouts — POSIX (`bin/../lib/node_modules`) and Windows (`node_modules`
    next to `node.exe`). Checking existence is the caller's job.
    """
    candidates: list[Path] = []
    for base in (node_bin, node_bin.resolve()):
        parent = base.parent
        # Windows: node.exe and node_modules live in the same directory.
        candidates.append(parent / "node_modules" / "npm" / "bin" / "npx-cli.js")
        # POSIX: <root>/bin/node and <root>/lib/node_modules.
        candidates.append(parent.parent / "lib" / "node_modules" / "npm" / "bin" / "npx-cli.js")

    unique: list[Path] = []
    for candidate in candidates:
        if candidate not in unique:
            unique.append(candidate)
    return unique


def _npx_cli_path(node_bin: Path) -> Path:
    """The first candidate that actually exists.

    If nothing was found, we fail right here with a clear message.
    Returning a nonexistent path would mean running `node <no-such-file>`:
    the process dies instantly and silently, while the panel is left
    waiting for a greeting from a corpse. That's exactly what it looked
    like for the artist — "Launching…" forever.
    """
    for candidate in npx_cli_candidates(node_bin):
        if candidate.is_file():
            return candidate
    raise NpxNotFoundError(
        f"no npm found next to {node_bin} (looked for npx-cli.js in: "
        + ", ".join(str(c) for c in npx_cli_candidates(node_bin))
        + ")"
    )


def npm_cache_dir() -> Path:
    """Where npm/npx keep their own cache — npx's downloaded packages live
    under `<this>/_npx/<hash>/node_modules/...`.

    `NPM_CONFIG_CACHE` (npm's own env var — npm itself accepts both the
    upper-case form and `npm_config_cache`, so both are checked here)
    overrides the default when set; cheap to read from this process's own
    environment, no need to spawn `npm config get cache` for it. The
    default itself is npm's own documented one (POSIX `~/.npm`, Windows
    `%LocalAppData%\\npm-cache`) — measured directly on this Mac and on a
    real Linux machine (`npm config get cache` answered `~/.npm` on both);
    the Windows branch is npm's documented default, not independently
    verified — no Windows machine in this project (`self_update.py`'s own
    docstring already notes the same gap for a different reason).
    """
    for name in ("NPM_CONFIG_CACHE", "npm_config_cache"):
        override = os.environ.get(name)
        if override:
            return Path(override)
    if platform.system() == "Windows":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "npm-cache"
    return Path.home() / ".npm"


def find_cached_npx_binary(scope: str, name_prefix: str, binary_name: str) -> Path | None:
    """The newest, actually-runnable `binary_name` inside any npx cache
    entry whose package name (within `scope`) starts with `name_prefix` —
    e.g. `scope="@anthropic-ai"`, `name_prefix="claude-agent-sdk-"` finds
    Claude's own platform-specific package (`claude-agent-sdk-darwin-
    arm64`, `-linux-x64`, ...) without needing to know which platform
    suffix this machine uses.

    npx keys its cache by content hash, not by package name — measured on
    two real machines (this Mac, a Linux box): the SAME package showed up
    under three and two different, unpredictable hash directories
    respectively (stale entries left by earlier resolutions — a fresh
    `claude-agent-acp` launch, a panel update, anything that re-resolves
    the dependency tree). So this is a glob across every hash directory,
    never a single guessed path. And — same discipline as `mcp_runtime.
    find()` for the fx server's own interpreter search — a path existing
    proves nothing; only actually running `--version` does, so every
    candidate is verified before being trusted, not just the first match.
    Ties (more than one candidate actually runs) broken by mtime, newest
    first: nothing else distinguishes them.
    """
    cache_root = npm_cache_dir() / "_npx"
    if not cache_root.is_dir():
        return None
    candidates: list[Path] = []
    try:
        hash_dirs = list(cache_root.iterdir())
    except OSError:
        return None
    for hash_dir in hash_dirs:
        scope_dir = hash_dir / "node_modules" / scope
        if not scope_dir.is_dir():
            continue
        try:
            entries = list(scope_dir.iterdir())
        except OSError:
            continue
        for entry in entries:
            if not entry.name.startswith(name_prefix):
                continue
            candidate = entry / binary_name
            if candidate.is_file():
                candidates.append(candidate)
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    for candidate in candidates:
        if _runs(candidate):
            return candidate
    return None


def _runs(binary: Path) -> bool:
    """Does `binary --version` actually work? The only question that
    counts — an npx cache entry can be a half-written download, a build
    for the wrong architecture that happened to land in the right-looking
    directory, or simply stale enough that the OS no longer trusts it."""
    try:
        result = subprocess.run(
            [str(binary), "--version"], capture_output=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def path_with_dirs(dirs: Sequence[str | Path], base: str | None = None) -> str:
    """`base` with `dirs` in front of it, in order, each appearing once.

    Split out of `path_with_node` because `client.py` has to redo this
    against a DIFFERENT base than the one available here — see
    `client._agent_path`: the PATH the agent should run with is the
    artist's login-shell PATH, which this module has no business spawning a
    shell to read.
    """
    existing = base if base is not None else os.environ.get("PATH", "")
    wanted = [str(directory) for directory in dirs]
    parts = list(wanted)
    for part in existing.split(os.pathsep):
        if part and part not in parts:
            parts.append(part)
    return os.pathsep.join(parts)


def path_with_node(node_bin: Path, base: str | None = None) -> str:
    """A PATH with our `node`'s directory prepended.

    This isn't for us, it's for npm itself: `npx-cli.js` spawns child
    processes with the `node` command and looks for it on PATH. Without
    this, the agent on a machine without Node dies before its first byte,
    and the client only sees "connection closed" — there's practically
    nothing to diagnose from the panel's side.

    We prepend to whatever PATH already exists rather than replacing it:
    the agent may need other tools from the machine too, and we're not
    going to take them away from it.
    """
    return path_with_dirs([node_bin.parent], base)

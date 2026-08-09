"""Installing, launching, and removing agents: download, verify sha256, extract.

The riskiest code in the project, for one reason — it writes to disk
whatever came in from the internet. Two rules keep this safe: the checksum
is verified BEFORE anything takes up a permanent spot
(`download_and_verify`), and extraction rejects any path inside the archive
that points outside the target directory (`extract_archive`, Zip Slip).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from . import paths
from .network import Fetcher, stream_fetch
from .registry import AgentEntry, BinaryDistribution, NpxDistribution, platform_key
from .settings import CustomAgent

_MANIFEST_NAME = "manifest.json"


class Progress(Protocol):
    def __call__(self, done: int, total: int | None, note: str) -> None: ...


@dataclass(frozen=True)
class LaunchSpec:
    command: str
    args: list[str]
    env: dict[str, str]  # added to the process environment, not a replacement for it
    #: Directories that must come FIRST on the agent's PATH, whatever the
    #: rest of it ends up being. Only `env["PATH"]` would be simpler, and it
    #: was that at first — but the value written here can only be built from
    #: Houdini's own PATH, and the PATH the agent should actually run with is
    #: the artist's login-shell one (`shellenv.py`), which is not known until
    #: `client.do_start`. Handing over the directories instead of a finished
    #: string lets that composition happen where both halves exist, without
    #: this module spawning a shell of its own. `env["PATH"]` is still set as
    #: well, so a spec spawned directly (a script, a diagnostic) remains
    #: correct on its own.
    path_prepend: tuple[str, ...] = ()


class InstallError(RuntimeError):
    """Anything that prevented installing an agent or Node. The reason is in the text."""


class ChecksumError(InstallError):
    """The downloaded file's sha256 didn't match what was expected.

    In this case `download_and_verify` leaves nothing on disk — no
    intermediate file, and certainly no final one.
    """


# --- on-disk manifest: how `is_installed`/`installed_version` check what's
# already installed without touching the network or the panel's global settings -----------


def _manifest_path_readonly(agent_id: str) -> Path:
    """Path to the manifest for READING — without the side effect of creating the agent's folder.

    `paths.agent_dir()` does a `mkdir` on every access (`paths._sub`).
    `is_installed`/`installed_version` are called for EVERY agent in the
    registry when rendering the "Agents" screen — going through
    `paths.agent_dir()` there would create an empty folder on disk for
    every agent that isn't installed yet.
    """
    return paths.agents_dir() / agent_id / _MANIFEST_NAME


#: In-process cache of `agent_id -> installed version (or None)`.
#: `is_installed`/`installed_version` are called for EVERY agent in the
#: registry on EVERY repaint of the "Agents" screen — without this, that's
#: one disk read + JSON parse per agent per repaint, for state that only
#: ever changes from inside this same module (`_write_manifest`,
#: `uninstall_agent`), so those two are the only places that need to keep
#: the cache honest.
_manifest_cache: dict[str, str | None] = {}


def reset_manifest_cache_for_tests() -> None:
    _manifest_cache.clear()


def _write_manifest(entry: AgentEntry, *, kind: str) -> None:
    payload = {
        "agent_id": entry.id,
        "version": entry.version,
        "kind": kind,
        "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    manifest = paths.agent_dir(entry.id) / _MANIFEST_NAME  # mkdir is warranted here — we're writing
    manifest.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
    _manifest_cache[entry.id] = entry.version


def installed_version(agent_id: str) -> str | None:
    if agent_id in _manifest_cache:
        return _manifest_cache[agent_id]

    manifest = _manifest_path_readonly(agent_id)
    version: str | None = None
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text("utf-8"))
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            raw_version = data.get("version")
            version = str(raw_version) if raw_version else None
    _manifest_cache[agent_id] = version
    return version


def is_installed(entry: AgentEntry) -> bool:
    """Install idempotency: the same version is already there — don't download it again."""
    return installed_version(entry.id) == entry.version


# --- downloading with sha256 verification ------------------------------------


class _HashingWriter:
    """A file-object proxy: writes to disk and computes sha256 over the stream.

    This way `download_and_verify` doesn't read the archive twice (once to
    write it, once to hash it) — the sha256 accumulates as chunks arrive
    from `network.stream_fetch`.
    """

    def __init__(self, raw, hasher: "hashlib._Hash") -> None:
        self._raw = raw
        self._hasher = hasher

    def write(self, chunk: bytes) -> int:
        self._hasher.update(chunk)
        return self._raw.write(chunk)


def _stream_to_file(url: str, dest_file, *, fetch: Fetcher | None, progress: Progress | None) -> int:
    """Download `url` into the open file object `dest_file`.

    With a `fetch` passed in (tests) — the whole body via `Fetcher`, which
    is fine for small test fixtures and, more importantly, the only way a
    test can substitute the network at all (`network.py`: "mocking one
    protocol is cheaper than patching urllib across six modules" — true for
    streamed archives too). Without `fetch` (production) — the real
    `network.stream_fetch`, so we don't have to hold tens of megabytes of an
    agent/Node archive in memory all at once.
    """
    if fetch is not None:
        payload = fetch(url)
        dest_file.write(payload)
        if progress is not None:
            progress(len(payload), len(payload), url.rsplit("/", 1)[-1])
        return len(payload)
    return stream_fetch(url, dest_file, progress=progress)


def download_and_verify(
    url: str,
    sha256: str,
    dest: Path,
    *,
    progress: Progress | None = None,
    fetch: Fetcher | None = None,
) -> Path:
    """Download `url` into `dest`, verifying sha256, atomically.

    We write to a temp file NEXT TO `dest` (`<dest>.part`) and rename it
    into place only once the checksum matches. An interrupted download or a
    mismatched hash leaves neither `dest` nor the `.part` file on disk —
    otherwise the panel would consider half an archive an "installed"
    agent.
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    hasher = hashlib.sha256()
    try:
        with tmp.open("wb") as raw:
            writer = _HashingWriter(raw, hasher)
            _stream_to_file(url, writer, fetch=fetch, progress=progress)
        digest = hasher.hexdigest()
        if digest.lower() != sha256.lower():
            raise ChecksumError(f"{url}: sha256 {digest} did not match expected {sha256}")
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(dest)
    return dest


# --- safe extraction (Zip Slip) ---------------------------------------


def _safe_member_path(dest: Path, member_name: str) -> Path:
    """Resolves an archive member's path inside `dest`, rejecting anything outside it.

    `Path.joinpath` itself "teleports" the result to an absolute path if one
    of the components is absolute
    (`Path("/a") / "/etc/passwd" == Path("/etc/passwd")`) — so checking
    "the result lives inside dest" after `resolve()` catches both `..`
    traversal and absolute member paths with the same piece of code.
    """
    # BOTH sides resolved, or the comparison is between a real path and a
    # possibly-symlinked one and rejects perfectly safe archives. macOS makes
    # this easy to hit: anything under /var is really /private/var, so a
    # member named plainly "opencode" failed the containment check and no
    # binary agent could install at all. Found by running a first-install on
    # a simulated fresh machine — the guard was right about traversal and
    # wrong about the ground it stood on.
    dest = dest.resolve()
    member_path = (dest / member_name).resolve()
    if member_path != dest and dest not in member_path.parents:
        raise InstallError(f"archive contains an unsafe path: {member_name!r}")
    return member_path


def _apply_zip_permissions(path: Path, info: "zipfile.ZipInfo") -> None:
    mode = (info.external_attr >> 16) & 0o777
    if mode:
        try:
            path.chmod(mode)
        except OSError:
            pass


def _extract_zip(archive: Path, dest: Path) -> None:
    with zipfile.ZipFile(archive) as zf:
        for info in zf.infolist():
            if info.filename.endswith("/"):
                _safe_member_path(dest, info.filename).mkdir(parents=True, exist_ok=True)
                continue
            target = _safe_member_path(dest, info.filename)
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info) as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
            _apply_zip_permissions(target, info)


def _extract_tar(archive: Path, dest: Path) -> None:
    with tarfile.open(archive, "r:*") as tf:
        for member in tf.getmembers():
            if member.issym() or member.islnk():
                # We don't create or follow symlinks inside the archive —
                # the simplest way to close off Zip Slip via links: a
                # target outside dest never ends up on disk at all. Our
                # agents/Node don't need this: npx_argv() calls npx-cli.js
                # directly, bypassing the bin/npx symlink shims that the
                # nodejs.org archive carries.
                continue
            target = _safe_member_path(dest, member.name)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            if not member.isfile():
                continue  # we don't need devices/fifos from the archive
            target.parent.mkdir(parents=True, exist_ok=True)
            extracted = tf.extractfile(member)
            if extracted is None:
                continue
            with extracted as src, target.open("wb") as out:
                shutil.copyfileobj(src, out)
            if member.mode:
                try:
                    target.chmod(member.mode)
                except OSError:
                    pass


def extract_archive(archive: Path, dest: Path) -> None:
    """tar.gz/tgz/zip. Paths with `..` or absolute ones are rejected (Zip Slip)."""
    archive = Path(archive)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith(".zip"):
        _extract_zip(archive, dest)
    elif name.endswith((".tar.gz", ".tgz", ".tar")):
        _extract_tar(archive, dest)
    else:
        raise InstallError(f"{archive}: unknown archive format")


# --- installing/removing/launching agents --------------------------------------


def _resolve_cmd(root: Path, cmd: str) -> Path:
    """`"./opencode"` — relative to the root of the extracted archive."""
    cleaned = cmd[2:] if cmd.startswith("./") else cmd
    cleaned = cleaned.replace("\\", "/")  # some registry entries are Windows-flavored
    return root / cleaned


def _make_executable(path: Path) -> None:
    if sys.platform == "win32":
        return
    try:
        mode = path.stat().st_mode
        path.chmod(mode | 0o111)
    except OSError:
        pass


def _npx_launch_spec(node_bin: Path, dist: NpxDistribution) -> LaunchSpec:
    """The launch command for an npx agent, together with a PATH to our Node.

    Calling `npx-cli.js` with our own `node` isn't enough, even though it
    looks like it should be: `npx-cli.js` itself spawns child processes with
    the `node` command, meaning it looks for it on PATH. On a machine
    without Node this fails instantly and silently — the agent process dies
    before its first byte, and the client only sees
    `ConnectionError: Connection closed` (verified by running with a
    stripped-down PATH). So our Node's directory gets added to the agent
    process's PATH — and only that: the system is never touched, exactly as
    the design promises.
    """
    from . import node as node_module

    args = node_module.npx_argv(node_bin, dist.package, dist.args)
    env = dict(dist.env)
    env["PATH"] = node_module.path_with_node(node_bin, env.get("PATH"))
    return LaunchSpec(
        command=args[0], args=args[1:], env=env, path_prepend=(str(node_bin.parent),)
    )


def _binary_launch_spec(version_dir: Path, dist: BinaryDistribution) -> LaunchSpec:
    return LaunchSpec(command=str(_resolve_cmd(version_dir, dist.cmd)), args=list(dist.args), env={})


def install_agent(
    entry: AgentEntry,
    *,
    progress: Progress | None = None,
    fetch: Fetcher | None = None,
    settings=None,
) -> LaunchSpec:
    """Install an agent and return a ready-to-use launch command.

    Idempotent: if the version in `entry` is already installed, there's no
    network, no disk, just `launch_spec` right away. An npx agent only
    needs `ensure_node()` and a manifest — the package itself is downloaded
    by `npx` on first launch, so the freshly-built spec below must carry the
    proxy too, or the very install this function exists for runs uncovered.
    A binary one downloads the archive, verifies its sha256, extracts it
    into `<data>/agents/<id>/<version>`, and sets +x.
    """
    if is_installed(entry):
        return launch_spec(entry, settings=settings)

    key = platform_key()
    dist = entry.distribution_for(key)
    if dist is None:
        # We pass the key explicitly using the same platform_key() call as
        # above: our own (not the registry module's) platform_key is
        # overridden in tests via runtime.platform_key, and
        # unavailable_reason() must agree on the same value rather than
        # re-reading the real platform.
        raise InstallError(entry.unavailable_reason(key))

    if isinstance(dist, NpxDistribution):
        from . import node as node_module

        node_bin = node_module.ensure_node(progress=progress, fetch=fetch)
        _write_manifest(entry, kind="npx")
        spec = _npx_launch_spec(node_bin, dist)
        return LaunchSpec(
            command=spec.command,
            args=spec.args,
            env=_with_proxy(spec.env, settings),
            path_prepend=spec.path_prepend,
        )

    if not dist.sha256:
        # Some registry entries have no sha256 (§ registry.py,
        # BinaryDistribution.sha256). The panel refuses to install a binary
        # it has nothing to verify against — an explicit error beats a
        # silent hole in the integrity of whatever runs on the artist's
        # machine.
        raise InstallError(f"{entry.name}: no sha256 in the registry to verify against, install refused")

    version_dir = paths.agent_dir(entry.id) / entry.version
    archive_name = dist.archive.rsplit("/", 1)[-1]
    agents_root = paths.agents_dir()
    with tempfile.TemporaryDirectory(dir=agents_root) as tmp_name:
        tmp_dir = Path(tmp_name)
        archive_path = tmp_dir / archive_name
        download_and_verify(dist.archive, dist.sha256, archive_path, progress=progress, fetch=fetch)

        extract_root = tmp_dir / "extracted"
        extract_root.mkdir()
        extract_archive(archive_path, extract_root)

        if version_dir.exists():
            shutil.rmtree(version_dir)
        version_dir.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(extract_root), str(version_dir))

    cmd_path = _resolve_cmd(version_dir, dist.cmd)
    _make_executable(cmd_path)
    _write_manifest(entry, kind="binary")
    spec = _binary_launch_spec(version_dir, dist)
    return LaunchSpec(
        command=spec.command,
        args=spec.args,
        env=_with_proxy(spec.env, settings),
        path_prepend=spec.path_prepend,
    )


def uninstall_agent(agent_id: str) -> None:
    """Wipes the agent's folder entirely. Leaves its neighbors (other agents) alone.

    We compute the path via `paths.agents_dir() / agent_id`, not via
    `paths.agent_dir(agent_id)`: the latter creates the directory as a side
    effect (`paths._sub` does a `mkdir` on every access) — destroying
    something it just created itself would be strange, and for an agent
    that never existed it would leave an empty folder on disk.
    """
    directory = paths.agents_dir() / agent_id
    if directory.exists():
        shutil.rmtree(directory)
    _manifest_cache.pop(agent_id, None)


def _with_proxy(env: dict[str, str], settings_obj) -> dict[str, str]:
    """Studio proxy underneath, the agent's own env on top.

    Order matters: a per-agent `HTTPS_PROXY` is a deliberate choice about
    that agent, and the panel-wide setting is only a default for agents
    that said nothing.
    """
    from . import proxy as proxy_module
    from . import settings as settings_module

    resolved = settings_module.load() if settings_obj is None else settings_obj
    merged = proxy_module.child_env(resolved)
    merged.update(env)
    return merged


def _with_oauth_tokens(env: dict[str, str], agent_id: str, settings_obj) -> dict[str, str]:
    """A token minted by a terminal-auth command that prints it once and
    writes it nowhere else — `settings.py::Settings.agent_oauth_tokens`'s
    own docstring has the report (Claude's `setup-token`, docs/facts/
    acp-sdk.md §21: no credentials file, so the token is gone unless the
    panel captures and re-supplies it itself, every launch). Injected the
    same way `_with_proxy` injects the studio proxy — underneath the
    agent's own env, which wins if it already set the same variable a
    different way (e.g. an artist who also exported it in their shell
    profile, or picked `ANTHROPIC_API_KEY` instead — see `_no_methods_
    advice`'s own rewrite for why the two are not interchangeable).
    """
    from . import settings as settings_module

    resolved = settings_module.load() if settings_obj is None else settings_obj
    tokens = getattr(resolved, "agent_oauth_tokens", {}).get(agent_id, {})
    merged = dict(tokens)
    merged.update(env)
    return merged


def launch_spec(entry: AgentEntry, *, settings=None) -> LaunchSpec:
    """The launch command for an already-installed agent.

    For npx, `ensure_node()` here is cheap: the agent was installed via
    `install_agent`, which already provisioned Node (system or our own) —
    this just finds it again, no network involved, since it's already on
    disk.
    """
    key = platform_key()
    dist = entry.distribution_for(key)
    if dist is None:
        # We pass the key explicitly using the same platform_key() call as
        # above: our own (not the registry module's) platform_key is
        # overridden in tests via runtime.platform_key, and
        # unavailable_reason() must agree on the same value rather than
        # re-reading the real platform.
        raise InstallError(entry.unavailable_reason(key))

    if isinstance(dist, NpxDistribution):
        from . import node as node_module

        node_bin = node_module.ensure_node()
        spec = _npx_launch_spec(node_bin, dist)
    else:
        if not is_installed(entry):
            raise InstallError(f"{entry.name} {entry.version}: not installed")
        version_dir = paths.agent_dir(entry.id) / entry.version
        spec = _binary_launch_spec(version_dir, dist)

    env = _with_oauth_tokens(_with_proxy(spec.env, settings), entry.id, settings)
    return LaunchSpec(
        command=spec.command, args=spec.args, env=env, path_prepend=spec.path_prepend
    )


def custom_launch_spec(agent: CustomAgent, *, settings=None) -> LaunchSpec:
    """"Custom Agent" — the command as-is, with no install step or versions."""
    return LaunchSpec(
        command=agent.command,
        args=list(agent.args),
        env=_with_proxy(dict(agent.env), settings),
    )

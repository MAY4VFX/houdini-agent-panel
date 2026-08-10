"""AGENTS.md (and a CLAUDE.md link to it) written next to the scene.

The agent is spawned as a plain subprocess with its cwd set to `$HIP`
(`scene.hip_dir()`) — it has no way to know on its own that it is running
inside a live Houdini session rather than an ordinary checkout, or that
scene changes go through `fxhoudini`'s MCP tools rather than a `.hip` file
on disk. This was issue #41's open question ("system prompt: you're inside
Houdini") — decided: a static `AGENTS.md`, the convention several agents
already read from their cwd on their own.

Two rules this module exists to enforce:

- **Never overwrite.** If the artist already has their own `AGENTS.md` (or
  `CLAUDE.md`) in the scene folder — for their own agents, unrelated to
  this panel — it is never touched, not even if it happens to look like
  ours.
- **Never write outside a real project folder.** `scene.hip_dir()` falls
  back to `$HOME` for an unsaved scene; writing there would drop files into
  the artist's actual home directory. Callers pass `scene.real_hip_dir()`
  here, which is `None` in exactly that case, and this module adds its own
  belt-and-suspenders check against `$HOME`, the system temp root, and the
  filesystem root, in case a real saved scene ever happens to sit in one of
  those.
"""

from __future__ import annotations

import platform
import shutil
import tempfile
from pathlib import Path

from .logbook import logger

_log = logger("houdini_agent_panel.context_files")

AGENTS_MD_NAME = "AGENTS.md"
CLAUDE_MD_NAME = "CLAUDE.md"

#: English on purpose — this is read by agents/models, not the artist.
AGENTS_MD_CONTENT = """\
# Working inside Houdini

You are running inside a live Houdini session, launched from a chat panel built into
Houdini itself. Your working directory is this scene's own folder.

## How to change the scene

Use the `fxhoudini` MCP tools for everything: creating and wiring nodes, setting
parameters, inspecting geometry, and so on. Do not edit the `.hip` file directly, do not
write a script to run under `hython`, and do not try to `import hou` — none of that is
available in this process. The MCP tools are the only way in.

## Houdini is live, and someone is watching

An artist has this session open on screen; every tool call you make happens there
immediately. Only take destructive actions — deleting nodes, `new_scene`, overwriting
files on disk — when asked to. Saving the scene is a deliberate action you take when
asked, never a side effect of something else.

## Never cook PDG/TOP synchronously

A synchronous TOP cook (e.g. `cookWorkItems`) runs on Houdini's main thread and freezes
the entire UI — including this panel — until every work item finishes. The normal cancel
action is frozen along with everything else, since it needs that same thread. Don't start
a long cook or render this way; if one is genuinely needed, warn the artist first and let
them start it.

## Batch your calls

Each `fxhoudini` tool call costs real time on top of the work it does, because Houdini's
object model only runs on the main thread. Prefer one batched call (e.g. building several
nodes at once) over many small ones — see that server's own tool instructions for
specifics.
"""


def ensure_context_files(directory: str | None) -> None:
    """Write `AGENTS.md` (and a `CLAUDE.md` symlink to it) into `directory`
    if they aren't already there. Safe to call on every boot and every
    scene change: both files are written at most once, existing ones are
    never touched, and a falsy `directory` — `scene.real_hip_dir()`'s
    answer for an unsaved scene — is a deliberate no-op, not an error.

    Never raises: this runs from the panel's boot path and from the
    `hip_dir` change watcher, neither of which has anything useful to do
    with a write failure beyond logging it.
    """
    if not directory:
        _log.info("context files: no saved scene yet, nothing written")
        return
    try:
        target_dir = Path(directory)
        if _is_unsafe_write_dir(target_dir):
            _log.warning(
                "context files: refusing to write into %s "
                "(resolves to home, a temp directory, or the filesystem root)",
                target_dir,
            )
            return
        _write_agents_md(target_dir)
        if (target_dir / AGENTS_MD_NAME).exists():
            _write_claude_md_link(target_dir)
    except Exception:  # noqa: BLE001 - see docstring: never raise from here
        _log.warning("context files: unexpected failure", exc_info=True)


def _is_unsafe_write_dir(directory: Path) -> bool:
    """Belt-and-suspenders on top of `scene.real_hip_dir()` already being
    `None` for an unsaved scene: refuses `$HOME` itself, anywhere under the
    system temp root, and the filesystem root, in case a real saved scene
    ever happens to resolve to one of those.
    """
    try:
        resolved = directory.resolve()
    except OSError:
        resolved = directory
    if resolved == Path.home().resolve():
        return True
    if resolved.parent == resolved:  # a path is its own parent only at the root
        return True
    temp_root = Path(tempfile.gettempdir())
    try:
        temp_root = temp_root.resolve()
    except OSError:
        pass
    if resolved == temp_root or temp_root in resolved.parents:
        return True
    return False


def _write_agents_md(directory: Path) -> None:
    target = directory / AGENTS_MD_NAME
    if target.exists():
        _log.info("context files: %s already exists, leaving it alone", target)
        return
    try:
        target.write_text(AGENTS_MD_CONTENT, encoding="utf-8")
    except OSError:
        _log.warning("context files: could not write %s", target, exc_info=True)
        return
    _log.info("context files: wrote %s", target)


def _write_claude_md_link(directory: Path) -> None:
    """`CLAUDE.md` -> `AGENTS.md`, relative, right next to it.

    Claude reads `CLAUDE.md` specifically, not `AGENTS.md`; a symlink keeps
    one file as the source of truth. Other agents (Gemini CLI reads
    `GEMINI.md`, others have their own conventions) are deliberately out of
    scope here — only Claude was asked for.
    """
    target = directory / CLAUDE_MD_NAME
    if target.exists() or target.is_symlink():
        _log.info("context files: %s already exists, leaving it alone", target)
        return
    try:
        target.symlink_to(AGENTS_MD_NAME)
    except OSError:
        # Windows without symlink privilege is the expected reason this
        # fails, but this project has no Windows machine to confirm it on
        # (same gap `_conpty_windows.py` and `node.py::npm_cache_dir`
        # already flag) — so this branch is taken on ANY OSError, not just
        # a Windows-shaped one, and falls back to a plain copy rather than
        # leaving the artist without a CLAUDE.md at all. A copy won't stay
        # in sync with a hand-edited AGENTS.md later, but Claude still gets
        # the same briefing on this write.
        try:
            shutil.copyfile(directory / AGENTS_MD_NAME, target)
        except OSError:
            _log.warning(
                "context files: could not create %s (symlink and copy both failed)",
                target,
                exc_info=True,
            )
            return
        _log.info(
            "context files: wrote %s as a copy of %s (symlink failed, likely no "
            "symlink privilege on %s)",
            target,
            AGENTS_MD_NAME,
            platform.system(),
        )
        return
    _log.info("context files: linked %s -> %s", target, AGENTS_MD_NAME)

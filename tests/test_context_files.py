"""Tests for `context_files.py` — AGENTS.md/CLAUDE.md written next to a
saved scene. No Houdini involved: callers pass a plain directory string,
exactly what `scene.real_hip_dir()` returns.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from houdini_agent_panel import context_files


@pytest.fixture(autouse=True)
def _tempdir_does_not_overlap_tmp_path(monkeypatch, tmp_path):
    """On macOS `tempfile.gettempdir()` resolves under the same
    `/var/folders/.../T` tree pytest's own `tmp_path` comes from (both
    trace back to `$TMPDIR`) — which would make every ordinary test
    directory below look "unsafe" too, since it IS a temp directory,
    without this module doing anything wrong. Point the default elsewhere
    so only the tests that deliberately build an unsafe directory (which
    override this themselves) exercise that check."""
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path.parent / "unrelated-tmp-root"))


# --- the no-op cases ---------------------------------------------------------


def test_none_directory_writes_nothing(caplog):
    """`scene.real_hip_dir()` returns `None` for an unsaved scene — this
    must be a deliberate no-op, not a fall-through to some other path.

    There is nowhere on disk to check for absence — `None` never becomes a
    `Path` at all, by design (`ensure_context_files`'s own `if not
    directory: return`, before anything else runs). What CAN be checked,
    and wasn't: that this is the specific branch actually taken, not some
    other path that also happens not to write anything and not to raise —
    `ensure_context_files`'s own `except Exception` would swallow a real
    regression here just as quietly. The early-return branch logs its own
    reason; its absence would mean something else ran instead.
    """
    caplog.set_level("INFO", logger="houdini_agent_panel.context_files")
    context_files.ensure_context_files(None)
    assert any("nothing written" in r.message for r in caplog.records), (
        "the early-return branch for a falsy directory must be the one that ran"
    )


def test_empty_string_directory_writes_nothing(tmp_path, caplog):
    """Same reasoning as `test_none_directory_writes_nothing` — `""` is
    just as falsy as `None` to `ensure_context_files`'s own check, and
    deserves the same proof it took the SAME branch, not a coincidence."""
    caplog.set_level("INFO", logger="houdini_agent_panel.context_files")
    context_files.ensure_context_files("")
    assert any("nothing written" in r.message for r in caplog.records), (
        "the early-return branch for a falsy directory must be the one that ran"
    )


@pytest.mark.parametrize(
    "unsafe",
    ["home", "temp_root", "temp_child", "filesystem_root"],
)
def test_refuses_unsafe_directories(tmp_path, monkeypatch, unsafe):
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.setattr("tempfile.gettempdir", lambda: str(tmp_path / "tmp"))
    (tmp_path / "tmp" / "sub").mkdir(parents=True)

    directory = {
        "home": tmp_path / "home",
        "temp_root": tmp_path / "tmp",
        "temp_child": tmp_path / "tmp" / "sub",
        "filesystem_root": Path(tmp_path.anchor),
    }[unsafe]

    context_files.ensure_context_files(str(directory))

    assert not (directory / context_files.AGENTS_MD_NAME).exists()
    assert not (directory / context_files.CLAUDE_MD_NAME).exists()


# --- AGENTS.md ---------------------------------------------------------------


def test_writes_agents_md_into_a_real_project_directory(tmp_path):
    scene_dir = tmp_path / "shots" / "shot010"
    scene_dir.mkdir(parents=True)

    context_files.ensure_context_files(str(scene_dir))

    written = scene_dir / context_files.AGENTS_MD_NAME
    assert written.is_file()
    text = written.read_text("utf-8")
    assert "fxhoudini" in text
    assert "hou" in text
    assert "cookWorkItems" in text


def test_does_not_overwrite_an_existing_agents_md(tmp_path):
    scene_dir = tmp_path / "shots" / "shot010"
    scene_dir.mkdir(parents=True)
    existing = scene_dir / context_files.AGENTS_MD_NAME
    existing.write_text("the artist's own instructions for their own agents", "utf-8")

    context_files.ensure_context_files(str(scene_dir))

    assert existing.read_text("utf-8") == "the artist's own instructions for their own agents"


def test_second_call_is_a_no_op(tmp_path):
    """Boot and every `hip_dir` change both call this — repeated calls on
    the same directory must not rewrite anything."""
    scene_dir = tmp_path / "shots" / "shot010"
    scene_dir.mkdir(parents=True)

    context_files.ensure_context_files(str(scene_dir))
    first_mtime = (scene_dir / context_files.AGENTS_MD_NAME).stat().st_mtime_ns

    context_files.ensure_context_files(str(scene_dir))
    second_mtime = (scene_dir / context_files.AGENTS_MD_NAME).stat().st_mtime_ns

    assert first_mtime == second_mtime


# --- CLAUDE.md ----------------------------------------------------------------


def test_claude_md_is_a_symlink_to_agents_md(tmp_path):
    scene_dir = tmp_path / "shots" / "shot010"
    scene_dir.mkdir(parents=True)

    context_files.ensure_context_files(str(scene_dir))

    link = scene_dir / context_files.CLAUDE_MD_NAME
    assert link.is_symlink()
    assert link.read_text("utf-8") == (scene_dir / context_files.AGENTS_MD_NAME).read_text("utf-8")


def test_does_not_overwrite_an_existing_claude_md(tmp_path):
    scene_dir = tmp_path / "shots" / "shot010"
    scene_dir.mkdir(parents=True)
    existing = scene_dir / context_files.CLAUDE_MD_NAME
    existing.write_text("the artist's own CLAUDE.md", "utf-8")

    context_files.ensure_context_files(str(scene_dir))

    assert existing.read_text("utf-8") == "the artist's own CLAUDE.md"
    assert not existing.is_symlink()


def test_claude_md_still_created_when_agents_md_was_already_there(tmp_path):
    """The pre-existing AGENTS.md is left untouched, but a missing CLAUDE.md
    still gets linked to it — same convention file, whoever wrote it."""
    scene_dir = tmp_path / "shots" / "shot010"
    scene_dir.mkdir(parents=True)
    (scene_dir / context_files.AGENTS_MD_NAME).write_text("artist's own", "utf-8")

    context_files.ensure_context_files(str(scene_dir))

    link = scene_dir / context_files.CLAUDE_MD_NAME
    assert link.is_symlink()
    assert link.read_text("utf-8") == "artist's own"


def test_falls_back_to_a_copy_when_symlink_creation_fails(tmp_path, monkeypatch):
    """Simulates Windows without symlink privilege: `Path.symlink_to`
    raises `OSError`, and the panel must not lose CLAUDE.md entirely."""
    scene_dir = tmp_path / "shots" / "shot010"
    scene_dir.mkdir(parents=True)

    def _raise(self, *args, **kwargs):
        raise OSError("simulated: no privilege to create symbolic links")

    monkeypatch.setattr(Path, "symlink_to", _raise)

    context_files.ensure_context_files(str(scene_dir))

    link = scene_dir / context_files.CLAUDE_MD_NAME
    assert link.is_file()
    assert not link.is_symlink()
    assert link.read_text("utf-8") == (scene_dir / context_files.AGENTS_MD_NAME).read_text("utf-8")


def test_survives_symlink_and_copy_both_failing(tmp_path, monkeypatch):
    scene_dir = tmp_path / "shots" / "shot010"
    scene_dir.mkdir(parents=True)

    monkeypatch.setattr(
        Path, "symlink_to", lambda self, *a, **k: (_ for _ in ()).throw(OSError("no symlinks"))
    )
    monkeypatch.setattr(
        "shutil.copyfile", lambda *a, **k: (_ for _ in ()).throw(OSError("no copy either"))
    )

    context_files.ensure_context_files(str(scene_dir))  # must not raise

    assert not (scene_dir / context_files.CLAUDE_MD_NAME).exists()
    # AGENTS.md itself is unaffected by CLAUDE.md's failure.
    assert (scene_dir / context_files.AGENTS_MD_NAME).is_file()


# --- never raises --------------------------------------------------------------


def test_unwritable_directory_does_not_raise(tmp_path):
    """A write failure (permissions, a read-only mount) must be logged, not
    propagated — this runs from the panel's boot path."""
    missing = tmp_path / "does" / "not" / "exist"
    context_files.ensure_context_files(str(missing))
    assert not missing.exists()

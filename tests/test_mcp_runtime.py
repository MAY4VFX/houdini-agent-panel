"""Picking the interpreter that runs the fx MCP server.

The artist asked why the server takes so long to start, and whether the
port scan was to blame. Measured instead, on Houdini 22.0.368: `hython -c
pass` costs 8.9/10.3/16.5s across three runs and the complete server
startup costs 8.5/10.9/14.6s — the same. None of the wait is the server,
the ports or the protocol; all of it is Houdini's embedded interpreter
loading Houdini.

They then asked whether Houdini ships a plain Python too, and whether we
could avoid depending on anything external. It does, and we can: the stock
CPython every build is compiled against, same version as the deps tree,
starts in 0.09s (0.05s on Linux) and runs the same server in 1.5s.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from houdini_agent_panel import mcp_runtime


# --- is_ephemeral ------------------------------------------------------


def test_is_ephemeral_true_for_a_path_under_the_system_temp_dir():
    """The real incident: `uvx --no-cache` unpacks its whole run into a
    directory under the OS temp root and deletes it the instant the
    command exits. `sys.executable` at that point is a path inside it —
    e.g. on macOS `/var/folders/.../T/.tmpXXXXXX/archive-v0/<hash>/bin/python`."""
    temp_root = Path(tempfile.gettempdir())
    doomed = temp_root / ".tmpdsyxSk" / "archive-v0" / "7gsPQrjI8Kdrz5MEiY1ZF" / "bin" / "python"

    assert mcp_runtime.is_ephemeral(doomed) is True


def test_is_ephemeral_false_for_an_ordinary_installed_python():
    assert mcp_runtime.is_ephemeral("/opt/homebrew/bin/python3.12") is False
    assert mcp_runtime.is_ephemeral("/usr/bin/python3") is False


def test_is_ephemeral_resolves_symlinks_before_comparing(monkeypatch, tmp_path):
    """macOS's own TMPDIR (and /tmp itself) are symlinks into /private —
    comparing unresolved paths would silently never match on the one
    platform the bug was actually found on."""
    real_temp = tmp_path / "real_temp"
    real_temp.mkdir()
    link = tmp_path / "temp_link"
    link.symlink_to(real_temp)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(link))

    inside_via_real_path = real_temp / "some" / "python"

    assert mcp_runtime.is_ephemeral(inside_via_real_path) is True


def test_hython_is_recognised_on_every_platform_spelling():
    assert mcp_runtime.is_houdini_python("/opt/hfs22.0/bin/hython") is True
    assert mcp_runtime.is_houdini_python("C:/Program Files/H22/bin/hython.exe") is True
    assert mcp_runtime.is_houdini_python("/opt/hfs22.0/python/bin/python3.13") is False
    assert mcp_runtime.is_houdini_python("/opt/homebrew/bin/python3") is False


def test_candidates_cover_the_layout_of_every_platform():
    """`hython` always lives in `$HFS/bin`, so `$HFS` is its grandparent.
    All three layouts are offered regardless of the host platform — the
    wrong ones simply do not exist on disk."""
    linux = mcp_runtime.plain_python_candidates(Path("/opt/hfs22.0/bin/hython"), (3, 13))
    assert Path("/opt/hfs22.0/python/bin/python3.13") in linux

    windows = mcp_runtime.plain_python_candidates(
        Path("C:/Program Files/Side Effects Software/Houdini 20.5.445/bin/hython.exe"), (3, 11)
    )
    assert any(c.name == "python.exe" and "python311" in c.as_posix() for c in windows)

    mac = mcp_runtime.plain_python_candidates(
        Path(
            "/Applications/Houdini/Houdini22.0.368/Frameworks/Houdini.framework"
            "/Versions/22.0/Resources/bin/hython"
        ),
        (3, 13),
    )
    assert (
        Path(
            "/Applications/Houdini/Houdini22.0.368/Frameworks/Python.framework"
            "/Versions/3.13/bin/python3.13"
        )
        in mac
    )


def test_find_returns_the_first_interpreter_that_can_import_the_server(tmp_path, monkeypatch):
    hython = tmp_path / "hfs22.0" / "bin" / "hython"
    hython.parent.mkdir(parents=True)
    hython.touch()
    plain = tmp_path / "hfs22.0" / "python" / "bin" / "python3.13"
    plain.parent.mkdir(parents=True)
    plain.touch()
    monkeypatch.setattr(mcp_runtime, "_imports", lambda python, module, path: True)

    assert mcp_runtime.find(hython, (3, 13), tmp_path / "deps", out=lambda *_: None) == plain


def test_find_refuses_an_interpreter_that_cannot_import_the_server(tmp_path, monkeypatch):
    """A path that exists proves nothing. Recording an interpreter that
    cannot start the server would trade slow for broken."""
    hython = tmp_path / "hfs22.0" / "bin" / "hython"
    hython.parent.mkdir(parents=True)
    hython.touch()
    plain = tmp_path / "hfs22.0" / "python" / "bin" / "python3.13"
    plain.parent.mkdir(parents=True)
    plain.touch()
    monkeypatch.setattr(mcp_runtime, "_imports", lambda python, module, path: False)
    said: list[str] = []

    assert mcp_runtime.find(hython, (3, 13), tmp_path / "deps", out=said.append) is None
    assert any("cannot import" in line for line in said)


def test_find_returns_none_when_houdini_ships_no_plain_python(tmp_path):
    hython = tmp_path / "hfs22.0" / "bin" / "hython"
    hython.parent.mkdir(parents=True)
    hython.touch()

    assert mcp_runtime.find(hython, (3, 13), tmp_path / "deps", out=lambda *_: None) is None


def test_the_import_check_is_told_where_the_tree_is_and_nothing_else(tmp_path, monkeypatch):
    """Houdini's plain CPython has no packages of its own, so the check must
    point it at the deps tree — and must drop the `PYTHONPATH` the installer
    inherited, or the answer describes somebody else's interpreter. An
    earlier version of this module got exactly that wrong and declared an
    environment holding nothing but pip to be working."""
    monkeypatch.setenv("PYTHONPATH", "/inherited/from/houdini")
    monkeypatch.setenv("PYTHONHOME", "/houdini")
    monkeypatch.setenv("PATH", "/usr/bin")
    seen: dict = {}

    def record(argv, **kwargs):
        seen.update(kwargs.get("env") or {})
        return subprocess.CompletedProcess(argv, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(mcp_runtime.subprocess, "run", record)

    assert mcp_runtime._imports(Path("/x/python3.13"), "fxhoudinimcp", Path("/deps/py3.13")) is True
    assert seen["PYTHONPATH"] == "/deps/py3.13", "the tree was not pointed at"
    assert "PYTHONHOME" not in seen
    assert seen["PATH"] == "/usr/bin", "the rest of the environment was thrown away too"


def test_the_fx_version_is_readable_from_the_tree(tmp_path):
    """Not used to pin a second copy any more — there is only one copy now —
    but the deps tree's own version is still what the panel reports."""
    from houdini_agent_panel.deps import installed_version

    (tmp_path / "fxhoudinimcp-2.10.0.dist-info").mkdir()

    assert installed_version(tmp_path, "fxhoudinimcp") == "2.10.0"
    assert installed_version(tmp_path, "nothing-here") is None


# --- is_uv_cache -------------------------------------------------------
#
# `is_ephemeral`'s docstring used to name `~/.cache/uv/archive-v0/<hash>/
# bin/python` as the GOOD case — "which survives, and everything works".
# It does not. uv reclaims its cache (`uv cache prune`, `uv cache clean`,
# and its own housekeeping) and the archive goes with it. Measured on the
# owner's own machine, 2026-08-31: an install recorded
# `/Users/may/.cache/uv/archive-v0/SCnAZuPVQXH2-Rz6laYiw/bin/python` as
# HAP_PYTHON, and by the next Houdini launch the panel logged "The Houdini
# MCP server's interpreter is gone" — no scene tools for that whole
# session. `uvx --from houdini-agent-panel …` is the install command the
# README hands out, so this is the DEFAULT path, not a corner.
#
# Kept separate from `is_ephemeral` on purpose, because the remedy differs:
# a temp-directory interpreter is gone before Houdini ever starts and must
# never be recorded, while a uv-cache one works today and is worth
# recording if there is nothing better — see `install._mcp_python`.
#
# Paths below are deliberately OUTSIDE the system temp directory: under it
# they would answer through `is_ephemeral`'s own rule and prove nothing.


def test_is_uv_cache_true_for_a_python_inside_uvs_cache(monkeypatch):
    monkeypatch.setenv("UV_CACHE_DIR", "/opt/uv-cache")

    assert mcp_runtime.is_uv_cache("/opt/uv-cache/archive-v0/SCnAZ/bin/python") is True


def test_is_uv_cache_true_for_uvs_default_location(monkeypatch):
    """Nobody sets `UV_CACHE_DIR`; the default is the path the owner\'s own
    broken install actually recorded."""
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: Path("/Users/fake")))

    assert mcp_runtime.is_uv_cache("/Users/fake/.cache/uv/archive-v0/SCnAZ/bin/python") is True


def test_is_uv_cache_false_for_an_unrelated_path_containing_uv(monkeypatch):
    """A folder of the artist\'s own that merely has `uv` in its path is not
    uv\'s cache — only the cache root counts."""
    monkeypatch.setenv("UV_CACHE_DIR", "/opt/uv-cache")

    assert mcp_runtime.is_uv_cache("/Users/fake/projects/uv/bin/python") is False


def test_is_uv_cache_false_for_an_ordinary_installed_python(monkeypatch):
    monkeypatch.setenv("UV_CACHE_DIR", "/opt/uv-cache")

    assert mcp_runtime.is_uv_cache("/opt/homebrew/bin/python3.12") is False


def test_a_uv_cache_python_is_not_called_ephemeral(monkeypatch):
    """The two are different problems with different remedies, and
    `install._mcp_python` branches on which one it is."""
    monkeypatch.setenv("UV_CACHE_DIR", "/opt/uv-cache")

    assert mcp_runtime.is_ephemeral("/opt/uv-cache/archive-v0/SCnAZ/bin/python") is False


def test_is_uv_cache_true_for_the_symlink_uv_actually_puts_there(tmp_path, monkeypatch):
    """The path uv hands out is a SYMLINK out of the cache.

    Measured on the owner's machine while verifying the first version of
    this fix, which did nothing at all:

        /Users/may/.cache/uv/archive-v0/<hash>/bin/python
          -> /Users/may/.local/share/uv/python/cpython-3.12.12-.../bin/python3.12

    `HAP_PYTHON` records the path as given — the one inside the cache, the
    one that disappears when uv prunes — so that is the path this question
    is about. Resolving it first walks straight out of the cache and
    answers False, which is how a fix with four passing tests still shipped
    doing nothing.
    """
    cache = tmp_path / "uv-cache"
    (cache / "archive-v0" / "hash" / "bin").mkdir(parents=True)
    real = tmp_path / "elsewhere" / "bin"
    real.mkdir(parents=True)
    (real / "python3.12").write_text("#!/bin/sh\n")
    link = cache / "archive-v0" / "hash" / "bin" / "python"
    link.symlink_to(real / "python3.12")
    monkeypatch.setenv("UV_CACHE_DIR", str(cache))

    assert mcp_runtime.is_uv_cache(link) is True

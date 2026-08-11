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

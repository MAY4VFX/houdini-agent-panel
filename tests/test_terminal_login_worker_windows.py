"""`TerminalLoginWorker`'s Windows/ConPTY branch — driven from macOS/Linux
by SUBSTITUTION, not by running on Windows (there is no Windows machine in
this project). Two things are faked, deliberately, at two different
levels:

- `terminal_login_mod._PTY_AVAILABLE` and `.platform.system` are
  monkeypatched so `TerminalLoginWorker.__init__` computes `_use_conpty
  = True` exactly the way it would on a real Windows machine with no
  POSIX pty — this is the same flag `work()` branches on for real.
- `terminal_login_mod._conpty_windows` (the whole module reference) is
  replaced with a fake exposing `.spawn`/`.ConPtyError`/`._CONPTY_
  AVAILABLE`, so `work()`'s ConPTY branch runs against a scripted fake
  process instead of ever calling into `ctypes`/`kernel32` — the actual
  `ctypes` wrapping logic has its own, separate test coverage in
  `tests/test_conpty_windows.py`, exercised against a fake `kernel32`.
  This file is one layer up: does `TerminalLoginWorker.work()` correctly
  drive whatever `_conpty_windows.spawn()`/`.ConPtyReader` return, using
  the exact same read loop, marker detection, and redaction the POSIX
  paths already have tests for.

What this DOES prove: `TerminalLoginWorker`'s own logic (branch
selection, the read loop, `send_line`, cleanup) is correct given a
ConPTY-shaped process. What this does NOT prove: that `_conpty_windows.
spawn()` itself works against a real Windows `kernel32.dll` — no test in
this repository can prove that without a Windows machine.
"""

from __future__ import annotations

import logging
import platform
import sys
import threading
import time

import pytest

from houdini_agent_panel.client import TerminalAuth
from houdini_agent_panel.ui import _conpty_windows
from houdini_agent_panel.ui import terminal_login as terminal_login_mod
from houdini_agent_panel.ui.terminal_login import TerminalLoginWorker


def _wait_until(app, condition, *, timeout_ms: int = 5000) -> None:
    from PySide6 import QtTest

    elapsed = 0
    step = 20
    while not condition() and elapsed < timeout_ms:
        app.processEvents()
        QtTest.QTest.qWait(step)
        elapsed += step
    assert condition(), "condition did not become true in time"


def _force_windows_conpty_path(monkeypatch) -> None:
    """Makes `TerminalLoginWorker.__init__` compute `_use_conpty = True`
    on THIS (POSIX) test machine — the substitution the module docstring
    promises. `_PTY_AVAILABLE` is real and `True` here (this machine has
    a real `pty` module); forcing it `False` is what stands in for
    "this is Windows, there is no POSIX pty at all"."""
    monkeypatch.setattr(terminal_login_mod, "_PTY_AVAILABLE", False)
    monkeypatch.setattr(terminal_login_mod.platform, "system", lambda: "Windows")


class _ScriptedConPtyProcess:
    """A hand-scripted stand-in for `_conpty_windows.ConPtyProcess`,
    shaped after the same scripts `test_terminal_login_worker.py` uses
    for the plain-pipe/POSIX-pty branches (`_CLAUDE_LIKE_SCRIPT` and
    friends) — except there is no real Windows process to spawn, so the
    "script" is data plus a callback instead of actual Python source.

    `.read()` BLOCKS (a short poll loop, not a real OS block) until
    either more output is queued or the process is marked finished —
    mirroring how a real ConPTY child genuinely blocks on `ReadFile`
    while it's sitting at an input prompt. Without this, a worker
    thread reading an initially-short queue would hit "EOF" immediately
    after the prompt line, exiting before `send_line` ever got a chance
    to answer it — exactly the shape a REAL blocked child does NOT have.
    """

    def __init__(self, initial_chunks: list[bytes]) -> None:
        self._lock = threading.Lock()
        self._queue: list[bytes] = list(initial_chunks)
        self._finished = False
        self._on_write = None
        self.pid = 424242
        self.written = b""
        self._exit_code = 0
        self.closed = False
        self.terminated = False

    def read(self, size: int = 4096) -> bytes:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with self._lock:
                if self._queue:
                    return self._queue.pop(0)
                if self._finished:
                    return b""
            time.sleep(0.01)
        return b""  # safety net — never actually hit in a passing test

    def write(self, data: bytes) -> None:
        with self._lock:
            self.written += data
            if self._on_write is not None:
                self._queue.extend(self._on_write(data))
                self._finished = True

    def poll(self):
        with self._lock:
            return None if (self._queue or not self._finished) else self._exit_code

    def wait(self, timeout=None) -> int:
        with self._lock:
            self._finished = True
            self._queue.clear()
        return self._exit_code

    def terminate(self) -> None:
        self.terminated = True
        with self._lock:
            self._finished = True
            self._queue.clear()

    def close(self) -> None:
        self.closed = True


class _FakeConPtyModule:
    """Stands in for the WHOLE `_conpty_windows` module reference
    `terminal_login.py` holds — `ConPtyReader`/`ConPtyError` are the
    REAL classes (no reason to fake them: `ConPtyReader` only needs a
    `.read(n) -> bytes` object, which `_ScriptedConPtyProcess` already
    is, and re-testing it here would just duplicate `test_conpty_windows
    .py`'s own coverage). Only `.spawn` is scripted, returning whatever
    process the test constructed instead of touching `ctypes`.
    """

    ConPtyReader = _conpty_windows.ConPtyReader
    ConPtyError = _conpty_windows.ConPtyError

    def __init__(self, process: "_ScriptedConPtyProcess", *, available: bool = True) -> None:
        self._process = process
        self._CONPTY_AVAILABLE = available
        self.spawn_calls: list[tuple] = []

    def spawn(self, command, args, *, env, cwd, **kwargs):
        self.spawn_calls.append((command, list(args), cwd))
        return self._process


# --- flag selection: _use_pty / _use_conpty are mutually exclusive -----


def test_use_conpty_is_selected_over_use_pty_when_posix_pty_is_unavailable(monkeypatch, tmp_path):
    _force_windows_conpty_path(monkeypatch)
    ta = TerminalAuth(command=sys.executable, args=[], env={})
    worker = TerminalLoginWorker("claude-acp", ta, cwd=str(tmp_path), use_pty=True)
    assert worker._use_pty is False
    assert worker._use_conpty is True


def test_use_conpty_is_false_when_use_pty_was_never_requested(monkeypatch, tmp_path):
    """A worker that never asked for a real terminal at all (Kimi's own
    `kimi login`, plain pipes) must not suddenly start one just because
    the platform happens to look like Windows in a test."""
    _force_windows_conpty_path(monkeypatch)
    ta = TerminalAuth(command=sys.executable, args=[], env={})
    worker = TerminalLoginWorker("kimi", ta, cwd=str(tmp_path), use_pty=False)
    assert worker._use_pty is False
    assert worker._use_conpty is False


def test_use_conpty_is_false_on_posix_when_pty_is_actually_available(tmp_path):
    """Without the platform substitution, this test machine's own real
    `_PTY_AVAILABLE` (True, it's POSIX) must win — `_use_conpty` is only
    ever true when POSIX pty genuinely isn't there."""
    if platform.system() == "Windows":
        pytest.skip("this asserts the POSIX default specifically")
    ta = TerminalAuth(command=sys.executable, args=[], env={})
    worker = TerminalLoginWorker("claude-acp", ta, cwd=str(tmp_path), use_pty=True)
    assert worker._use_pty is True
    assert worker._use_conpty is False


# --- ConPTY unavailable: a clear failure, never a silent pipe fallback -


def test_conpty_unavailable_raises_a_clear_error_instead_of_falling_back_to_pipes(
    qapp, monkeypatch, tmp_path, caplog
):
    """The exact requirement this whole module exists to satisfy: an old
    Windows build (no `CreatePseudoConsole` in kernel32) must not be
    treated as "just don't use a terminal" — the trap §20's own POSIX
    fix corrected once already, on the other platform."""
    caplog.set_level(logging.INFO, logger="houdini_agent_panel.ui.terminal_login")
    _force_windows_conpty_path(monkeypatch)
    monkeypatch.setattr(terminal_login_mod, "_CONPTY_AVAILABLE", False)

    ta = TerminalAuth(command="claude", args=["setup-token"], env={})
    worker = TerminalLoginWorker("claude-acp", ta, cwd=str(tmp_path), use_pty=True)
    assert worker._use_conpty is True

    failures: list[str] = []
    worker.failed.connect(failures.append)
    worker.start()

    _wait_until(qapp, lambda: bool(failures))
    assert "ConPTY" in failures[0]
    assert "1809" in failures[0]
    messages = [r.message for r in caplog.records]
    assert any("ConPTY unavailable" in m for m in messages)
    worker.wait(3000)


# --- end-to-end against a scripted fake process -------------------------


_FAKE_URL = "https://claude.com/cai/oauth/authorize?state=windows-test"


def test_conpty_branch_parses_the_url_and_detects_the_input_prompt(qapp, monkeypatch, tmp_path):
    _force_windows_conpty_path(monkeypatch)
    monkeypatch.setattr(terminal_login_mod, "_CONPTY_AVAILABLE", True)

    process = _ScriptedConPtyProcess(
        [
            b"Opening browser to sign in...\n",
            _FAKE_URL.encode() + b"\n",
            b"Paste code here if prompted > ",
        ]
    )
    process._on_write = lambda data: [b"got:" + data.strip() + b"\n"]
    fake_module = _FakeConPtyModule(process)
    monkeypatch.setattr(terminal_login_mod, "_conpty_windows", fake_module)

    ta = TerminalAuth(command="claude", args=["setup-token"], env={})
    worker = TerminalLoginWorker("claude-acp", ta, cwd=str(tmp_path), use_pty=True)

    found: list[tuple[str, str]] = []
    awaiting: list[bool] = []
    lines: list[str] = []
    worker.url_found.connect(lambda url, code: found.append((url, code)))
    worker.input_requested.connect(lambda: awaiting.append(True))
    worker.line_received.connect(lines.append)
    worker.start()

    _wait_until(qapp, lambda: bool(awaiting))
    assert found == [(_FAKE_URL, "")]

    worker.send_line("MY-WINDOWS-CODE")
    _wait_until(qapp, lambda: any("got:MY-WINDOWS-CODE" in line for line in lines))

    # Carriage return, not line feed: Enter on a console is CR, and this
    # build reads its own keystrokes rather than lines. Same reasoning as
    # the POSIX path — see `_ENTER_ON_A_TERMINAL`.
    assert process.written == b"MY-WINDOWS-CODE\r"
    assert fake_module.spawn_calls == [("claude", ["setup-token"], str(tmp_path))]
    worker.wait(3000)


def test_conpty_branch_closes_the_process_on_the_worker_thread_when_done(qapp, monkeypatch, tmp_path):
    _force_windows_conpty_path(monkeypatch)
    monkeypatch.setattr(terminal_login_mod, "_CONPTY_AVAILABLE", True)

    process = _ScriptedConPtyProcess([b"placeholder ran\n"])
    process._finished = True  # ends on its own, nothing to wait on
    fake_module = _FakeConPtyModule(process)
    monkeypatch.setattr(terminal_login_mod, "_conpty_windows", fake_module)

    ta = TerminalAuth(command="claude", args=["setup-token"], env={})
    worker = TerminalLoginWorker("claude-acp", ta, cwd=str(tmp_path), use_pty=True)

    exited: list[int] = []
    worker.exited.connect(exited.append)
    worker.start()

    _wait_until(qapp, lambda: bool(exited))
    worker.wait(3000)
    assert process.closed is True


def test_conpty_branch_stop_terminates_the_scripted_process(qapp, monkeypatch, tmp_path):
    _force_windows_conpty_path(monkeypatch)
    monkeypatch.setattr(terminal_login_mod, "_CONPTY_AVAILABLE", True)

    process = _ScriptedConPtyProcess([b"Paste code here if prompted > "])
    fake_module = _FakeConPtyModule(process)
    monkeypatch.setattr(terminal_login_mod, "_conpty_windows", fake_module)

    ta = TerminalAuth(command="claude", args=["setup-token"], env={})
    worker = TerminalLoginWorker("claude-acp", ta, cwd=str(tmp_path), use_pty=True)

    exited: list[int] = []
    worker.exited.connect(exited.append)
    worker.start()

    _wait_until(qapp, lambda: worker._conpty_process is not None)
    worker.stop()

    _wait_until(qapp, lambda: bool(exited), timeout_ms=5000)
    worker.wait(3000)
    assert process.terminated is True


def test_conpty_branch_token_capture_and_redaction_still_apply(qapp, monkeypatch, tmp_path, caplog):
    """The token-capture/redaction logic in `work()`'s read loop is
    platform-neutral (docs/facts/acp-sdk.md §21) — this confirms it
    still fires correctly when the reader behind it is a `ConPtyReader`
    instead of `process.stdout`/`_PtyMasterReader`."""
    caplog.set_level(logging.INFO, logger="houdini_agent_panel.ui.terminal_login")
    _force_windows_conpty_path(monkeypatch)
    monkeypatch.setattr(terminal_login_mod, "_CONPTY_AVAILABLE", True)

    fake_token = "FAKE-WINDOWS-TOKEN-VALUE-NOT-REAL-123456"
    process = _ScriptedConPtyProcess(
        [
            b"Your OAuth token (valid for 1 year):\n",
            fake_token.encode() + b"\n",
            # Literally `<token>` — see `_TOKEN_VALUE_RE`. The value comes
            # from the bare line above, never from this one.
            b"Use this token by setting: export CLAUDE_CODE_OAUTH_TOKEN=<token>\n",
        ]
    )
    process._finished = True
    fake_module = _FakeConPtyModule(process)
    monkeypatch.setattr(terminal_login_mod, "_conpty_windows", fake_module)

    ta = TerminalAuth(command="claude", args=["setup-token"], env={})
    worker = TerminalLoginWorker("claude-acp", ta, cwd=str(tmp_path), use_pty=True)

    captured: list[tuple[str, str]] = []
    lines: list[str] = []
    worker.token_captured.connect(lambda env_var, token: captured.append((env_var, token)))
    worker.line_received.connect(lines.append)
    worker.start()

    _wait_until(qapp, lambda: bool(captured))
    assert captured == [("CLAUDE_CODE_OAUTH_TOKEN", fake_token)]
    assert not any(fake_token in line for line in lines)
    messages = [r.message for r in caplog.records]
    assert not any(fake_token in m for m in messages)
    worker.wait(3000)

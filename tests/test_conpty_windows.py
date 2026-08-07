"""`ui/_conpty_windows.py` — Windows ConPTY via `ctypes` + `kernel32.dll`.

This module has never run against a real Windows machine (no Windows box
in this project — see the module's own docstring). What CAN be tested
for real, from macOS/Linux, is the WRAPPING logic: does `spawn()` call
the documented sequence of functions, in the documented order, with the
documented arguments; does an out-parameter written by kernel32 actually
flow back into the returned `ConPtyProcess`; does a failure at any given
step raise a `ConPtyError` naming that step and a concrete error code,
with every handle opened so far cleaned up, instead of leaking or
silently continuing.

`_FakeKernel32` below is the injection point that makes this possible:
`_conpty_windows.spawn`/`conpty_available` accept `kernel32=...`, and
every call site uses `ctypes.pointer(x)` (not `ctypes.byref(x)`) for
out-parameters specifically because a `pointer` object supports
`ptr[0] = value` from plain Python — `byref` is a call-only proxy that a
pure-Python stand-in function cannot write through. This is the same
"fake kernel32" substitution the task instructions describe: it proves
`spawn()`'s own bookkeeping is correct; it does NOT prove a real
`kernel32.dll` accepts the calls this module makes.
"""

from __future__ import annotations

import ctypes
import logging
import platform

import pytest

from houdini_agent_panel.ui import _conpty_windows as conpty


class _FakeKernel32:
    """A plain-Python stand-in for the exact subset of `kernel32.dll`
    `_conpty_windows.spawn` calls. "Handles" are plain increasing ints —
    real Win32 handles are opaque anyway, nothing here depends on their
    actual shape, only on them round-tripping correctly.

    `fail_at="<FunctionName>"` makes exactly that call report failure
    (return `False`/a nonzero HRESULT and set a `GetLastError()` code),
    the same as the real function would on a real, broken machine —
    every OTHER call before it still succeeds normally, so a test can
    check that everything opened up to that point gets closed again.
    """

    def __init__(self, *, fail_at: str | None = None) -> None:
        self.calls: list[str] = []
        self.fail_at = fail_at
        self._next_handle = 1000
        self.closed_handles: list[int] = []
        self.terminated = False
        self.exit_code = 259  # STILL_ACTIVE by default
        self.written = b""
        self.last_command_line = ""
        self._read_queue: list[bytes] = []
        self._last_error = 0

    def _handle(self) -> int:
        self._next_handle += 1
        return self._next_handle

    def GetLastError(self) -> int:
        """Bound and called explicitly by `_conpty_windows.py` (not
        `ctypes.get_last_error()`, which does not exist outside Windows
        at all) — exactly the seam this fake exists to fill."""
        return self._last_error

    def _fail(self, name: str, code: int) -> bool:
        self._last_error = code
        return self.fail_at == name

    def CreatePipe(self, read_ptr, write_ptr, attrs, size):
        self.calls.append("CreatePipe")
        if self._fail("CreatePipe", 5):  # ERROR_ACCESS_DENIED
            return False
        read_ptr[0] = self._handle()
        write_ptr[0] = self._handle()
        return True

    def CreatePseudoConsole(self, size, hinput, houtput, flags, phpc):
        self.calls.append("CreatePseudoConsole")
        if self.fail_at == "CreatePseudoConsole":
            return -2147024809  # an arbitrary nonzero HRESULT (E_INVALIDARG-shaped)
        phpc[0] = self._handle()
        return 0

    def ClosePseudoConsole(self, hpc):
        self.calls.append("ClosePseudoConsole")

    def InitializeProcThreadAttributeList(self, attr_list, count, flags, size_ptr):
        self.calls.append("InitializeProcThreadAttributeList")
        if attr_list is None:
            # The documented sizing call — expected to "fail" every time,
            # its only job is reporting the required size.
            size_ptr[0] = 64
            self._last_error = 122  # ERROR_INSUFFICIENT_BUFFER
            return False
        if self._fail("InitializeProcThreadAttributeList", 87):  # ERROR_INVALID_PARAMETER
            return False
        return True

    def UpdateProcThreadAttribute(self, attr_list, flags, attribute, value, size, prev, ret_size):
        self.calls.append("UpdateProcThreadAttribute")
        if self._fail("UpdateProcThreadAttribute", 87):
            return False
        return True

    def DeleteProcThreadAttributeList(self, attr_list):
        self.calls.append("DeleteProcThreadAttributeList")

    def CreateProcessW(
        self, app_name, cmd_line, p_attrs, t_attrs, inherit, flags, env, cwd, startup_info_ptr, process_info_ptr
    ):
        self.calls.append("CreateProcessW")
        self.last_command_line = cmd_line.value
        if self._fail("CreateProcessW", 2):  # ERROR_FILE_NOT_FOUND
            return False
        pi = process_info_ptr.contents
        pi.hProcess = self._handle()
        pi.hThread = self._handle()
        pi.dwProcessId = 4242
        pi.dwThreadId = 4343
        return True

    def CloseHandle(self, handle):
        self.calls.append("CloseHandle")
        self.closed_handles.append(handle)
        return True

    def ReadFile(self, handle, buf, size, read_count_ptr, overlapped):
        self.calls.append("ReadFile")
        if not self._read_queue:
            return False  # broken pipe / EOF
        chunk = self._read_queue.pop(0)
        buf[0 : len(chunk)] = chunk
        read_count_ptr[0] = len(chunk)
        return True

    def WriteFile(self, handle, data, size, written_ptr, overlapped):
        self.calls.append("WriteFile")
        self.written += bytes(data[:size])
        written_ptr[0] = size
        return True

    def GetExitCodeProcess(self, handle, code_ptr):
        code_ptr[0] = self.exit_code
        return True

    def WaitForSingleObject(self, handle, millis):
        self.calls.append("WaitForSingleObject")
        return 0

    def TerminateProcess(self, handle, code):
        self.calls.append("TerminateProcess")
        self.terminated = True
        return True


def _spawn(fake, **kwargs):
    return conpty.spawn("cmd.exe", ["/c", "echo", "hi"], env={"FOO": "bar"}, cwd="C:\\work", kernel32=fake, **kwargs)


# --- spawn(): the documented sequence, success path -------------------


def test_spawn_calls_createpipe_twice_before_anything_else():
    fake = _FakeKernel32()
    _spawn(fake)
    assert fake.calls[0] == "CreatePipe"
    assert fake.calls[1] == "CreatePipe"


def test_spawn_returns_a_process_with_the_pid_kernel32_reported():
    fake = _FakeKernel32()
    process = _spawn(fake)
    assert isinstance(process, conpty.ConPtyProcess)
    assert process.pid == 4242


def test_spawn_builds_a_quoted_command_line_via_list2cmdline():
    fake = _FakeKernel32()
    conpty.spawn("C:\\Program Files\\claude.exe", ["setup-token"], env={}, cwd=None, kernel32=fake)
    assert fake.last_command_line == '"C:\\Program Files\\claude.exe" setup-token'


def test_spawn_calls_the_full_documented_sequence():
    fake = _FakeKernel32()
    _spawn(fake)
    # Order matters: pipes, then the pseudo console, then the attribute
    # list, then the process itself — CreateProcessW must be LAST.
    ordered = [c for c in fake.calls if c != "CloseHandle"]
    assert ordered == [
        "CreatePipe",
        "CreatePipe",
        "CreatePseudoConsole",
        "InitializeProcThreadAttributeList",  # sizing call
        "InitializeProcThreadAttributeList",  # real call
        "UpdateProcThreadAttribute",
        "CreateProcessW",
    ]


def test_spawn_closes_the_pty_side_handles_after_createpseudoconsole_succeeds():
    """Microsoft's own sample closes its copies of the PTY-side handles
    right after `CreatePseudoConsole` — they're dup'd internally, and
    holding ours open defeats proper EOF detection later."""
    fake = _FakeKernel32()
    _spawn(fake)
    # 2 pty-side handles closed right after CreatePseudoConsole, kept
    # handles (input_write/output_read/hprocess/hthread) NOT closed by
    # spawn() itself — only by ConPtyProcess.close().
    assert fake.calls.count("CloseHandle") == 2


# --- spawn(): failure paths, one per documented step -------------------


@pytest.mark.parametrize(
    "step",
    [
        "CreatePipe",
        "CreatePseudoConsole",
        "InitializeProcThreadAttributeList",
        "UpdateProcThreadAttribute",
        "CreateProcessW",
    ],
)
def test_spawn_raises_conpty_error_naming_the_failed_step(step):
    fake = _FakeKernel32(fail_at=step)
    with pytest.raises(conpty.ConPtyError, match=step):
        _spawn(fake)


def test_spawn_reports_the_hresult_when_createpseudoconsole_fails():
    fake = _FakeKernel32(fail_at="CreatePseudoConsole")
    with pytest.raises(conpty.ConPtyError, match="0x"):
        _spawn(fake)


def test_spawn_reports_getlasterror_when_createprocessw_fails():
    fake = _FakeKernel32(fail_at="CreateProcessW")
    with pytest.raises(conpty.ConPtyError, match="GetLastError=2"):
        _spawn(fake)


def test_spawn_closes_every_handle_opened_so_far_when_createpseudoconsole_fails():
    fake = _FakeKernel32(fail_at="CreatePseudoConsole")
    with pytest.raises(conpty.ConPtyError):
        _spawn(fake)
    # 2 pty-side handles (always closed) + input_write + output_read —
    # nothing left dangling from a setup that never finished.
    assert fake.calls.count("CloseHandle") == 4


def test_spawn_deletes_the_attribute_list_when_createprocessw_fails():
    fake = _FakeKernel32(fail_at="CreateProcessW")
    with pytest.raises(conpty.ConPtyError):
        _spawn(fake)
    assert "DeleteProcThreadAttributeList" in fake.calls
    assert "ClosePseudoConsole" in fake.calls


def test_spawn_logs_each_step_for_a_tester_report(caplog):
    caplog.set_level(logging.INFO, logger="houdini_agent_panel.ui.conpty_windows")
    fake = _FakeKernel32()
    _spawn(fake)
    messages = [r.message for r in caplog.records]
    assert any("pipes created" in m for m in messages)
    assert any("CreatePseudoConsole ok" in m for m in messages)
    assert any("proc thread attribute list initialised" in m for m in messages)
    assert any("CreateProcessW ok, pid=4242" in m for m in messages)


def test_spawn_logs_a_concrete_error_code_on_failure(caplog):
    caplog.set_level(logging.INFO, logger="houdini_agent_panel.ui.conpty_windows")
    fake = _FakeKernel32(fail_at="CreateProcessW")
    with pytest.raises(conpty.ConPtyError):
        _spawn(fake)
    messages = [r.message for r in caplog.records]
    assert any("CreateProcessW failed: GetLastError=2" in m for m in messages)


# --- ConPtyProcess: .pid/.poll()/.wait()/.terminate()/.read()/.write() -


def test_process_poll_reports_none_while_still_active_then_the_exit_code():
    fake = _FakeKernel32()
    process = _spawn(fake)
    assert process.poll() is None
    fake.exit_code = 0
    assert process.poll() == 0


def test_process_wait_returns_the_exit_code():
    fake = _FakeKernel32()
    fake.exit_code = 3
    process = _spawn(fake)
    assert process.wait() == 3


def test_process_terminate_calls_terminateprocess():
    fake = _FakeKernel32()
    process = _spawn(fake)
    process.terminate()
    assert fake.terminated is True


def test_process_read_returns_queued_bytes_then_empty_on_eof():
    fake = _FakeKernel32()
    process = _spawn(fake)
    fake._read_queue.append(b"hello")
    assert process.read(4096) == b"hello"
    assert process.read(4096) == b""  # queue drained -> ReadFile "fails" -> EOF, same meaning as a POSIX EIO


def test_process_write_reaches_the_input_pipe():
    fake = _FakeKernel32()
    process = _spawn(fake)
    process.write(b"MY-CODE-123\n")
    assert fake.written == b"MY-CODE-123\n"


def test_process_close_releases_every_handle_and_is_idempotent():
    fake = _FakeKernel32()
    process = _spawn(fake)
    process.close()
    process.close()  # must not raise, and must not double-report anything odd
    assert "ClosePseudoConsole" in fake.calls
    assert "DeleteProcThreadAttributeList" in fake.calls
    # input_write, output_read, hthread, hprocess — 4 handles closed by close()
    close_calls_after_spawn = fake.calls.count("CloseHandle") - 2  # minus the 2 pty-side ones spawn() already closed
    assert close_calls_after_spawn == 4


# --- conpty_available() -------------------------------------------------


def test_conpty_available_true_when_the_symbol_is_present():
    assert conpty.conpty_available(kernel32=_FakeKernel32()) is True


def test_conpty_available_false_when_the_symbol_is_missing():
    class _Kernel32WithoutConPty:
        pass

    assert conpty.conpty_available(kernel32=_Kernel32WithoutConPty()) is False


def test_conpty_available_false_on_a_non_windows_platform_without_injection():
    if platform.system() == "Windows":
        pytest.skip("this asserts the non-Windows short-circuit specifically")
    assert conpty.conpty_available() is False


def test_conpty_available_logs_the_windows_build_and_symbol_result(caplog):
    caplog.set_level(logging.INFO, logger="houdini_agent_panel.ui.conpty_windows")
    conpty.conpty_available(kernel32=_FakeKernel32())
    messages = [r.message for r in caplog.records]
    assert any("CreatePseudoConsole found in kernel32" in m for m in messages)


# --- module-level helpers ------------------------------------------------


def test_build_environment_block_shape():
    assert conpty._build_environment_block({"A": "1", "B": "2"}) == "A=1\0B=2\0\0"


def test_build_environment_block_empty_env():
    assert conpty._build_environment_block({}) == "\0\0"


def test_redact_for_log_masks_token_shaped_runs():
    text = "prefix aZ9-x7Qw2vN8mK3pL6rT1sY4 suffix"
    redacted = conpty._redact_for_log(text)
    assert "aZ9-x7Qw2vN8mK3pL6rT1sY4" not in redacted
    assert "chars redacted" in redacted


def test_redact_for_log_leaves_short_text_alone():
    assert conpty._redact_for_log("short and safe") == "short and safe"


def test_windows_build_never_raises():
    assert isinstance(conpty._windows_build(), str)
    assert conpty._windows_build() != ""


def test_load_kernel32_raises_on_a_non_windows_platform():
    """Documents (and locks in) the exact failure this dev machine hits:
    `_CONPTY_AVAILABLE` must be `False` here, because `_load_kernel32`
    itself cannot succeed — confirms `terminal_login.py`'s Windows branch
    never reaches a real `ctypes.WinDLL` call on this machine, only the
    "explain, don't crash" path."""
    if platform.system() == "Windows":
        pytest.skip("this exercises the non-Windows failure path specifically")
    with pytest.raises(AttributeError):
        conpty._load_kernel32()


# --- ConPtyReader: the .read(1) -> str contract -------------------------


class _FixedProcess:
    """A process double for `ConPtyReader` alone — just `.read(n) ->
    bytes`, the only method it calls."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def read(self, size: int = 4096) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


def test_conpty_reader_reassembles_a_utf8_character_split_across_reads():
    """Same quirk `_PtyMasterReader`'s own test in test_terminal_login_
    worker.py covers on POSIX — not independently measured on Windows,
    but the same incremental-decoder fix applies for the same reason."""
    encoded = "✢".encode("utf-8")
    assert len(encoded) == 3
    reader = conpty.ConPtyReader(_FixedProcess([encoded[:1], encoded[1:]]))
    assert reader.read(1) == "✢"


def test_conpty_reader_returns_empty_string_on_eof():
    reader = conpty.ConPtyReader(_FixedProcess([]))
    assert reader.read(1) == ""


def test_conpty_reader_yields_one_character_at_a_time():
    reader = conpty.ConPtyReader(_FixedProcess([b"abc"]))
    assert [reader.read(1), reader.read(1), reader.read(1)] == ["a", "b", "c"]


def test_conpty_reader_logs_the_first_chunk_redacted(caplog):
    caplog.set_level(logging.INFO, logger="houdini_agent_panel.ui.conpty_windows")
    token_like = "aZ9-x7Qw2vN8mK3pL6rT1sY4"
    reader = conpty.ConPtyReader(_FixedProcess([f"token={token_like}".encode()]))
    reader.read(1)
    messages = [r.message for r in caplog.records]
    assert any("first output chunk" in m for m in messages)
    assert not any(token_like in m for m in messages)


def test_conpty_reader_logs_when_there_is_nothing_at_all(caplog):
    caplog.set_level(logging.INFO, logger="houdini_agent_panel.ui.conpty_windows")
    reader = conpty.ConPtyReader(_FixedProcess([]))
    reader.read(1)
    messages = [r.message for r in caplog.records]
    assert any("returned nothing at all" in m for m in messages)

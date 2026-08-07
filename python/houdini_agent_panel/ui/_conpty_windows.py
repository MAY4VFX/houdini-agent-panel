"""ConPTY (Windows pseudo console) via `ctypes` + `kernel32.dll` — the
Windows counterpart to what `pty.openpty()` gives `_PtyMasterReader` on
POSIX (`terminal_login.py`, docs/facts/acp-sdk.md §20).

**NOT INDEPENDENTLY VERIFIED — no Windows machine in this project** (the
same gap `node.py::npm_cache_dir`'s own docstring and `self_update.py`'s
already note, for different reasons). Every step below follows
Microsoft's own documented sequence for "Creating a pseudoconsole
session" (`CreatePipe` x2 → `CreatePseudoConsole` → build a
`STARTUPINFOEXW` with `PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE` via
`InitializeProcThreadAttributeList`/`UpdateProcThreadAttribute` →
`CreateProcessW` with `EXTENDED_STARTUPINFO_PRESENT`) exactly as
Microsoft's own C sample does it — but the actual sequence of syscalls
has never been run against a real `kernel32.dll`. `tests/
test_conpty_windows.py` exercises the WRAPPING logic (argument order,
error handling, the `.pid`/`.poll()`/`.wait()`/`.terminate()`/`.read()`/
`.write()` contract) against an injected fake `kernel32` object — that
proves this module calls the right functions with the right arguments in
the right order, not that a real Windows `kernel32.dll` accepts them.
See this module's own diagnostic logging (`_log`, prefixed `conpty:`) —
every step logs a concrete result (an `HRESULT`, a `GetLastError()` code,
a pid, a byte count), specifically so a tester's `panel.log` is enough to
tell a maintainer which numbered step actually failed, without needing
access to the tester's machine.

Deliberately stdlib-only — no `pywinpty` — same reasoning `node.py`
already documents for not bundling a Node download: this repository
guards its own install weight, and `pywinpty` would be a second runtime
dependency for exactly one platform's one auth flow.

Importable on any platform: `ctypes.wintypes` is a pure Python struct
layout module with no OS calls at import time (confirmed: `import
ctypes.wintypes` succeeds on macOS) — only `ctypes.WinDLL(...)`, which
this module defers to `_load_kernel32()`, actually requires Windows.
That is what lets `terminal_login.py` import this module unconditionally
and lets its own non-DLL logic (argument marshalling, error paths) be
exercised by a real test run on macOS/Linux, via an injected fake
`kernel32`.
"""

from __future__ import annotations

import codecs
import contextlib
import ctypes
import platform
import re
import subprocess
import sys
from ctypes import wintypes

from ..logbook import logger as _logbook_logger

_log = _logbook_logger("houdini_agent_panel.ui.conpty_windows")


# --- structures ------------------------------------------------------------
#
# Pure ctypes struct layouts — importable on any platform, no OS call
# involved in defining them. `ctypes.wintypes` supplies the primitive
# aliases (`DWORD`, `HANDLE`, `LPWSTR`, ...); the structures themselves
# (`STARTUPINFOW`, `STARTUPINFOEXW`, `PROCESS_INFORMATION`) are not in
# `ctypes.wintypes` at all and have to be hand-defined, field for field,
# matching the layout `processthreadsapi.h`/`wincon.h` document.


class _COORD(ctypes.Structure):
    _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.c_void_p),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class _STARTUPINFOEXW(ctypes.Structure):
    _fields_ = [("StartupInfo", _STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


# --- constants (Microsoft's own documented values, not guessed) -----------

_PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE = 0x00020016
_EXTENDED_STARTUPINFO_PRESENT = 0x00080000
_CREATE_UNICODE_ENVIRONMENT = 0x00000400
_STILL_ACTIVE = 259
_INFINITE = 0xFFFFFFFF


class ConPtyError(RuntimeError):
    """Raised with a message already written for the artist — same shape
    `self_update.SelfUpdateError`/`bugreport.BugReportError` already use:
    classify the failure once, where it happens, not re-derive it in the
    UI from a raw code. `TerminalLoginWorker.work()` lets this propagate;
    `ui/worker.py`'s `Worker.run()` turns it into a `failed` signal
    (`str(exc)`, exactly this message) and a full traceback in
    `panel.log` — the message itself is deliberately specific (a step
    name plus a numeric code), because that log is the only diagnostic a
    maintainer without a Windows machine will ever get.
    """


#: Duplicated, not imported, from `terminal_login.py`'s own
#: `_LOOKS_LIKE_A_TOKEN_RE`/`_redact_for_log` — importing them here would
#: make this module and `terminal_login.py` (which imports THIS module)
#: import each other. Same shape, same reasoning `bugreport.py`'s own
#: docstring gives for porting rather than importing its sibling
#: service's redaction list: kept in sync by hand, and a shape either
#: copy misses is still caught by the other one.
_LOOKS_LIKE_A_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-\.]{24,}")


def _redact_for_log(text: str) -> str:
    return _LOOKS_LIKE_A_TOKEN_RE.sub(lambda m: f"<{len(m.group(0))} chars redacted>", text)


def _windows_build() -> str:
    """Best-effort "which Windows, exactly" string for the log header —
    never raises; a diagnostic detail; a failure to determine it must
    never block a real spawn attempt over it. `sys.getwindowsversion()`
    is Windows-only (`AttributeError` everywhere else, caught below) and
    gives the actual build number (e.g. 22631 for a 23H2 release), which
    `platform.platform()` alone does not — the build number is exactly
    what distinguishes "before ConPTY existed" (pre-1809, build < 17763)
    from a build that has it.
    """
    try:
        version = sys.getwindowsversion()  # type: ignore[attr-defined]
        return f"{version.major}.{version.minor}.{version.build}"
    except AttributeError:
        try:
            return platform.platform()
        except Exception:  # noqa: BLE001 - a log header must never raise
            return "unknown"


def _load_kernel32():
    """Loads the real `kernel32.dll` and binds the exact function
    signatures this module needs. Raises on any non-Windows platform —
    `ctypes.WinDLL` itself does not exist in the `ctypes` module built
    for other platforms (confirmed directly: `AttributeError: module
    'ctypes' has no attribute 'WinDLL'` on macOS) — so this never gets
    far enough to pretend a fake success is real.

    Never called by the test suite: every test that needs a `kernel32`
    passes its own fake object directly to `spawn()`/`conpty_available()`
    instead, which is what lets those tests run on macOS/Linux at all.
    """
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]

    kernel32.CreatePipe.argtypes = [
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.POINTER(wintypes.HANDLE),
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.CreatePipe.restype = wintypes.BOOL

    kernel32.CreatePseudoConsole.argtypes = [
        _COORD,
        wintypes.HANDLE,
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    kernel32.CreatePseudoConsole.restype = ctypes.c_long  # HRESULT

    kernel32.ClosePseudoConsole.argtypes = [wintypes.HANDLE]
    kernel32.ClosePseudoConsole.restype = None

    kernel32.InitializeProcThreadAttributeList.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL

    kernel32.UpdateProcThreadAttribute.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL

    kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
    kernel32.DeleteProcThreadAttributeList.restype = None

    kernel32.CreateProcessW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.BOOL,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.LPCWSTR,
        ctypes.POINTER(_STARTUPINFOEXW),
        ctypes.POINTER(_PROCESS_INFORMATION),
    ]
    kernel32.CreateProcessW.restype = wintypes.BOOL

    kernel32.ReadFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel32.ReadFile.restype = wintypes.BOOL

    kernel32.WriteFile.argtypes = [
        wintypes.HANDLE,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.c_void_p,
    ]
    kernel32.WriteFile.restype = wintypes.BOOL

    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL

    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD

    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess.restype = wintypes.BOOL

    # Bound and called explicitly (`kernel32.GetLastError()`) rather than
    # `ctypes.get_last_error()`/`ctypes.set_last_error()` — those two are
    # defined ONLY in the `ctypes` module built for Windows (confirmed:
    # `AttributeError` for both on macOS), which would make every error
    # path below untestable from anywhere else. A plain kernel32 call
    # keeps error reporting inside the exact same dependency-injection
    # seam as everything else in this module — the fake `kernel32` test
    # double supplies its own `GetLastError()`, no Windows-only ctypes
    # internals involved.
    kernel32.GetLastError.argtypes = []
    kernel32.GetLastError.restype = wintypes.DWORD

    return kernel32


def conpty_available(kernel32=None) -> bool:
    """True only when `CreatePseudoConsole` is actually present in
    `kernel32.dll` — checked by looking the symbol UP rather than by a
    Windows-build-number cutoff alone (Windows 10 1809 / build 17763 is
    when Microsoft shipped it): the symbol IS the real requirement, a
    version number is only a proxy for it, and checking the symbol
    directly costs one attribute lookup. `_windows_build()` is still
    logged alongside it, for a tester's report to show at a glance.

    `kernel32=...` is the dependency-injection point
    `tests/test_conpty_windows.py` uses to exercise both outcomes (symbol
    present / absent) without ever touching a real DLL. On a genuinely
    non-Windows platform this returns `False` immediately, without
    attempting `ctypes.WinDLL` at all (which does not exist there) —
    unless a fake `kernel32` was explicitly passed in, which is exactly
    what the test suite does to reach this function's real logic from
    macOS/Linux.
    """
    if kernel32 is None:
        if platform.system() != "Windows":
            return False
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        except (AttributeError, OSError) as exc:
            _log.info("conpty: kernel32 could not be loaded (%r)", exc)
            return False
    found = hasattr(kernel32, "CreatePseudoConsole")
    _log.info(
        "conpty: CreatePseudoConsole %s in kernel32 (windows build: %s)",
        "found" if found else "NOT found",
        _windows_build(),
    )
    return found


#: Computed once at import time, the same style `terminal_login.py`'s own
#: `_PTY_AVAILABLE` uses — cheap (one attribute lookup, or an immediate
#: `False` on non-Windows) and this module is only ever imported once per
#: process.
_CONPTY_AVAILABLE = conpty_available()


def _build_environment_block(env: dict) -> str:
    """Windows' own `lpEnvironment` shape for `CREATE_UNICODE_ENVIRONMENT`:
    `"KEY=VALUE\\0KEY2=VALUE2\\0\\0"` — each entry NUL-terminated, the
    whole block double-NUL-terminated. `ctypes.create_unicode_buffer`
    preserves the embedded NULs (it copies the Python string's exact
    characters, it does not `strlen()`-truncate at the first one), so the
    result below is safe to hand to `CreateProcessW` as-is.
    """
    if not env:
        return "\0\0"
    return "\0".join(f"{key}={value}" for key, value in env.items()) + "\0\0"


class ConPtyProcess:
    """Wraps a ConPTY-attached child process behind the same narrow
    contract `subprocess.Popen` already gives the POSIX/plain-pipe paths
    in `terminal_login.py`: `.pid`, `.poll()`, `.wait()`, `.terminate()`
    — so `TerminalLoginWorker.work()`'s cleanup code (`orphans.
    record_started`/`_stopped`, `stop()`) does not need to know which of
    the three it has. `.read(n)`/`.write(data)` are the ConPTY-specific
    pipe I/O `ConPtyReader`/`TerminalLoginWorker.send_line` use in place
    of `os.read`/`os.write` on a pty master fd.
    """

    def __init__(
        self,
        *,
        kernel32,
        hprocess,
        hthread,
        pid: int,
        hpc,
        input_write,
        output_read,
        attribute_list,
    ) -> None:
        self._kernel32 = kernel32
        self._hprocess = hprocess
        self._hthread = hthread
        self._pid = pid
        self._hpc = hpc
        self._input_write = input_write
        self._output_read = output_read
        self._attribute_list = attribute_list
        self._closed = False

    @property
    def pid(self) -> int:
        return self._pid

    def poll(self) -> int | None:
        code = wintypes.DWORD()
        if not self._kernel32.GetExitCodeProcess(self._hprocess, ctypes.pointer(code)):
            return None  # can't tell right now — treated as "still running", same as Popen would be forced to
        if code.value == _STILL_ACTIVE:
            return None
        return code.value

    def wait(self, timeout: float | None = None) -> int:
        millis = _INFINITE if timeout is None else max(0, int(timeout * 1000))
        self._kernel32.WaitForSingleObject(self._hprocess, millis)
        code = wintypes.DWORD()
        self._kernel32.GetExitCodeProcess(self._hprocess, ctypes.pointer(code))
        return code.value

    def terminate(self) -> None:
        self._kernel32.TerminateProcess(self._hprocess, 1)

    def read(self, size: int = 4096) -> bytes:
        """`b""` means EOF/broken pipe — the same meaning `_PtyMasterReader.
        read` gives an `OSError` (EIO) on POSIX; `ConPtyReader` above it
        never has to know which platform it's actually running on."""
        buf = ctypes.create_string_buffer(size)
        read_count = wintypes.DWORD()
        ok = self._kernel32.ReadFile(self._output_read, buf, size, ctypes.pointer(read_count), None)
        if not ok:
            return b""
        return buf.raw[: read_count.value]

    def write(self, data: bytes) -> None:
        written = wintypes.DWORD()
        self._kernel32.WriteFile(self._input_write, data, len(data), ctypes.pointer(written), None)

    def close(self) -> None:
        """Releases every handle this process owns — called from
        `TerminalLoginWorker.work()`'s own `finally`, mirroring how the
        POSIX branch closes `self._pty_master_fd` there. Idempotent and
        exception-swallowing per-handle: a partially-failed spawn (an
        error raised after SOME handles were already created) must still
        be able to call this once, on whatever handles it actually got,
        without one failed `CloseHandle` stopping the rest from being
        tried.
        """
        if self._closed:
            return
        self._closed = True
        for handle in (self._input_write, self._output_read, self._hthread, self._hprocess):
            with contextlib.suppress(Exception):
                self._kernel32.CloseHandle(handle)
        with contextlib.suppress(Exception):
            self._kernel32.ClosePseudoConsole(self._hpc)
        if self._attribute_list is not None:
            with contextlib.suppress(Exception):
                self._kernel32.DeleteProcThreadAttributeList(self._attribute_list)


def spawn(
    command: str,
    args: list,
    *,
    env: dict,
    cwd: str | None,
    columns: int = 120,
    rows: int = 30,
    kernel32=None,
) -> ConPtyProcess:
    """Spawns `command *args` attached to a fresh ConPTY, following
    Microsoft's own documented sequence exactly (see the module
    docstring). Every step logs its own concrete result — an `HRESULT`,
    a `GetLastError()` code, a pid — through `_log`, `conpty:`-prefixed,
    specifically so a failure here is diagnosable from a tester's
    `panel.log` alone.

    Raises `ConPtyError` (never returns a half-built process) on the
    first step that fails; every partially-created handle from THAT
    attempt is closed before raising, not left leaked for the caller to
    guess about — the caller has no cleanup of its own to do on a raised
    `ConPtyError`, only on the object this function actually returns.

    `kernel32=...` is the same dependency-injection point `conpty_
    available` offers — real production code always leaves it `None`
    (loading the genuine DLL via `_load_kernel32`); tests pass a fake
    object that mimics the same call shapes without ever touching
    Windows.
    """
    if kernel32 is None:
        kernel32 = _load_kernel32()

    _log.info("conpty: spawning %s %s (windows build %s)", command, _redact_for_log(" ".join(args)), _windows_build())

    pty_input_read = wintypes.HANDLE()
    input_write = wintypes.HANDLE()
    output_read = wintypes.HANDLE()
    pty_output_write = wintypes.HANDLE()

    if not kernel32.CreatePipe(ctypes.pointer(pty_input_read), ctypes.pointer(input_write), None, 0):
        error = kernel32.GetLastError()
        _log.error("conpty: CreatePipe (input) failed: GetLastError=%s", error)
        raise ConPtyError(f"ConPTY setup failed at CreatePipe (input): GetLastError={error}")
    if not kernel32.CreatePipe(ctypes.pointer(output_read), ctypes.pointer(pty_output_write), None, 0):
        error = kernel32.GetLastError()
        _log.error("conpty: CreatePipe (output) failed: GetLastError=%s", error)
        kernel32.CloseHandle(pty_input_read)
        kernel32.CloseHandle(input_write)
        raise ConPtyError(f"ConPTY setup failed at CreatePipe (output): GetLastError={error}")
    _log.info("conpty: pipes created")

    size = _COORD(X=columns, Y=rows)
    hpc = wintypes.HANDLE()
    hr = kernel32.CreatePseudoConsole(size, pty_input_read, pty_output_write, 0, ctypes.pointer(hpc))
    # The PTY-side handles are dup'd internally by CreatePseudoConsole
    # (Microsoft's own sample closes these right after the call,
    # success or failure) — closed here unconditionally, before checking
    # `hr`, so a failure path below doesn't also have to remember them.
    kernel32.CloseHandle(pty_input_read)
    kernel32.CloseHandle(pty_output_write)
    if hr != 0:
        _log.error("conpty: CreatePseudoConsole failed: HRESULT=0x%08x", hr & 0xFFFFFFFF)
        kernel32.CloseHandle(input_write)
        kernel32.CloseHandle(output_read)
        raise ConPtyError(f"ConPTY setup failed at CreatePseudoConsole: HRESULT=0x{hr & 0xFFFFFFFF:08x}")
    _log.info("conpty: CreatePseudoConsole ok, size=%dx%d", columns, rows)

    attr_size = ctypes.c_size_t(0)
    # Documented two-call pattern: the first call is EXPECTED to fail
    # (there is no buffer yet) — its only job is to report the required
    # size into `attr_size`. Its return value is deliberately not
    # checked; only the size it reports is used.
    kernel32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.pointer(attr_size))
    attribute_list = ctypes.create_string_buffer(attr_size.value or 1)
    if not kernel32.InitializeProcThreadAttributeList(attribute_list, 1, 0, ctypes.pointer(attr_size)):
        error = kernel32.GetLastError()
        _log.error("conpty: InitializeProcThreadAttributeList failed: GetLastError=%s", error)
        kernel32.ClosePseudoConsole(hpc)
        kernel32.CloseHandle(input_write)
        kernel32.CloseHandle(output_read)
        raise ConPtyError(f"ConPTY setup failed at InitializeProcThreadAttributeList: GetLastError={error}")

    if not kernel32.UpdateProcThreadAttribute(
        attribute_list,
        0,
        _PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE,
        ctypes.pointer(hpc),
        ctypes.sizeof(wintypes.HANDLE),
        None,
        None,
    ):
        error = kernel32.GetLastError()
        _log.error("conpty: UpdateProcThreadAttribute failed: GetLastError=%s", error)
        kernel32.DeleteProcThreadAttributeList(attribute_list)
        kernel32.ClosePseudoConsole(hpc)
        kernel32.CloseHandle(input_write)
        kernel32.CloseHandle(output_read)
        raise ConPtyError(f"ConPTY setup failed at UpdateProcThreadAttribute: GetLastError={error}")
    _log.info("conpty: proc thread attribute list initialised")

    startup_info = _STARTUPINFOEXW()
    startup_info.StartupInfo.cb = ctypes.sizeof(_STARTUPINFOEXW)
    startup_info.lpAttributeList = ctypes.cast(attribute_list, ctypes.c_void_p)
    process_info = _PROCESS_INFORMATION()

    # `list2cmdline` is `subprocess`'s own Windows-command-line quoting —
    # reused rather than reimplemented, the same "don't duplicate what
    # already exists" rule this repository applies to `fxhoudinimcp/
    # install.py`. `CreateProcessW`'s docs require `lpCommandLine` to be
    # a MUTABLE buffer (the system may modify its contents) — a plain
    # Python `str` is not that, hence `create_unicode_buffer`.
    command_line = subprocess.list2cmdline([command, *args])
    command_line_buffer = ctypes.create_unicode_buffer(command_line)
    env_buffer = ctypes.create_unicode_buffer(_build_environment_block(env))

    ok = kernel32.CreateProcessW(
        None,
        command_line_buffer,
        None,
        None,
        False,
        _EXTENDED_STARTUPINFO_PRESENT | _CREATE_UNICODE_ENVIRONMENT,
        env_buffer,
        cwd,
        ctypes.pointer(startup_info),
        ctypes.pointer(process_info),
    )
    if not ok:
        error = kernel32.GetLastError()
        _log.error("conpty: CreateProcessW failed: GetLastError=%s command=%r", error, command)
        kernel32.DeleteProcThreadAttributeList(attribute_list)
        kernel32.ClosePseudoConsole(hpc)
        kernel32.CloseHandle(input_write)
        kernel32.CloseHandle(output_read)
        raise ConPtyError(f"ConPTY setup failed at CreateProcessW: GetLastError={error}")

    _log.info("conpty: CreateProcessW ok, pid=%s", process_info.dwProcessId)

    return ConPtyProcess(
        kernel32=kernel32,
        hprocess=process_info.hProcess,
        hthread=process_info.hThread,
        pid=process_info.dwProcessId,
        hpc=hpc,
        input_write=input_write,
        output_read=output_read,
        attribute_list=attribute_list,
    )


class ConPtyReader:
    """Adapts a `ConPtyProcess` to the same one-character-at-a-time
    `.read(1) -> str` interface `_PtyMasterReader` (`terminal_login.py`)
    gives the POSIX pty path — `TerminalLoginWorker.work()`'s read loop
    needs only one shape regardless of which platform it's on.

    Same incremental-UTF-8-decode treatment `_PtyMasterReader` uses, for
    the same reason: a multi-byte character (this build's own spinner
    glyphs, per docs/facts/acp-sdk.md §20, are all multi-byte) can arrive
    split across two separate `ReadFile` chunks just as easily as across
    two `os.read()` calls — NOT independently measured on Windows, unlike
    its POSIX sibling, but the cost of guarding for it is the same few
    lines either way, and a naive per-chunk `.decode()` already proved
    itself unsafe once on the POSIX side.

    Logs the first chunk it ever reads (length and a REDACTED preview,
    `conpty: first output chunk (...)`) — the single most useful line in
    a tester's log for telling "ConPTY came up and the child is actually
    printing something" from "spawned fine, dead silence", the exact
    failure mode plain pipes produced on POSIX before §20's fix.
    """

    def __init__(self, process: "ConPtyProcess") -> None:
        self._process = process
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._pending = ""
        self._first_read_logged = False

    def read(self, _size: int = 1) -> str:
        while not self._pending:
            chunk = self._process.read(4096)
            if not self._first_read_logged:
                self._first_read_logged = True
                if chunk:
                    preview = chunk.decode("utf-8", errors="replace")
                    _log.info("conpty: first output chunk (%d bytes): %s", len(chunk), _redact_for_log(preview))
                else:
                    _log.info("conpty: read() returned nothing at all before EOF")
            if not chunk:
                return ""
            self._pending = self._decoder.decode(chunk)
        char, self._pending = self._pending[0], self._pending[1:]
        return char


__all__ = [
    "ConPtyError",
    "ConPtyProcess",
    "ConPtyReader",
    "conpty_available",
    "spawn",
]

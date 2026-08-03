"""ACP client on top of `agent-client-protocol`, wrapped in Qt signals.

The riskiest part of the project (see docs/architecture.md §6): the SDK is
async, Qt is synchronous, and the agent is someone else's process. Rules this
file holds to:

- The asyncio loop lives on its own `QThread` (`AcpWorker`); we never touch
  `hou` from it — all scene work goes through the separate fx process.
- Out of the worker — only Qt signals (delivery to another thread is Qt's
  concern: the signal queue automatically becomes thread-safe when the
  receiving object lives on a different thread than the one `.emit()` was
  called from). Into it — only via `AcpWorker.submit()`
  (`asyncio.run_coroutine_threadsafe`).
- We don't use `qasync`: our own loop runner on a dedicated `QThread` is
  simpler and doesn't pull in an extra dependency.

`AcpWorker` is both the loop runner (`run()` is overridden instead of the Qt
event loop) and the implementation of the ACP `Client` protocol
(`session_update`, `request_permission`, `on_connect` are called by `acp`
itself from coroutines running on this same loop) — the two roles from the
architecture docstring ("owns the loop, the agent process, the connection"
and "lives on the worker thread") naturally converge into one object: the
thing that services the agent's callbacks physically is that worker thread.

**Houdini swaps out asyncio (`haio`, see docs/facts/houdini.md §9).** Inside
Houdini, `asyncio.get_event_loop_policy()` returns
`haio.HoudiniEventLoopPolicy`, and through it:

1. `asyncio.new_event_loop()` returns `haio.HoudiniEventLoop`, whose
   `run_forever()` requires the main thread — on our worker `QThread` it
   raises `RuntimeError`. Fixed by taking the loop class directly instead of
   going through the policy: `asyncio.SelectorEventLoop()` (POSIX) /
   `asyncio.ProactorEventLoop()` (Windows).
2. `acp.spawn_agent_process()` doesn't work at all: it's built on
   `asyncio.create_subprocess_exec`, which on POSIX goes through the child
   watcher via the GLOBAL policy
   (`get_event_loop_policy().get_child_watcher()`), and `haio` raises
   `NotImplementedError` from there — regardless of which loop object we use
   ourselves. Workaround: spawn the process with plain `subprocess.Popen`
   (doesn't go through the child watcher at all) and hook its pipes into the
   loop via `connect_read_pipe`/`connect_write_pipe` — that public API
   doesn't need a watcher — then hand the pair to `acp.connect_to_agent`
   (the documented byte-stream connection form, see facts/acp-sdk.md §1).

This is also true outside Houdini (the stock policy is perfectly happy with
`SelectorEventLoop()` taken directly too), so we always take this path
instead of branching the code into "under Houdini" and "in tests".
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import acp
from acp.schema import (
    AgentCapabilities,
    AllowedOutcome,
    AudioContentBlock,
    ClientCapabilities,
    DeniedOutcome,
    EmbeddedResourceContentBlock,
    EnvVariable,
    ImageContentBlock,
    Implementation,
    McpServerStdio,
    PromptCapabilities,
    ResourceContentBlock,
    SessionModeState,
    TextContentBlock,
)

from . import __version__
from .sessions import SessionMode as _SessionMode
from .sessions import SessionState
from .ui.qt import QtCore, Signal

if TYPE_CHECKING:  # types only — runtime.py doesn't need to exist at import time
    from .runtime import LaunchSpec

#: stdio transport buffer limit. asyncio's default is 64 KB — a base64 image
#: in a session/update will overflow it and hang the connection (see
#: docs/facts/acp-sdk.md §1). The agent side of the SDK (`run_agent`) sets
#: the exact same value by default — keeping the client symmetric.
_STDIO_BUFFER_LIMIT = 50 * 1024 * 1024

#: "login required" error code — ACP's own convention (application-specific
#: JSON-RPC range, not the standard -32700..-32603).
#: Ceiling for waiting on an `initialize` reply. Deliberately generous: an
#: npx agent on first launch downloads its own package first, and a minute
#: there is normal, not a sign of breakage. But there has to be a ceiling:
#: without one the panel waits forever.
_CONNECT_TIMEOUT = 180.0

#: How many of the agent's last stderr lines to show in the error message.
_STDERR_TAIL_LINES = 12

_AUTH_REQUIRED_CODE = -32000

_CONTENT_BLOCK_TYPES: dict[str, type] = {
    "text": TextContentBlock,
    "image": ImageContentBlock,
    "audio": AudioContentBlock,
    "resource_link": ResourceContentBlock,
    "resource": EmbeddedResourceContentBlock,
}


@dataclass(frozen=True)
class AuthMethod:
    id: str
    name: str
    description: str = ""


@dataclass(frozen=True)
class AgentInfo:
    """Flat snapshot of `initialize`, so the UI doesn't have to pull in ACP's
    pydantic models.

    `supports_*` is the single source of truth for whether to draw the
    attachment button/microphone/etc.: the agent doesn't support it — the
    control doesn't get drawn.
    """

    name: str
    version: str
    protocol_version: int
    supports_image: bool
    supports_audio: bool
    supports_embedded_context: bool
    supports_load_session: bool
    supports_logout: bool
    auth_methods: tuple[AuthMethod, ...]


def _agent_info_from(init: Any) -> AgentInfo:
    implementation = init.agent_info
    caps = init.agent_capabilities or AgentCapabilities()
    prompt_caps = caps.prompt_capabilities or PromptCapabilities()
    auth_caps = caps.auth
    return AgentInfo(
        name=implementation.name if implementation else "agent",
        version=(implementation.version if implementation else "") or "",
        protocol_version=init.protocol_version,
        supports_image=bool(prompt_caps.image),
        supports_audio=bool(prompt_caps.audio),
        supports_embedded_context=bool(prompt_caps.embedded_context),
        supports_load_session=bool(caps.load_session),
        # The AgentAuthCapabilities.logout contract is exactly this: a
        # missing field or None means "the agent doesn't support it", and
        # LogoutCapabilities() (an empty but non-None object) means "it
        # does" (docs/facts/acp-sdk.md, acp/schema.py:3747-3754). We check
        # `is not None` specifically — that's a direct match to the
        # contract, not a guess via truthiness.
        supports_logout=auth_caps is not None and getattr(auth_caps, "logout", None) is not None,
        auth_methods=tuple(
            AuthMethod(id=m.id, name=m.name, description=getattr(m, "description", None) or "")
            for m in (init.auth_methods or [])
        ),
    )


def _build_mcp_servers(entries: list[dict]) -> list[McpServerStdio]:
    """`scene.mcp_servers()` -> objects that `new_session` understands."""
    return [
        McpServerStdio(
            name=entry["name"],
            command=entry["command"],
            args=list(entry.get("args", [])),
            env=[EnvVariable(name=e["name"], value=e["value"]) for e in entry.get("env", [])],
        )
        for entry in entries
    ]


def _build_content_block(block: dict) -> Any:
    """Build an ACP content block from a `Composer.submitted` dict."""
    kind = block.get("type")
    cls = _CONTENT_BLOCK_TYPES.get(kind)
    if cls is None:
        raise ValueError(f"unknown content block type: {kind!r}")
    return cls(**block)


def _chunk_text(content: Any) -> str:
    return content.text if getattr(content, "type", None) == "text" else ""


@dataclass(frozen=True)
class ConfigChoice:
    value: str
    name: str
    description: str = ""


@dataclass(frozen=True)
class ConfigOption:
    """One agent-side setting: model, reasoning effort, fast mode and so on.

    Agents expose these through `configOptions` in the `session/new` reply —
    that is where the model picker actually lives in ACP. The panel draws
    what the agent offers and nothing else: `category` tells the UI how to
    group them (`model_config` and friends), and it is the agent's word, not
    ours.
    """

    id: str
    name: str
    current_value: str
    choices: tuple[ConfigChoice, ...] = ()
    description: str = ""
    category: str = ""


def _config_options_from(raw) -> list[ConfigOption]:
    """Flatten the SDK's config options, skipping anything not a select.

    Booleans and future kinds are dropped deliberately rather than guessed
    at: drawing a control we do not understand is worse than not drawing it.
    """
    result: list[ConfigOption] = []
    for option in raw or []:
        choices = getattr(option, "options", None)
        if not choices:
            continue
        result.append(
            ConfigOption(
                id=getattr(option, "id", "") or "",
                name=getattr(option, "name", "") or "",
                current_value=str(getattr(option, "current_value", "") or ""),
                choices=tuple(
                    ConfigChoice(
                        value=str(getattr(c, "value", "")),
                        name=getattr(c, "name", "") or str(getattr(c, "value", "")),
                        description=getattr(c, "description", "") or "",
                    )
                    for c in choices
                ),
                description=getattr(option, "description", "") or "",
                category=getattr(option, "category", "") or "",
            )
        )
    return result



class AcpWorker(QtCore.QThread):
    """Lives on the worker thread. Owns the loop, the agent process, the connection.

    `run()` is overridden: instead of `QThread`'s event loop it spins an
    `asyncio` loop (`loop.run_forever()`). The methods `session_update` /
    `request_permission` / `on_connect` implement the ACP `Client` via duck
    typing (the protocol is not an ABC, subclassing isn't required, see
    docs/facts/acp-sdk.md §2) and are called by `acp` itself from coroutines
    running on this same loop.
    """

    # --- connection lifecycle ---------------------------------------
    connected = Signal(object)  # AgentInfo
    disconnected = Signal(str)  # reason, "" on a normal stop
    failed = Signal(str)  # human-readable text
    auth_required = Signal(list)  # list[AuthMethod]
    log_line = Signal(str)  # agent stderr

    # --- sessions -----------------------------------------------------------
    session_started = Signal(str, object)  # session_id, SessionState
    modes_changed = Signal(str, object)  # session_id, acp.schema.SessionModeState
    commands_changed = Signal(str, list)  # session_id, list[acp.schema.AvailableCommand]
    config_options_changed = Signal(str, list)  # session_id, list[ConfigOption]

    # --- feed --------------------------------------------------------------
    message_chunk = Signal(str, str, str)  # session_id, message_id, text
    thought_chunk = Signal(str, str, str)
    tool_call = Signal(str, object)  # session_id, acp.schema.ToolCallStart
    tool_call_update = Signal(str, object)  # session_id, acp.schema.ToolCallProgress
    plan_changed = Signal(str, list)  # session_id, list[acp.schema.PlanEntry]
    usage_changed = Signal(str, object)  # session_id, acp.schema.Usage
    turn_finished = Signal(str, str)  # session_id, stop_reason
    error = Signal(str, str)  # session_id (may be ""), text

    # --- permissions ---------------------------------------------------------
    permission_requested = Signal(str, str, object, list)
    # request_key, session_id, ToolCallUpdate, list[PermissionOption]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # The loop is created here (on AcpWorker's owning thread, i.e. still
        # the main thread — before start()), not in run(): submit() may be
        # needed before the thread has actually started, and the loop
        # reference must be valid right after the constructor.
        #
        # We take the loop class DIRECTLY, not via asyncio.new_event_loop():
        # inside Houdini that goes through the swapped-in `haio` policy,
        # whose run_forever() requires the main thread (see the module
        # docstring and docs/facts/houdini.md §9). Direct construction
        # bypasses the policy entirely and works the same under Houdini and
        # outside it.
        if sys.platform == "win32":
            self.loop: asyncio.AbstractEventLoop = asyncio.ProactorEventLoop()
        else:
            self.loop = asyncio.SelectorEventLoop()
        self._ready = threading.Event()

        self._conn: acp.ClientSideConnection | None = None
        self._process: subprocess.Popen | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader: asyncio.StreamReader | None = None
        self._stderr_reader: asyncio.StreamReader | None = None
        self._stderr_task: asyncio.Task | None = None
        self._exit_watch_task: asyncio.Task | None = None
        self._closing = False
        #: Resolved with the return code once the agent process dies. This
        #: is how `initialize` learns that a reply will never arrive,
        #: instead of waiting for it forever.
        self._exited: asyncio.Future | None = None
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES * 4)

        self._agent_info: AgentInfo | None = None
        self._pending_permissions: dict[str, asyncio.Future] = {}
        # Cache of availableModes per session — current_mode_update only
        # carries the new currentModeId, while modes_changed must hand out a
        # full SessionModeState (see docs/architecture.md §6).
        self._session_modes: dict[str, list] = {}

    # --- loop plumbing ------------------------------------------------

    def run(self) -> None:  # noqa: D102 - overrides QThread.run
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        self.loop.run_forever()
        self.loop.close()

    def wait_until_ready(self, timeout: float = 5.0) -> None:
        self._ready.wait(timeout)

    def submit(self, coro) -> "asyncio.Future":
        """Schedule a coroutine on the worker's loop from ANY other thread."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def request_loop_stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)

    # --- ACP Client protocol (called by `acp` from coroutines on this same loop) --

    def on_connect(self, conn: Any) -> None:
        # `conn` is already known to us as the result of `spawn_agent_process`
        # — nothing extra to store here, this is a purely protocol-level
        # callback.
        pass

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        kind = update.session_update
        if kind == "agent_message_chunk":
            text = _chunk_text(update.content)
            self.message_chunk.emit(session_id, update.message_id or "", text)
        elif kind == "agent_thought_chunk":
            text = _chunk_text(update.content)
            self.thought_chunk.emit(session_id, update.message_id or "", text)
        elif kind == "tool_call":
            self.tool_call.emit(session_id, update)
        elif kind == "tool_call_update":
            self.tool_call_update.emit(session_id, update)
        elif kind == "plan":
            self.plan_changed.emit(session_id, list(update.entries))
        elif kind == "available_commands_update":
            self.commands_changed.emit(session_id, list(update.available_commands))
        elif kind == "current_mode_update":
            available = self._session_modes.get(session_id, [])
            state = SessionModeState(
                current_mode_id=update.current_mode_id, available_modes=available
            )
            self.modes_changed.emit(session_id, state)
        elif kind == "usage_update":
            # The update IS the usage report — `used`/`size`/`cost` live on it
            # directly. There is no `.usage` attribute, and reading one raised
            # AttributeError on every single token-counter update.
            self.usage_changed.emit(session_id, update)
        elif kind == "config_option_update":
            options = _config_options_from(getattr(update, "config_options", None))
            if options:
                self.config_options_changed.emit(session_id, options)
        # user_message_chunk — the panel already drew its own input when it
        # sent it, so it doesn't need the agent's echo; session_info_update is
        # out of scope for v1 and silently ignored.

    async def request_permission(
        self, session_id: str, tool_call: Any, options: list, **kwargs: Any
    ):
        request_key = str(uuid.uuid4())
        future: asyncio.Future = self.loop.create_future()
        self._pending_permissions[request_key] = future
        self.permission_requested.emit(request_key, session_id, tool_call, list(options))
        option_id = await future
        if option_id is None:
            return acp.RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))
        outcome = AllowedOutcome(outcome="selected", option_id=option_id)
        return acp.RequestPermissionResponse(outcome=outcome)

    def resolve_permission(self, request_key: str, option_id: str | None) -> None:
        """Called from the MAIN thread — resolves a Future from another thread."""

        def _resolve() -> None:
            future = self._pending_permissions.pop(request_key, None)
            if future is not None and not future.done():
                future.set_result(option_id)

        self.loop.call_soon_threadsafe(_resolve)

    # --- operations scheduled by the facade via submit() ------------------------

    async def do_start(self, spec: "LaunchSpec", cwd: str) -> None:
        self._closing = False
        try:
            # `acp.spawn_agent_process` doesn't work inside Houdini (see the
            # module docstring) — we spawn the process and hook its pipes
            # into the loop ourselves, the same public way it does
            # internally, minus the step that needs a child watcher.
            env = dict(acp.default_environment())
            env.update(spec.env)
            process = subprocess.Popen(
                [spec.command, *spec.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                cwd=cwd,
            )
            self._process = process

            reader = asyncio.StreamReader(limit=_STDIO_BUFFER_LIMIT, loop=self.loop)
            reader_protocol = asyncio.StreamReaderProtocol(reader, loop=self.loop)
            await self.loop.connect_read_pipe(lambda: reader_protocol, process.stdout)

            write_transport, write_protocol = await self.loop.connect_write_pipe(
                lambda: asyncio.streams.FlowControlMixin(loop=self.loop), process.stdin
            )
            writer = asyncio.StreamWriter(write_transport, write_protocol, reader, self.loop)

            stderr_reader = asyncio.StreamReader(limit=_STDIO_BUFFER_LIMIT, loop=self.loop)
            stderr_protocol = asyncio.StreamReaderProtocol(stderr_reader, loop=self.loop)
            await self.loop.connect_read_pipe(lambda: stderr_protocol, process.stderr)

            self._reader = reader
            self._writer = writer
            self._stderr_reader = stderr_reader
            conn = acp.connect_to_agent(self, writer, reader)
            self._conn = conn

            self._exited = self.loop.create_future()
            self._stderr_task = self.loop.create_task(self._pump_stderr())
            self._exit_watch_task = self.loop.create_task(self._watch_process_exit(process))

            init = await self._initialize_or_fail(conn)
            if init is None:
                return
        except Exception as exc:  # noqa: BLE001 - anything at startup -> failed, not a crash
            await self._cleanup()
            self.failed.emit(self._describe_failure(str(exc)))
            return

        self._agent_info = _agent_info_from(init)
        self.connected.emit(self._agent_info)


    async def _initialize_or_fail(self, conn) -> "Any | None":
        """`initialize`, but not forever.

        A bare `await conn.initialize(...)` is what made the panel print
        "Launching claude-acp…" for an artist and then hang indefinitely.
        The agent had died right after starting (in that case, because of an
        npx path that didn't exist), the process closed its pipes, a reply
        to `initialize` could no longer arrive under any circumstances, and
        we just kept waiting.

        We now wait for three outcomes at once: a reply, the process dying,
        and an overall time ceiling. Returns None if the connection didn't
        come up — the `failed` signal has already been emitted in that case.
        """
        init_task = self.loop.create_task(
            conn.initialize(
                protocol_version=acp.PROTOCOL_VERSION,
                client_capabilities=ClientCapabilities(),  # we don't declare fs/terminal
                client_info=Implementation(name="houdini-agent-panel", version=__version__),
            )
        )
        waiters = {init_task, self._exited}
        done, _pending = await asyncio.wait(
            waiters, return_when=asyncio.FIRST_COMPLETED, timeout=_CONNECT_TIMEOUT
        )

        if init_task in done:
            return init_task.result()

        init_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await init_task

        if self._exited in done:
            code = self._exited.result()
            reason = f"agent exited with code {code} without replying to initialize"
        else:
            reason = (
                f"agent did not reply to initialize within {int(_CONNECT_TIMEOUT)}s "
                "and appears to have failed to start"
            )

        await self._cleanup()
        self.failed.emit(self._describe_failure(reason))
        return None

    def _describe_failure(self, reason: str) -> str:
        """Append the agent's stderr tail to the reason.

        Without it, a message like "agent exited with code 1" tells a human
        nothing, while the whole story is usually right there: a missing
        file, missing permissions, a missing environment variable.
        """
        tail = [line for line in self._stderr_tail if line.strip()]
        if not tail:
            return reason
        return reason + "\n\n" + "\n".join(tail[-_STDERR_TAIL_LINES:])

    async def do_stop(self) -> None:
        """The same shutdown ladder that `spawn_agent_process.__aexit__` used
        to do: close the ACP connection, then stdin (EOF -> drain -> close),
        then wait for/kill the process. We carry it here by hand — when
        `spawn_agent_process` went away, so did its automatic call to it."""
        self._closing = True
        for task in (self._stderr_task, self._exit_watch_task):
            if task is not None:
                task.cancel()

        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()
            self._conn = None

        await self._close_writer()
        await self._terminate_process()

        self._process = None
        self._reader = None
        self._stderr_reader = None
        self._agent_info = None

    async def do_authenticate(self, method_id: str) -> None:
        if self._conn is None:
            return
        try:
            await self._conn.authenticate(method_id=method_id)
        except acp.RequestError as exc:
            self.error.emit("", str(exc))

    async def do_logout(self) -> None:
        """`ClientSideConnection` in agent-client-protocol 0.12.0 doesn't wrap
        `logout` in its own method (unlike `authenticate`), even though
        `AGENT_METHODS["logout"]` and the `LogoutRequest`/`LogoutResponse`
        schema are declared — verified by reading
        `acp/client/connection.py`. We send it via the same low-level
        `send_request` that every other method on the class uses
        internally."""
        if self._conn is None:
            return
        try:
            await self._conn._conn.send_request(acp.AGENT_METHODS["logout"], {})
        except acp.RequestError as exc:
            self.error.emit("", str(exc))
            return
        # A successful logout returns the agent to its "pre-login" state —
        # the login screen should reappear with the same authMethods as at
        # initialize. We don't add a separate signal for this: it's the
        # same state as "login required", which the panel already knows how
        # to show.
        methods = list(self._agent_info.auth_methods) if self._agent_info else []
        self.auth_required.emit(methods)

    async def do_new_session(self, cwd: str, mcp_servers: list[dict]) -> None:
        if self._conn is None:
            self.error.emit("", "no connection to the agent")
            return
        try:
            servers = _build_mcp_servers(mcp_servers)
            response = await self._conn.new_session(cwd=cwd, mcp_servers=servers)
        except acp.RequestError as exc:
            if not self._emit_if_auth_required(exc):
                self.error.emit("", str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self.error.emit("", str(exc))
            return

        current_mode_id = None
        available_modes: list[_SessionMode] = []
        if response.modes is not None:
            current_mode_id = response.modes.current_mode_id
            self._session_modes[response.session_id] = list(response.modes.available_modes)
            available_modes = [
                _SessionMode(id=m.id, name=m.name, description=m.description or "")
                for m in response.modes.available_modes
            ]

        state = SessionState(
            session_id=response.session_id,
            title="New conversation",
            cwd=cwd,
            created_at=time.time(),
            current_mode_id=current_mode_id,
            available_modes=available_modes,
            available_commands=[],
        )
        self.session_started.emit(response.session_id, state)
        # The model picker lives here in ACP: agents expose model, reasoning
        # effort and fast mode as session config options, not as a dedicated
        # protocol concept. Nobody read them before, so the chip stayed
        # permanently hidden.
        options = _config_options_from(getattr(response, "config_options", None))
        if options:
            self.config_options_changed.emit(response.session_id, options)

    async def do_prompt(self, session_id: str, blocks: list[dict]) -> None:
        if self._conn is None:
            self.error.emit(session_id, "no connection to the agent")
            return
        content = [_build_content_block(block) for block in blocks]
        try:
            response = await self._conn.prompt(session_id=session_id, prompt=content)
        except acp.RequestError as exc:
            if not self._emit_if_auth_required(exc):
                self.error.emit(session_id, str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self.error.emit(session_id, str(exc))
            return
        self.turn_finished.emit(session_id, response.stop_reason)

    async def do_cancel(self, session_id: str) -> None:
        if self._conn is not None:
            await self._conn.cancel(session_id=session_id)

    async def do_set_config_option(self, session_id: str, config_id: str, value: str) -> None:
        if self._conn is None:
            return
        try:
            await self._conn.set_config_option(
                config_id=config_id, session_id=session_id, value=value
            )
        except acp.RequestError as exc:
            if not self._emit_if_auth_required(exc):
                self.error.emit(session_id, str(exc))
        except Exception as exc:  # noqa: BLE001
            self.error.emit(session_id, str(exc))

    async def do_set_mode(self, session_id: str, mode_id: str) -> None:
        if self._conn is None:
            return
        try:
            await self._conn.set_session_mode(session_id=session_id, mode_id=mode_id)
        except acp.RequestError as exc:
            if not self._emit_if_auth_required(exc):
                self.error.emit(session_id, str(exc))

    # --- internals -----------------------------------------------------------

    def _emit_if_auth_required(self, exc: "acp.RequestError") -> bool:
        if getattr(exc, "code", None) != _AUTH_REQUIRED_CODE:
            return False
        methods = list(self._agent_info.auth_methods) if self._agent_info else []
        self.auth_required.emit(methods)
        return True

    async def _pump_stderr(self) -> None:
        """Reads the agent's stderr continuously — an unread pipe fills up
        and hangs the agent process (see docs/facts/acp-sdk.md §1).

        `process.stderr` from `subprocess.Popen` is a plain blocking file
        object; reading it directly in a coroutine would block the whole
        loop. So stderr gets its own `StreamReader`, hooked up via
        `connect_read_pipe` in `do_start`, exactly like stdout."""
        if self._stderr_reader is None:
            return
        try:
            while True:
                line = await self._stderr_reader.readline()
                if not line:
                    break
                text = line.decode(errors="replace").rstrip("\n")
                self._stderr_tail.append(text)
                self.log_line.emit(text)
        except asyncio.CancelledError:
            pass

    async def _watch_process_exit(self, process: subprocess.Popen) -> None:
        # Popen.wait() is blocking (os.waitpid under the hood) — run it in
        # an executor so it doesn't hang the loop for the agent's whole
        # lifetime.
        try:
            code = await self._await_process(process)
        except asyncio.CancelledError:
            return

        exited = self._exited
        if exited is not None and not exited.done():
            exited.set_result(code)
        if not self._closing:
            self.disconnected.emit(f"agent process exited unexpectedly (code {code})")

    async def _await_process(self, process: subprocess.Popen) -> int:
        return await self.loop.run_in_executor(None, process.wait)

    async def _close_writer(self) -> None:
        writer = self._writer
        self._writer = None
        if writer is None:
            return
        try:
            writer.write_eof()
        except (AttributeError, OSError, RuntimeError):
            writer.close()
        with contextlib.suppress(Exception):
            await writer.drain()
        with contextlib.suppress(Exception):
            writer.close()
        with contextlib.suppress(Exception):
            await writer.wait_closed()

    async def _terminate_process(self) -> None:
        """The same ladder as `spawn_stdio_transport`: wait, `terminate()`,
        wait some more, `kill()`. A hung agent has no right to hold up
        Houdini's shutdown — hence the timeout at every step."""
        process = self._process
        if process is None:
            return
        try:
            await asyncio.wait_for(self._await_process(process), timeout=2.0)
            return
        except asyncio.TimeoutError:
            pass
        process.terminate()
        try:
            await asyncio.wait_for(self._await_process(process), timeout=2.0)
            return
        except asyncio.TimeoutError:
            pass
        process.kill()
        with contextlib.suppress(Exception):
            await self._await_process(process)

    async def _cleanup(self) -> None:
        """Roll back a partially started launch — the same thing as
        `do_stop`, but without waiting for a "normal" stop (`_conn` may not
        even have come up)."""
        for task in (self._stderr_task, self._exit_watch_task):
            if task is not None:
                task.cancel()
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()
            self._conn = None
        await self._close_writer()
        await self._terminate_process()
        self._process = None
        self._reader = None
        self._stderr_reader = None


#: Signals forwarded 1:1 from the worker to the facade (see AcpClient.__init__).
_FORWARDED_SIGNALS = (
    "connected",
    "disconnected",
    "failed",
    "auth_required",
    "log_line",
    "session_started",
    "modes_changed",
    "commands_changed",
    "config_options_changed",
    "message_chunk",
    "thought_chunk",
    "tool_call",
    "tool_call_update",
    "plan_changed",
    "usage_changed",
    "turn_finished",
    "error",
    "permission_requested",
)


class AcpClient(QtCore.QObject):
    """Facade on the MAIN thread. The only thing the UI sees.

    All the signals below are the same ones `AcpWorker` has, but AcpClient
    lives on the main thread (never moved with `moveToThread`), so
    subscribing to its signals from the UI doesn't require thinking about
    threads — they've already been forwarded by the worker.
    """

    connected = Signal(object)
    disconnected = Signal(str)
    failed = Signal(str)
    auth_required = Signal(list)
    log_line = Signal(str)

    session_started = Signal(str, object)
    modes_changed = Signal(str, object)
    commands_changed = Signal(str, list)
    config_options_changed = Signal(str, list)

    message_chunk = Signal(str, str, str)
    thought_chunk = Signal(str, str, str)
    tool_call = Signal(str, object)
    tool_call_update = Signal(str, object)
    plan_changed = Signal(str, list)
    usage_changed = Signal(str, object)
    turn_finished = Signal(str, str)
    error = Signal(str, str)

    permission_requested = Signal(str, str, object, list)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._worker: AcpWorker | None = None
        #: Workers that have already been stopped. Kept alive only so Qt
        #: never sees a `QThread` freed out from under it; nothing is ever
        #: read back out of here.
        self._retired: list[AcpWorker] = []
        self._agent_info: AgentInfo | None = None
        self._running = False
        self._spawn_worker()

    def _spawn_worker(self) -> "AcpWorker":
        """Bring up a fresh worker thread and wire it to this facade.

        Rebuilding the worker rather than the whole `AcpClient` is what makes
        an agent switch possible at all. Every panel wires its slots to THIS
        object's signals once, and a stopped worker's asyncio loop is closed
        for good — `run_coroutine_threadsafe` on it raises "Event loop is
        closed". Before this, switching agents killed the worker and then
        submitted `do_start` into the corpse: the chip showed the new agent
        and nothing else ever happened again, in that panel, until Houdini
        was restarted.
        """
        worker = AcpWorker()

        # Connection order matters: Qt calls multiple slots of the same
        # signal in the order they were connected. Internal state
        # (`_running`, `_agent_info`) must be updated BEFORE the forwarding
        # reaches external subscribers — otherwise a UI slot reacting to
        # `connected` might see a not-yet-updated `is_running()`/`agent_info()`.
        worker.connected.connect(self._on_connected)
        worker.disconnected.connect(self._on_stopped)
        worker.failed.connect(self._on_stopped)
        for name in _FORWARDED_SIGNALS:
            getattr(worker, name).connect(getattr(self, name).emit)

        worker.start()
        worker.wait_until_ready()
        self._worker = worker
        return worker

    def _live_worker(self) -> "AcpWorker":
        """The worker that can still accept work — a new one if the old died."""
        worker = self._worker
        if worker is None or not worker.isRunning() or worker.loop.is_closed():
            return self._spawn_worker()
        return worker

    def _submit(self, make_coro) -> None:
        """Schedule work on the live worker, or drop it if there is none.

        Takes a factory rather than a coroutine so that nothing is ever
        created for a dead worker — an un-awaited coroutine object would
        only produce a RuntimeWarning nobody reads. Deliberately does NOT
        resurrect the worker: with no agent running there is nothing for
        `prompt`/`cancel`/`set_mode` to talk to, and quietly starting a
        thread for them would hide that.
        """
        worker = self._worker
        if worker is None or not worker.isRunning() or worker.loop.is_closed():
            return
        worker.submit(make_coro(worker))

    # --- connection lifecycle -----------------------------------------

    def start(self, spec: "LaunchSpec", *, cwd: str) -> None:
        worker = self._live_worker()
        worker.submit(worker.do_start(spec, cwd))

    def stop(self) -> None:
        """Reliable stop: close the connection, wait for the process, stop
        the loop, join the thread with a timeout. A hung agent has no right
        to hold up Houdini's shutdown — hence the timeout at every step.

        The client itself survives: `start()` builds a new worker. Callers
        keep their signal connections, which is the whole point — they are
        wired to this object, not to the thread behind it.
        """
        worker = self._worker
        if worker is None or not worker.isRunning():
            return
        future = worker.submit(worker.do_stop())
        with contextlib.suppress(Exception):
            future.result(timeout=10.0)
        worker.request_loop_stop()
        worker.wait(5000)
        self._worker = None
        self._retired.append(worker)
        self._running = False
        self._agent_info = None
        self.disconnected.emit("")

    def is_running(self) -> bool:
        return self._running

    def agent_info(self) -> AgentInfo | None:
        return self._agent_info

    # --- sessions ---------------------------------------------------------------

    def authenticate(self, method_id: str) -> None:
        self._submit(lambda w: w.do_authenticate(method_id))

    def logout(self) -> None:
        """Only if `agent_info().supports_logout` — otherwise the agent
        never declared this method, and calling it is guaranteed to error
        out."""
        self._submit(lambda w: w.do_logout())

    def new_session(self, *, cwd: str, mcp_servers: list[dict]) -> None:
        self._submit(lambda w: w.do_new_session(cwd, mcp_servers))

    def prompt(self, session_id: str, blocks: list[dict]) -> None:
        self._submit(lambda w: w.do_prompt(session_id, blocks))

    def cancel(self, session_id: str) -> None:
        self._submit(lambda w: w.do_cancel(session_id))

    def set_mode(self, session_id: str, mode_id: str) -> None:
        self._submit(lambda w: w.do_set_mode(session_id, mode_id))

    def set_config_option(self, session_id: str, config_id: str, value: str) -> None:
        """Change an agent-side setting: model, reasoning effort, fast mode."""
        self._submit(lambda w: w.do_set_config_option(session_id, config_id, value))

    def answer_permission(self, request_key: str, option_id: str | None) -> None:
        """`option_id=None` — "cancelled", results in a `DeniedOutcome`."""
        worker = self._worker
        if worker is None or not worker.isRunning() or worker.loop.is_closed():
            return
        worker.resolve_permission(request_key, option_id)

    # --- internals ----------------------------------------------------------

    def _on_connected(self, info: AgentInfo) -> None:
        self._agent_info = info
        self._running = True

    def _on_stopped(self, _reason: str) -> None:
        self._running = False
        self._agent_info = None

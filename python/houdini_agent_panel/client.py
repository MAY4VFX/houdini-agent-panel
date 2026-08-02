"""ACP-клиент поверх `agent-client-protocol`, обёрнутый в Qt-сигналы.

Самая рискованная часть проекта (см. docs/architecture.md §6): SDK
асинхронный, Qt синхронный, агент — чужой процесс. Правила, которых держится
этот файл:

- asyncio-цикл живёт на своём `QThread` (`AcpWorker`), `hou` из него не
  трогаем никогда — вся работа со сценой идёт через отдельный процесс fx.
- Наружу из воркера — только Qt-сигналы (доставка в другой поток — забота
  Qt: очередь сигналов автоматически становится потокобезопасной, когда
  объект-получатель живёт в другом потоке, чем поток, из которого позвали
  `.emit()`). Внутрь — только `AcpWorker.submit()`
  (`asyncio.run_coroutine_threadsafe`).
- `qasync` не используем: свой раннер цикла на выделенном `QThread` проще и
  не тянет лишнюю зависимость.

`AcpWorker` — это одновременно и раннер цикла (`run()` переопределён вместо
Qt event loop), и реализация ACP `Client`-протокола (`session_update`,
`request_permission`, `on_connect` дёргаются самим `acp` изнутри корутин,
работающих на этом же цикле) — оба назначения из докстринга архитектуры
(«владеет циклом, процессом агента, соединением» и «живёт на рабочем
потоке») естественно сходятся в одном объекте: то, что обслуживает колбэки
агента, физически и есть тот самый рабочий поток.

**Houdini подменяет asyncio (`haio`, см. docs/facts/houdini.md §9).** Внутри
Houdini `asyncio.get_event_loop_policy()` отдаёт `haio.HoudiniEventLoopPolicy`,
и через неё:

1. `asyncio.new_event_loop()` возвращает `haio.HoudiniEventLoop`, чей
   `run_forever()` требует главный поток — на нашем рабочем `QThread` он
   валится `RuntimeError`. Лечится тем, что класс цикла берётся напрямую
   классом, а не через политику: `asyncio.SelectorEventLoop()` (POSIX) /
   `asyncio.ProactorEventLoop()` (Windows).
2. `acp.spawn_agent_process()` не работает вообще: он построен на
   `asyncio.create_subprocess_exec`, а тот на POSIX идёт за child watcher'ом
   через ГЛОБАЛЬНУЮ политику (`get_event_loop_policy().get_child_watcher()`),
   и `haio` бросает оттуда `NotImplementedError` — независимо от того, какой
   именно объект цикла мы используем сами. Обход: поднимать процесс обычным
   `subprocess.Popen` (не идёт за child watcher'ом вообще) и заводить его
   каналы в цикл через `connect_read_pipe`/`connect_write_pipe` — этому
   публичному API watcher не нужен — а связку отдавать в `acp.connect_to_agent`
   (документированная байтовая форма подключения, см. facts/acp-sdk.md §1).

Это верно и вне Houdini (стоковая политика тоже прекрасно живёт с
`SelectorEventLoop()`, взятым напрямую), так что мы всегда идём этим путём,
не разветвляя код на «под Houdini» и «в тестах».
"""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import sys
import threading
import time
import uuid
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

if TYPE_CHECKING:  # только для типов — runtime.py не обязан существовать при импорте
    from .runtime import LaunchSpec

#: Лимит буфера stdio-транспорта. 64 КБ по умолчанию у asyncio — картинка в
#: base64 в session/update его переполнит и повесит соединение (см.
#: docs/facts/acp-sdk.md §1). Столько же выставляет агентская сторона SDK
#: (`run_agent`) по умолчанию — держим клиента симметричным.
_STDIO_BUFFER_LIMIT = 50 * 1024 * 1024

#: Код ошибки "нужен логин" — соглашение самого ACP (application-specific
#: диапазон JSON-RPC, не стандартный -32700..-32603).
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
    """Плоский снимок `initialize`, чтобы UI не тянул pydantic-модели ACP.

    `supports_*` — единственный источник правды о том, рисовать ли кнопку
    вложений/микрофон/т.п.: агент не умеет — контрол не рисуется.
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
        supports_logout=auth_caps is not None and getattr(auth_caps, "logout", None) is not None,
        auth_methods=tuple(
            AuthMethod(id=m.id, name=m.name, description=getattr(m, "description", None) or "")
            for m in (init.auth_methods or [])
        ),
    )


def _build_mcp_servers(entries: list[dict]) -> list[McpServerStdio]:
    """`scene.mcp_servers()` -> объекты, которые понимает `new_session`."""
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
    """Готовый ACP-блок из словаря `Composer.submitted`."""
    kind = block.get("type")
    cls = _CONTENT_BLOCK_TYPES.get(kind)
    if cls is None:
        raise ValueError(f"неизвестный тип контент-блока: {kind!r}")
    return cls(**block)


def _chunk_text(content: Any) -> str:
    return content.text if getattr(content, "type", None) == "text" else ""


class AcpWorker(QtCore.QThread):
    """Живёт на рабочем потоке. Владеет циклом, процессом агента, соединением.

    `run()` переопределён: вместо `QThread`'ного event loop крутит
    `asyncio`-цикл (`loop.run_forever()`). Методы `session_update` /
    `request_permission` / `on_connect` реализуют ACP `Client` через
    duck-typing (протокол — не ABC, наследоваться не обязательно, см.
    docs/facts/acp-sdk.md §2) и вызываются самим `acp` изнутри корутин,
    работающих на этом же цикле.
    """

    # --- жизненный цикл соединения ---------------------------------------
    connected = Signal(object)  # AgentInfo
    disconnected = Signal(str)  # причина, "" при штатном стопе
    failed = Signal(str)  # текст для человека
    auth_required = Signal(list)  # list[AuthMethod]
    log_line = Signal(str)  # stderr агента

    # --- сессии -----------------------------------------------------------
    session_started = Signal(str, object)  # session_id, SessionState
    modes_changed = Signal(str, object)  # session_id, acp.schema.SessionModeState
    commands_changed = Signal(str, list)  # session_id, list[acp.schema.AvailableCommand]

    # --- лента --------------------------------------------------------------
    message_chunk = Signal(str, str, str)  # session_id, message_id, text
    thought_chunk = Signal(str, str, str)
    tool_call = Signal(str, object)  # session_id, acp.schema.ToolCallStart
    tool_call_update = Signal(str, object)  # session_id, acp.schema.ToolCallProgress
    plan_changed = Signal(str, list)  # session_id, list[acp.schema.PlanEntry]
    usage_changed = Signal(str, object)  # session_id, acp.schema.Usage
    turn_finished = Signal(str, str)  # session_id, stop_reason
    error = Signal(str, str)  # session_id (может быть ""), текст

    # --- разрешения ---------------------------------------------------------
    permission_requested = Signal(str, str, object, list)
    # request_key, session_id, ToolCallUpdate, list[PermissionOption]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        # Цикл создаётся здесь (на потоке-владельце AcpWorker, т.е. пока ещё
        # главном — до start()), а не в run(): submit() может понадобиться
        # раньше, чем поток реально стартовал, и ссылка на loop должна быть
        # валидна сразу после конструктора.
        #
        # Класс цикла берём НАПРЯМУЮ, а не через asyncio.new_event_loop():
        # внутри Houdini та идёт через подменённую политику `haio`, чей
        # run_forever() требует главный поток (см. докстринг модуля и
        # docs/facts/houdini.md §9). Прямая конструкция обходит политику
        # целиком и работает одинаково что под Houdini, что вне неё.
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

        self._agent_info: AgentInfo | None = None
        self._pending_permissions: dict[str, asyncio.Future] = {}
        # Кэш availableModes на сессию — current_mode_update несёт только
        # новый currentModeId, а modes_changed обязан отдавать полный
        # SessionModeState (см. docs/architecture.md §6).
        self._session_modes: dict[str, list] = {}

    # --- инфраструктура цикла ------------------------------------------------

    def run(self) -> None:  # noqa: D102 - переопределение QThread.run
        asyncio.set_event_loop(self.loop)
        self._ready.set()
        self.loop.run_forever()
        self.loop.close()

    def wait_until_ready(self, timeout: float = 5.0) -> None:
        self._ready.wait(timeout)

    def submit(self, coro) -> "asyncio.Future":
        """Запланировать корутину на цикле воркера из ЛЮБОГО другого потока."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop)

    def request_loop_stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)

    # --- ACP Client protocol (вызывается `acp` из корутин на этом же цикле) --

    def on_connect(self, conn: Any) -> None:
        # `conn` нам уже известен как результат `spawn_agent_process` —
        # отдельно сохранять нечего, это чисто протокольный колбэк.
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
            self.usage_changed.emit(session_id, update.usage)
        # user_message_chunk — панель уже нарисовала свой ввод сама при
        # отправке, эхо от агента ей не нужно; config_option_update и
        # session_info_update — вне охвата v1, тихо игнорируем.

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
        """Вызывается из ГЛАВНОГО потока — резолвит Future из чужого потока."""

        def _resolve() -> None:
            future = self._pending_permissions.pop(request_key, None)
            if future is not None and not future.done():
                future.set_result(option_id)

        self.loop.call_soon_threadsafe(_resolve)

    # --- операции, планируемые фасадом через submit() ------------------------

    async def do_start(self, spec: "LaunchSpec", cwd: str) -> None:
        self._closing = False
        try:
            # `acp.spawn_agent_process` не годится внутри Houdini (см.
            # докстринг модуля) — поднимаем процесс и заводим его каналы в
            # цикл сами, тем же публичным путём, что и она изнутри, минус
            # шаг, которому нужен child watcher.
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

            self._stderr_task = self.loop.create_task(self._pump_stderr())
            self._exit_watch_task = self.loop.create_task(self._watch_process_exit(process))

            init = await conn.initialize(
                protocol_version=acp.PROTOCOL_VERSION,
                client_capabilities=ClientCapabilities(),  # fs/terminal не объявляем
                client_info=Implementation(name="houdini-agent-panel", version=__version__),
            )
        except Exception as exc:  # noqa: BLE001 - что угодно на старте -> failed, не краш
            await self._cleanup()
            self.failed.emit(str(exc))
            return

        self._agent_info = _agent_info_from(init)
        self.connected.emit(self._agent_info)

    async def do_stop(self) -> None:
        """Та же лестница останова, что раньше делал `spawn_agent_process.__aexit__`:
        закрыть ACP-соединение, потом stdin (EOF → drain → close), потом
        подождать/добить процесс. Переносим её сюда вручную — вместе с
        `spawn_agent_process` ушёл и её автоматический вызов."""
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

    async def do_new_session(self, cwd: str, mcp_servers: list[dict]) -> None:
        if self._conn is None:
            self.error.emit("", "нет соединения с агентом")
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
            title="Новый разговор",
            cwd=cwd,
            created_at=time.time(),
            current_mode_id=current_mode_id,
            available_modes=available_modes,
            available_commands=[],
        )
        self.session_started.emit(response.session_id, state)

    async def do_prompt(self, session_id: str, blocks: list[dict]) -> None:
        if self._conn is None:
            self.error.emit(session_id, "нет соединения с агентом")
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

    async def do_set_mode(self, session_id: str, mode_id: str) -> None:
        if self._conn is None:
            return
        try:
            await self._conn.set_session_mode(session_id=session_id, mode_id=mode_id)
        except acp.RequestError as exc:
            if not self._emit_if_auth_required(exc):
                self.error.emit(session_id, str(exc))

    # --- внутреннее -----------------------------------------------------------

    def _emit_if_auth_required(self, exc: "acp.RequestError") -> bool:
        if getattr(exc, "code", None) != _AUTH_REQUIRED_CODE:
            return False
        methods = list(self._agent_info.auth_methods) if self._agent_info else []
        self.auth_required.emit(methods)
        return True

    async def _pump_stderr(self) -> None:
        """Читает stderr агента непрерывно — незачитанный пайп переполнится
        и подвесит процесс агента (см. docs/facts/acp-sdk.md §1).

        `process.stderr` от `subprocess.Popen` — обычный блокирующий файловый
        объект; читать его напрямую в корутине значило бы блокировать весь
        цикл. Поэтому у stderr свой `StreamReader`, заведённый через
        `connect_read_pipe` в `do_start`, точно как у stdout."""
        if self._stderr_reader is None:
            return
        try:
            while True:
                line = await self._stderr_reader.readline()
                if not line:
                    break
                self.log_line.emit(line.decode(errors="replace").rstrip("\n"))
        except asyncio.CancelledError:
            pass

    async def _watch_process_exit(self, process: subprocess.Popen) -> None:
        # Popen.wait() блокирующий (os.waitpid под капотом) — в executor'е,
        # чтобы не подвесить цикл на весь процесс жизни агента.
        try:
            code = await self._await_process(process)
        except asyncio.CancelledError:
            return
        if not self._closing:
            self.disconnected.emit(f"agent-процесс неожиданно завершился (код {code})")

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
        """Та же лестница, что у `spawn_stdio_transport`: подождать,
        `terminate()`, подождать ещё, `kill()`. Зависший агент не имеет права
        держать закрытие Houdini — отсюда таймауты на каждом шаге."""
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
        """Откат частично поднятого старта — то же самое, что `do_stop`,
        но без ожидания «штатного» останова (`_conn` мог даже не появиться)."""
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


#: Сигналы, форвардящиеся 1:1 с воркера на фасад (см. AcpClient.__init__).
_FORWARDED_SIGNALS = (
    "connected",
    "disconnected",
    "failed",
    "auth_required",
    "log_line",
    "session_started",
    "modes_changed",
    "commands_changed",
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
    """Фасад на ГЛАВНОМ потоке. Единственное, что видит UI.

    Все сигналы ниже — те же самые, что и у `AcpWorker`, но AcpClient живёт в
    главном потоке (никогда не двигается `moveToThread`), поэтому подписка
    на его сигналы из UI не требует размышлений о потоках — они уже
    форварднуты воркером.
    """

    connected = Signal(object)
    disconnected = Signal(str)
    failed = Signal(str)
    auth_required = Signal(list)
    log_line = Signal(str)

    session_started = Signal(str, object)
    modes_changed = Signal(str, object)
    commands_changed = Signal(str, list)

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
        self._worker = AcpWorker()
        self._agent_info: AgentInfo | None = None
        self._running = False

        # Порядок связывания важен: Qt зовёт несколько слотов одного сигнала
        # в порядке подключения. Внутреннее состояние (`_running`,
        # `_agent_info`) обязано обновиться РАНЬШЕ, чем форвардинг долетит до
        # внешних подписчиков — иначе слот UI, сработавший на `connected`,
        # может увидеть ещё не обновлённый `is_running()`/`agent_info()`.
        self._worker.connected.connect(self._on_connected)
        self._worker.disconnected.connect(self._on_stopped)
        self._worker.failed.connect(self._on_stopped)
        for name in _FORWARDED_SIGNALS:
            getattr(self._worker, name).connect(getattr(self, name).emit)

        self._worker.start()
        self._worker.wait_until_ready()

    # --- жизненный цикл соединения -----------------------------------------

    def start(self, spec: "LaunchSpec", *, cwd: str) -> None:
        self._worker.submit(self._worker.do_start(spec, cwd))

    def stop(self) -> None:
        """Надёжный останов: закрыть соединение, дождаться процесса, погасить
        цикл, join потока с таймаутом. Зависший агент не имеет права держать
        закрытие Houdini — отсюда таймауты на каждом шаге."""
        if not self._worker.isRunning():
            return
        future = self._worker.submit(self._worker.do_stop())
        with contextlib.suppress(Exception):
            future.result(timeout=10.0)
        self._worker.request_loop_stop()
        self._worker.wait(5000)
        self._running = False
        self._agent_info = None
        self.disconnected.emit("")

    def is_running(self) -> bool:
        return self._running

    def agent_info(self) -> AgentInfo | None:
        return self._agent_info

    # --- сессии ---------------------------------------------------------------

    def authenticate(self, method_id: str) -> None:
        self._worker.submit(self._worker.do_authenticate(method_id))

    def new_session(self, *, cwd: str, mcp_servers: list[dict]) -> None:
        self._worker.submit(self._worker.do_new_session(cwd, mcp_servers))

    def prompt(self, session_id: str, blocks: list[dict]) -> None:
        self._worker.submit(self._worker.do_prompt(session_id, blocks))

    def cancel(self, session_id: str) -> None:
        self._worker.submit(self._worker.do_cancel(session_id))

    def set_mode(self, session_id: str, mode_id: str) -> None:
        self._worker.submit(self._worker.do_set_mode(session_id, mode_id))

    def answer_permission(self, request_key: str, option_id: str | None) -> None:
        """`option_id=None` — «отменено», уходит `DeniedOutcome`."""
        self._worker.resolve_permission(request_key, option_id)

    # --- внутреннее ----------------------------------------------------------

    def _on_connected(self, info: AgentInfo) -> None:
        self._agent_info = info
        self._running = True

    def _on_stopped(self, _reason: str) -> None:
        self._running = False
        self._agent_info = None

# `agent-client-protocol` (PyPI) 0.12.0 — точный справочник клиентской части

Пакет: `agent_client_protocol-0.12.0` (`import acp`).
Проверено чтением исходников в
`.../scratchpad/venv/lib/python3.14/site-packages/acp/` и живым `python -c "import acp; ..."`
на интерпретаторе `.../scratchpad/venv/bin/python`. Ссылки на строки — из файлов пакета.

Schema сгенерирована из `schema/meta.json`, ref `refs/tags/schema-v1.19.0`
(см. `acp/meta.py:1-2`). `PROTOCOL_VERSION = 1` (`acp/meta.py:49`).

## 0. Совместимость с Python 3.11 (Houdini 20.5)

`agent_client_protocol-0.12.0.dist-info/METADATA`:
```
Requires-Python: <3.15,>=3.10
Classifier: Programming Language :: Python :: 3.10
Classifier: Programming Language :: Python :: 3.11
Classifier: Programming Language :: Python :: 3.12
Classifier: Programming Language :: Python :: 3.13
Classifier: Programming Language :: Python :: 3.14
```
Ничего не требует >=3.12. Пакет официально поддерживает 3.10-3.14, значит встроенный
Python 3.11 в Houdini 20.5 подходит без оговорок. Модули используют
`from __future__ import annotations` и `X | Y` синтаксис в аннотациях (не в рантайме) —
это тоже безопасно на 3.11 благодаря отложенным аннотациям.

## 1. Как клиент поднимает агента как подпроцесс

Есть готовый асинхронный контекст-менеджер, специально под сценарий «мы клиент, агент —
внешний CLI-процесс», это то, что нужно панели:

```python
# acp/stdio.py:161-183
@asynccontextmanager
async def spawn_agent_process(
    to_client: Callable[[Agent], Client] | Client,
    command: str,
    *args: str,
    env: Mapping[str, str] | None = None,
    cwd: str | Path | None = None,
    transport_kwargs: Mapping[str, Any] | None = None,
    **connection_kwargs: Any,
) -> AsyncIterator[tuple[ClientSideConnection, aio_subprocess.Process]]:
    """Spawn an ACP agent subprocess and return a ClientSideConnection to it."""
```

Использование:
```python
async with acp.spawn_agent_process(my_client_impl, "claude-code-acp") as (conn, process):
    resp = await conn.initialize(protocol_version=acp.PROTOCOL_VERSION)
    ...
```
`to_client` — либо готовый объект, реализующий `Client`-протокол (см. §2), либо фабрика
`Callable[[Agent], Client]`, которой передаётся сам `conn` (как `Agent`) — удобно, если
клиенту нужно дергать агента в обратную сторону (например, отменить сессию из UI).
`ClientSideConnection` в `to_client(self)` вызывается синхронно в `__init__`
(`acp/client/connection.py:71`).

Под капотом — `acp/transports.py:47-119`, `spawn_stdio_transport`:
- собирает окружение через `default_environment()` (наследует только
  `HOME, LOGNAME, PATH, SHELL, TERM, USER` на POSIX — почти пустой env, не полный `os.environ`!
  надо явно передавать `env=` с нужными переменными, если агенту нужно что-то ещё,
  например `ANTHROPIC_API_KEY` или PATH до нод/питона проекта) и мержит с переданным `env`.
- `asyncio.create_subprocess_exec(command, *args, stdin=PIPE, stdout=PIPE, stderr=PIPE, env=merged_env, cwd=...)`.
- при выходе из `async with`: graceful shutdown — `stdin.write_eof()` → `drain()` → `close()`,
  затем `wait_for(process.wait(), timeout=2.0)`, при таймауте `terminate()`, ещё раз таймаут,
  затем `kill()`. Параметр `shutdown_timeout` настраиваемый через `transport_kwargs`.
- `stderr` подпроцесса по умолчанию — отдельный `PIPE` (`aio_subprocess.PIPE`), не смешивается
  со stdout; его надо читать отдельно, иначе буфер может заполниться и подвиснуть.

`spawn_agent_process` — это высокоуровневый хелпер (создаёт `ClientSideConnection` сам).
Если нужен только низкоуровневый `Connection` без готовой связки методов — есть
`spawn_stdio_connection(handler, command, *args, ...)` (`acp/stdio.py:143-158`), но для
клиента панели это не нужно: `spawn_agent_process` подходит напрямую.

Обратный хелпер `spawn_client_process` (агент спавнит клиента) — не нужен для этого проекта,
панель именно клиент.

Буфер stdio по умолчанию у низкоуровневых `stdio_streams()` — 64KB (`asyncio.StreamReader`
default), но `run_agent()` (агентская сторона) поднимает лимит до 50MB
(`DEFAULT_STDIO_BUFFER_LIMIT_BYTES = 50 * 1024 * 1024`, `acp/core.py:36`) — это константа
для агентской стороны через `run_agent`; `spawn_agent_process`/`spawn_stdio_transport`
принимают свой `limit` через `transport_kwargs={"limit": ...}`, но по умолчанию НЕ поднимают
его до 50MB (default `limit=None` → `asyncio.create_subprocess_exec` без `limit=` → стандартный
64KB reader limit). **Для больших base64-картинок/аудио от агента может понадобиться явно
передать `transport_kwargs={"limit": 50*1024*1024}`.**

## 2. Класс, который реализует КЛИЕНТ (колбэки, которые агент зовёт у нас)

`acp.interfaces.Client` — это `typing.Protocol` (structural typing), **не ABC**:
```python
>>> acp.Client.__mro__
(<class 'acp.interfaces.Client'>, <class 'typing.Protocol'>, <class 'typing.Generic'>, <class 'object'>)
```
Наследоваться от него не обязательно — маршрутизация (`acp/client/router.py`,
`_resolve_handler` в `acp/router.py:31-46`) работает через `getattr(obj, attr_name)`,
чистый duck-typing. Если метода нет на объекте — для request-методов с `optional=True`
роутер вернёт `default_result` вместо ошибки (см. список ниже), иначе клиенту прилетит
JSON-RPC `-32601 Method not found`.

Полный протокол (`acp/interfaces.py:83-158`), с точными сигнатурами (все параметры —
keyword, плюс `**kwargs` для полей `_meta` / будущих полей):

```python
class Client(Protocol):
    async def request_permission(
        self, session_id: str, tool_call: ToolCallUpdate, options: list[PermissionOption], **kwargs: Any
    ) -> RequestPermissionResponse: ...

    async def session_update(
        self, session_id: str,
        update: UserMessageChunk | AgentMessageChunk | AgentThoughtChunk | ToolCallStart | ToolCallProgress
              | AgentPlanUpdate | AgentPlanContentUpdate | AgentPlanRemovedUpdate | AvailableCommandsUpdate
              | CurrentModeUpdate | ConfigOptionUpdate | SessionInfoUpdate | UsageUpdate,
        **kwargs: Any,
    ) -> None: ...

    async def write_text_file(self, session_id: str, path: str, content: str, **kwargs: Any) -> WriteTextFileResponse | None: ...
    async def read_text_file(self, session_id: str, path: str, line: int | None = None, limit: int | None = None, **kwargs: Any) -> ReadTextFileResponse: ...

    async def create_terminal(self, session_id: str, command: str, args: list[str] | None = None,
                               env: list[EnvVariable] | None = None, cwd: str | None = None,
                               output_byte_limit: int | None = None, **kwargs: Any) -> CreateTerminalResponse: ...
    async def terminal_output(self, session_id: str, terminal_id: str, **kwargs: Any) -> TerminalOutputResponse: ...
    async def release_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> ReleaseTerminalResponse | None: ...
    async def wait_for_terminal_exit(self, session_id: str, terminal_id: str, **kwargs: Any) -> WaitForTerminalExitResponse: ...
    async def kill_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> KillTerminalResponse | None: ...

    async def create_elicitation(self, message: str, mode: ElicitationMode, **kwargs: Any) -> CreateElicitationResponse: ...
    async def complete_elicitation(self, elicitation_id: str, **kwargs: Any) -> None: ...

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...
    async def ext_notification(self, method: str, params: dict[str, Any]) -> None: ...
    def on_connect(self, conn: Agent) -> None: ...
```

Методы для панели houdini-agent-panel: реально обязательные —
`session_update` (входящий поток обновлений — это то, что рисует чат) и
`request_permission` (диалог разрешений). Остальные — по потребности агента;
если панель их не реализует, роутер сам вернёт разумный дефолт для терминалов
(`optional=True, default_result={} | None`, см. `acp/client/router.py:103-144`), но
`write_text_file`/`read_text_file`/`request_permission`/`session_update` — НЕ опциональны
в роутере (`route_request(... )` без `optional=True`, `acp/client/router.py:95-102`,
`163`) — если агент их вызовет, а панель не предоставит метод, будет JSON-RPC ошибка
`method not found`. На практике: агент вызывает `fs/read_text_file` /
`fs/write_text_file` только если панель заявила поддержку в
`ClientCapabilities.fs` (см. §4) — так что если not implementing, просто не
объявляй capability.

Метод **не async, обычный**: `on_connect(self, conn: Agent) -> None` — вызывается синхронно
сразу в конструкторе `ClientSideConnection.__init__` (`acp/client/connection.py:83-84`),
если у клиента есть атрибут `on_connect`. Удобное место, чтобы сохранить `conn` (агент
как `Agent`-объект) на сессию клиента, если понадобится, например, звать `conn.cancel(...)`
изнутри самого объекта Client.

Соответствие JSON-RPC method name → Python-метод (`CLIENT_METHODS`, `acp/meta.py:33-48`):
```
session/request_permission -> request_permission
session/update             -> session_update
fs/write_text_file         -> write_text_file
fs/read_text_file          -> read_text_file
terminal/create            -> create_terminal
terminal/output            -> terminal_output
terminal/release           -> release_terminal
terminal/wait_for_exit     -> wait_for_terminal_exit
terminal/kill               -> kill_terminal
mcp/connect                -> (не в Client Protocol — внутренний для elicitation/mcp транспортов)
mcp/message                -> (см. выше)
mcp/disconnect              -> (см. выше)
elicitation/create          -> create_elicitation
elicitation/complete        -> complete_elicitation
```

## 3. Исходящие вызовы клиента — `ClientSideConnection`

`acp/client/connection.py` — класс `ClientSideConnection` (то, что возвращает
`spawn_agent_process`/`connect_to_agent`). Все методы — `async`, все параметры keyword,
плюс `**kwargs` мержится в `_meta` (`field_meta`) запроса.

```python
class ClientSideConnection:
    def __init__(self, to_client: Callable[[Agent], Client] | Client, input_stream: Any,
                 output_stream: Any = None, *, use_unstable_protocol: bool = False,
                 **connection_kwargs: Any) -> None: ...

    async def initialize(self, protocol_version: int,
                          client_capabilities: ClientCapabilities | None = None,
                          client_info: Implementation | None = None, **kwargs) -> InitializeResponse: ...

    async def new_session(self, cwd: str, additional_directories: list[str] | None = None,
                           mcp_servers: list[HttpMcpServer|SseMcpServer|AcpMcpServer|McpServerStdio] | None = None,
                           **kwargs) -> NewSessionResponse: ...

    async def load_session(self, cwd: str, session_id: str,
                            mcp_servers: ... | None = None,
                            additional_directories: list[str] | None = None, **kwargs) -> LoadSessionResponse: ...

    async def list_sessions(self, cwd: str | None = None, cursor: str | None = None, **kwargs) -> ListSessionsResponse: ...

    async def set_session_mode(self, session_id: str, mode_id: str, **kwargs) -> SetSessionModeResponse: ...

    async def set_config_option(self, config_id: str, session_id: str, value: str | bool, **kwargs) -> SetSessionConfigOptionResponse: ...

    async def authenticate(self, method_id: str, **kwargs) -> AuthenticateResponse: ...

    async def prompt(self, session_id: str,
                      prompt: list[TextContentBlock|ImageContentBlock|AudioContentBlock|ResourceContentBlock|EmbeddedResourceContentBlock],
                      **kwargs) -> PromptResponse: ...

    async def fork_session(self, session_id: str, cwd: str, additional_directories=None, mcp_servers=None, **kwargs) -> ForkSessionResponse: ...
    async def resume_session(self, session_id: str, cwd: str, additional_directories=None, mcp_servers=None, **kwargs) -> ResumeSessionResponse: ...
    async def close_session(self, session_id: str, **kwargs) -> CloseSessionResponse | None: ...

    async def cancel(self, session_id: str, **kwargs) -> None:  # это НОТИФИКАЦИЯ, не request — нет ответа
        ...

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...
    async def ext_notification(self, method: str, params: dict[str, Any]) -> None: ...

    async def close(self) -> None: ...
    async def __aenter__(self) -> "ClientSideConnection": ...
    async def __aexit__(self, exc_type, exc, tb) -> None: ...  # закрывает соединение
```

Важно про `cancel`: это JSON-RPC *notification* (`session/cancel`), а не request —
метод не блокируется в ожидании ответа и всегда возвращает `None` немедленно после отправки
(`await notify_model(...)`, `acp/client/connection.py:269-275`). Отменённый промпт агент
завершит через обычный `session/update` поток + `PromptResponse.stop_reason == "cancelled"`
на текущем `await conn.prompt(...)`.

`session/list`, `session/fork`, `session/resume`, `session/close` — это методы схемы
v1.19.0, они присутствуют в SDK, но по факту не все агенты их поддерживают
(зависит от `AgentCapabilities.session_capabilities`, см. §4). Для минимального ACP-клиента
(Claude Code/Codex/Gemini CLI как агент) реально нужны только: `initialize`, `new_session`,
`prompt`, `cancel`, `set_session_mode`, опционально `authenticate`.

JSON-RPC имена методов агента (`AGENT_METHODS`, `acp/meta.py:3-32`):
```
initialize            -> initialize
authenticate          -> authenticate
session/new           -> new_session
session/load          -> load_session
session/set_mode      -> set_session_mode
session/set_config_option -> set_config_option
session/prompt        -> prompt
session/cancel        -> cancel (notification)
session/list          -> list_sessions
session/delete        -> (нет в Agent Protocol / ClientSideConnection — есть DeleteSessionRequest в schema, но не обёрнуто методом)
session/fork          -> fork_session
session/resume        -> resume_session
session/close         -> close_session
```

## 4. Модели

Все модели — pydantic v2 `BaseModel` с алиасами camelCase (`Field(alias="...")`),
Python-имена полей — snake_case. У всех есть `field_meta: dict|None` с алиасом `_meta`
(универсальное расширение протокола) — не путать с kwargs-параметром `**kwargs` у методов
(тот как раз мапится именно в это поле).

### InitializeRequest / InitializeResponse (`acp/schema.py:6307-6382`, `6647-6724`)

```python
class InitializeRequest(BaseModel):
    protocol_version: int              # alias "protocolVersion", 0..65535
    client_capabilities: ClientCapabilities | None = ClientCapabilities()  # alias "clientCapabilities"
    client_info: Implementation | None = None                              # alias "clientInfo"

class InitializeResponse(BaseModel):
    protocol_version: int                                    # alias "protocolVersion"
    agent_capabilities: AgentCapabilities | None = AgentCapabilities()     # alias "agentCapabilities"
    auth_methods: list[EnvVarAuthMethod|TerminalAuthMethod|AuthMethodAgent] | None = []  # alias "authMethods"
    agent_info: Implementation | None = None                              # alias "agentInfo"
```
`InitializeRequest` терпимо относится к нечисловому `protocol_version` (например строка
даты от старых клиентов Zed) — конвертирует в `1` при ошибке парсинга
(`_coerce_protocol_version`, `acp/schema.py:6349-6361`) — это защита на СТОРОНЕ АГЕНТА,
панели как клиенту это не важно, просто отправляй `acp.PROTOCOL_VERSION` (int `1`).

`Implementation` (`acp/schema.py:1250` область) — `{name: str, version: str, title: str|None}`
(не выписываю по полям — это тривиальная модель типа npm-package info).

### ClientCapabilities (`acp/schema.py:5818-5947`)
```python
class ClientCapabilities(BaseModel):
    fs: FileSystemCapabilities | None = FileSystemCapabilities()   # readTextFile/writeTextFile — оба default False
    terminal: bool | None = False
    session: ClientSessionCapabilities | None = None
    plan: PlanCapabilities | None = None            # UNSTABLE: plan_update/plan_removed апдейты
    auth: AuthCapabilities | None = {"terminal": False}
    elicitation: ElicitationCapabilities | None = None
    nes: ClientNesCapabilities | None = None        # Next Edit Suggestions, UNSTABLE
    position_encodings: list[str] | None = None     # alias "positionEncodings", UNSTABLE
```
`FileSystemCapabilities`:
```python
class FileSystemCapabilities(BaseModel):
    read_text_file: bool | None = False   # alias "readTextFile"
    write_text_file: bool | None = False  # alias "writeTextFile"
```
Если панель хочет, чтобы агент читал/писал файлы через клиента (а не напрямую через ФС) —
надо выставить `fs=FileSystemCapabilities(read_text_file=True, write_text_file=True)`
и реализовать `Client.read_text_file`/`write_text_file`. Иначе агент читает/пишет файлы
сам (у него есть `cwd`).

### AgentCapabilities (`acp/schema.py:6412-...`, начало)
```python
class AgentCapabilities(BaseModel):
    load_session: bool | None = False                          # alias "loadSession" — поддержка session/load
    prompt_capabilities: PromptCapabilities | None = PromptCapabilities()   # alias "promptCapabilities": image/audio/embeddedContext bool-флаги
    mcp_capabilities: McpCapabilities | None = McpCapabilities()            # alias "mcpCapabilities": http/sse/acp bool-флаги
    session_capabilities: SessionCapabilities | None = SessionCapabilities()  # alias "sessionCapabilities" — fork/resume/close/list/delete
    auth: AgentAuthCapabilities | None = ...
```
Проверяй `agent_capabilities.prompt_capabilities.image` перед отправкой `ImageContentBlock`
в промпте — если `False`, агент может это отвергнуть.

### NewSessionRequest / NewSessionResponse (`acp/schema.py:5172-5219`, `6216-6265`)
```python
class NewSessionRequest(BaseModel):
    cwd: str                                    # обязателен, абсолютный путь
    additional_directories: list[str] | None = None   # alias "additionalDirectories"
    mcp_servers: list[HttpMcpServer|SseMcpServer|AcpMcpServer|McpServerStdio]   # alias "mcpServers" — список, не Optional (но ClientSideConnection.new_session сам подставляет [] если None, см. §3)

class NewSessionResponse(BaseModel):
    session_id: str                             # alias "sessionId"
    modes: SessionModeState | None = None        # начальный набор режимов агента, если поддерживает
    config_options: list[SessionConfigOptionSelect|SessionConfigOptionBoolean] | None = None  # alias "configOptions"
```

### MCP-серверы — точная форма записи (`acp/schema.py:2270-2372`)
Дискриминации по форме класса нет единого `type`-литерала в базовых классах (это Union без
discriminator в `NewSessionRequest.mcp_servers`, значит pydantic будет пробовать по порядку/по
полям) — 4 варианта:

```python
class McpServerStdio(BaseModel):
    name: str
    command: str             # абсолютный путь к исполняемому файлу MCP-сервера
    args: list[str]           # НЕ Optional — список аргументов (может быть [])
    env: list[EnvVariable]     # НЕ Optional — список {name: str, value: str} (см. EnvVariable)

class McpServerHttp(BaseModel):
    name: str
    url: str
    headers: list[HttpHeader]   # [{name: str, value: str}]

class McpServerSse(BaseModel):
    name: str
    url: str
    headers: list[HttpHeader]

class McpServerAcp(BaseModel):
    name: str
    server_id: str    # alias "serverId" — уникальный id ACP-транспортного MCP-сервера
```
Т.е. да — есть варианты http и sse, помимо stdio, плюс собственный "acp"-транспорт
(MCP-сервер, доступный через тот же ACP-соединение). В `interfaces.py`/`connection.py` они
экспортируются как `HttpMcpServer`/`SseMcpServer`/`AcpMcpServer` (тонкие сабклассы
`McpServerHttp`/`McpServerSse`/`McpServerAcp` без доп. полей, `acp/schema.py:4387-4398`) —
именно эти "публичные" имена и нужно использовать в списке `mcp_servers`.
`EnvVariable`: `{name: str, value: str}` (`acp/schema.py:210-228`, тривиальна).

### PromptRequest — content-блоки (`acp/schema.py:5222-5269`, блоки — `4819-4838`)
```python
prompt: list[TextContentBlock | ImageContentBlock | AudioContentBlock | ResourceContentBlock | EmbeddedResourceContentBlock]
```
Дискриминатор — поле `type` (Literal) на каждом варианте:
```python
class TextContentBlock(TextContent):        type: Literal["text"]
class ImageContentBlock(ImageContent):        type: Literal["image"]
class AudioContentBlock(AudioContent):        type: Literal["audio"]
class ResourceContentBlock(ResourceLink):     type: Literal["resource_link"]
class EmbeddedResourceContentBlock(EmbeddedResource):  type: Literal["resource"]
```
Базовые поля (без discriminator, эти — родительские классы):
```python
class TextContent(BaseModel):
    annotations: Annotations | None = None
    text: str

class ImageContent(BaseModel):
    annotations: Annotations | None = None
    data: str          # base64
    mime_type: str      # alias "mimeType"
    uri: str | None = None

class AudioContent(BaseModel):
    annotations: Annotations | None = None
    data: str          # base64
    mime_type: str      # alias "mimeType"

class ResourceLink(BaseModel):   # ResourceContentBlock = ResourceLink + type
    annotations: Annotations | None = None
    description: str | None = None
    mime_type: str | None = None    # alias "mimeType"
    name: str
    size: int | None = None
    title: str | None = None
    uri: str

class EmbeddedResource(BaseModel):  # EmbeddedResourceContentBlock = EmbeddedResource + type
    annotations: Annotations | None = None
    resource: TextResourceContents | BlobResourceContents
```
Хелперы-конструкторы (`acp/helpers.py`, готовы к использованию без прямого импорта схемы):
```python
text_block(text: str) -> TextContentBlock
image_block(data: str, mime_type: str, *, uri: str|None=None) -> ImageContentBlock
audio_block(data: str, mime_type: str) -> AudioContentBlock
resource_link_block(name, uri, *, mime_type=None, size=None, description=None, title=None) -> ResourceContentBlock
embedded_text_resource(uri, text, *, mime_type=None) -> TextResourceContents
embedded_blob_resource(uri, blob, *, mime_type=None) -> BlobResourceContents
resource_block(resource: TextResourceContents|BlobResourceContents) -> EmbeddedResourceContentBlock
```

### SessionNotification / `session/update` (`acp/schema.py:6529-6568`)
```python
class SessionNotification(BaseModel):
    session_id: str    # alias "sessionId"
    update: Union[UserMessageChunk, AgentMessageChunk, AgentThoughtChunk, ToolCallStart, ToolCallProgress,
                  AgentPlanUpdate, AgentPlanContentUpdate, AgentPlanRemovedUpdate, AvailableCommandsUpdate,
                  CurrentModeUpdate, ConfigOptionUpdate, SessionInfoUpdate, UsageUpdate]
    # Field(discriminator="session_update")   <-- дискриминатор ИМЕННО это поле (alias "sessionUpdate"), НЕ "type"
```
Каждый вариант — literal на поле `session_update` (alias `sessionUpdate`):
```python
UserMessageChunk(ContentChunk):        session_update: Literal["user_message_chunk"]
AgentMessageChunk(ContentChunk):        session_update: Literal["agent_message_chunk"]
AgentThoughtChunk(ContentChunk):        session_update: Literal["agent_thought_chunk"]
ToolCallStart(ToolCall):                session_update: Literal["tool_call"]
ToolCallProgress(ToolCallUpdate):       session_update: Literal["tool_call_update"]
AgentPlanUpdate(Plan):                  session_update: Literal["plan"]
AgentPlanContentUpdate(PlanUpdate):     session_update: Literal["plan_update"]        # UNSTABLE
AgentPlanRemovedUpdate(PlanRemoved):    session_update: Literal["plan_removed"]        # UNSTABLE
AvailableCommandsUpdate(_AvailableCommandsUpdate): session_update: Literal["available_commands_update"]
CurrentModeUpdate(_CurrentModeUpdate):  session_update: Literal["current_mode_update"]
ConfigOptionUpdate(_ConfigOptionUpdate): session_update: Literal["config_option_update"]
SessionInfoUpdate(_SessionInfoUpdate):  session_update: Literal["session_info_update"]   # предположительно; не выписан подробно
UsageUpdate(_UsageUpdate):              session_update: Literal["usage_update"]
```
`ContentChunk` (база для user/agent/thought-чанков):
```python
class ContentChunk(BaseModel):
    content: TextContentBlock | ImageContentBlock | AudioContentBlock | ResourceContentBlock | EmbeddedResourceContentBlock  # discriminator="type"
    message_id: str | None = None   # alias "messageId" — общий для всех чанков одного сообщения
```
Практический разбор входящего `session/update` в Python:
```python
async def session_update(self, session_id, update, **kwargs):
    match update.session_update:  # это Python-имя поля (alias "sessionUpdate" только на проводе)
        case "agent_message_chunk":
            text = update.content.text if update.content.type == "text" else None
        case "tool_call":
            ...  # update — ToolCallStart, поля как в ToolCall (см. ниже)
        case "tool_call_update":
            ...  # update — ToolCallProgress (все поля Optional — частичное обновление)
        case "plan":
            for entry in update.entries: ...
```

### ToolCall / ToolCallStart / ToolCallUpdate / ToolCallProgress (`acp/schema.py:5716-5789`, `6046-6099`, `6295-6300`)
```python
ToolKind = Literal["read","edit","delete","move","search","execute","think","fetch","switch_mode","other"]
ToolCallStatus = Literal["pending","in_progress","completed","failed"]

class ToolCall(BaseModel):    # ToolCallStart = ToolCall + session_update literal
    tool_call_id: str          # alias "toolCallId"
    title: str
    kind: ToolKind | None = None
    status: ToolCallStatus | None = None
    content: list[ContentToolCallContent|FileEditToolCallContent|TerminalToolCallContent] | None = None
    locations: list[ToolCallLocation] | None = None   # для "follow-along" в UI — подсветить файл
    raw_input: Any | None = None    # alias "rawInput"
    raw_output: Any | None = None   # alias "rawOutput"

class ToolCallUpdate(BaseModel):   # ToolCallProgress = ToolCallUpdate + session_update literal
    tool_call_id: str        # обязателен — id обновляемого вызова
    kind: ToolKind | None = None
    status: ToolCallStatus | None = None
    title: str | None = None
    content: list[...] | None = None       # ЗАМЕНЯЕТ весь список при обновлении, не патчит по элементам
    locations: list[ToolCallLocation] | None = None
    raw_input: Any | None = None
    raw_output: Any | None = None
```
`ToolCallLocation` — `{path: str, line: int|None}` (примерно, `acp/schema.py:186-209`).
Три варианта содержимого tool call, дискриминатор поля `type`:
```python
class ContentToolCallContent(Content):   type наследуется из Content — там нет literal сверху,
    # фактически: {"type": "content", "content": <ContentBlock>}
class FileEditToolCallContent(Diff):      # {"type": "diff", "path": str, "new_text": str, "old_text": str|None}
class TerminalToolCallContent(Terminal):  # {"type": "terminal", "terminal_id": str}
```
(смотри хелперы `tool_content()/tool_diff_content()/tool_terminal_ref()` в `acp/helpers.py:129-138`
— они сами простявляют правильный `type`.)

Хелперы для быстрой сборки апдейтов (`acp/helpers.py`):
```python
start_tool_call(tool_call_id, title, *, kind=None, status=None, content=None, locations=None, raw_input=None, raw_output=None) -> ToolCallStart
start_read_tool_call(tool_call_id, title, path, *, extra_options=None) -> ToolCallStart   # kind="read", проставляет locations/raw_input сам
start_edit_tool_call(tool_call_id, title, path, content, *, extra_options=None) -> ToolCallStart  # kind="edit"
update_tool_call(tool_call_id, *, title=None, kind=None, status=None, content=None, locations=None, raw_input=None, raw_output=None) -> ToolCallProgress
```

### Plan / PlanEntry (`acp/schema.py:4169-4227`)
```python
PlanEntryPriority = Literal["high","medium","low"]
PlanEntryStatus = Literal["pending","in_progress","completed"]

class PlanEntry(BaseModel):
    content: str
    priority: PlanEntryPriority
    status: PlanEntryStatus

class Plan(BaseModel):   # AgentPlanUpdate = Plan + session_update: Literal["plan"]
    entries: list[PlanEntry]   # КАЖДЫЙ раз полный список — клиент заменяет план целиком, не патчит
```
Хелперы: `plan_entry(content, *, priority="medium", status="pending") -> PlanEntry`,
`update_plan(entries: Iterable[PlanEntry]) -> AgentPlanUpdate`.

### RequestPermissionRequest / RequestPermissionResponse / PermissionOption (`acp/schema.py:6382-6412`, `5320-5341`, `3382-3406`)
```python
PermissionOptionKind = Literal["allow_once","allow_always","reject_once","reject_always"]

class PermissionOption(BaseModel):
    option_id: str      # alias "optionId"
    name: str
    kind: PermissionOptionKind

class RequestPermissionRequest(BaseModel):
    session_id: str            # alias "sessionId"
    tool_call: ToolCallUpdate   # alias "toolCall" — детали вызова, требующего разрешения
    options: list[PermissionOption]

class RequestPermissionResponse(BaseModel):
    outcome: DeniedOutcome | AllowedOutcome   # discriminator="outcome" (само поле "outcome" внутри варианта — литерал)

class DeniedOutcome(BaseModel):
    outcome: Literal["cancelled"]

class SelectedPermissionOutcome(BaseModel):    # AllowedOutcome = SelectedPermissionOutcome + outcome: Literal["selected"]
    option_id: str    # alias "optionId" — какой из PermissionOption.option_id выбран

class AllowedOutcome(SelectedPermissionOutcome):
    outcome: Literal["selected"]
```
Т.е. форма ответа: либо `RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))`
(пользователь отменил/закрыл диалог), либо
`RequestPermissionResponse(outcome=AllowedOutcome(outcome="selected", option_id="allow_once"))`
(пользователь выбрал один из присланных вариантов). Обрати внимание: дискриминирующее поле
называется `outcome` И на уровне обёртки (`RequestPermissionResponse.outcome`), И как литерал
внутри самого варианта (`DeniedOutcome.outcome`/`AllowedOutcome.outcome`) — не путать эти два
разных `outcome`.

### AvailableCommand / AvailableCommandsUpdate (`acp/schema.py:5084-...`, `5116-...`, `5712-5714`)
```python
class AvailableCommand(BaseModel):
    name: str              # e.g. "create_plan"
    description: str
    input: AvailableCommandInput | None = None
    # AvailableCommandInput = RootModel[UnstructuredCommandInput]
    # UnstructuredCommandInput (acp/schema.py:1795) = {"hint": str} — единственное поле,
    # человекочитаемая подсказка о том, что ожидается после имени команды (напр. "/model <name>").

class _AvailableCommandsUpdate(BaseModel):
    available_commands: list[AvailableCommand]   # alias "availableCommands"

class AvailableCommandsUpdate(_AvailableCommandsUpdate):
    session_update: Literal["available_commands_update"]
```
Хелпер: `update_available_commands(commands: Iterable[AvailableCommand]) -> AvailableCommandsUpdate`.

### SessionModeState / SessionMode / CurrentModeUpdate (`acp/schema.py:3918-...`, `1381-...`, `1815`/`4157`)
```python
class SessionMode(BaseModel):
    id: str
    name: str
    description: str | None = None

class SessionModeState(BaseModel):
    current_mode_id: str          # alias "currentModeId"
    available_modes: list[SessionMode]   # alias "availableModes"

class CurrentModeUpdate(_CurrentModeUpdate):     # _CurrentModeUpdate, скорее всего {current_mode_id: str}
    session_update: Literal["current_mode_update"]
```
Хелпер: `update_current_mode(current_mode_id: str) -> CurrentModeUpdate`.
`SetSessionModeRequest`/`Response` — исходящий вызов `set_session_mode(session_id, mode_id)`
(см. §3) переключает текущий режим (например "ask"/"code"/"architect" — конкретные id
зависят от агента, приходят через `SessionModeState.available_modes`).

### PromptResponse / StopReason (`acp/schema.py:4051-4087`, `15`)
```python
StopReason = Literal["end_turn","max_tokens","max_turn_requests","refusal","cancelled"]

class PromptResponse(BaseModel):
    stop_reason: StopReason    # alias "stopReason"
    usage: Usage | None = None   # UNSTABLE — токены
```

## 5. Ошибка `auth_required`

Нет отдельного класса исключения — единый `acp.RequestError(code, message, data=None)`
(`acp/exceptions.py`), обычный `Exception` с полями `.code`/`.data`:
```python
class RequestError(Exception):
    def __init__(self, code: int, message: str, data: Any | None = None) -> None: ...
    @classmethod
    def auth_required(cls, data=None) -> "RequestError":
        return cls(-32000, "Authentication required", data)
    # + parse_error(-32700), invalid_request(-32600), method_not_found(-32601),
    #   invalid_params(-32602), internal_error(-32603), resource_not_found(-32002)
```
`auth_required` — это конструктор для АГЕНТСКОЙ стороны (чтобы агент кинул эту ошибку
из своего `initialize`/`new_session`/`prompt`, если требуется логин). На КЛИЕНТСКОЙ
стороне (`ClientSideConnection`) она приходит просто как JSON-RPC error-объект и
транслируется в `RequestError` с тем же кодом (`acp/connection.py:279-289`,
`_handle_response`):
```python
if "error" in message:
    error_obj = message.get("error") or {}
    self._state.reject_outgoing(request_id, RequestError(
        error_obj.get("code", -32603), error_obj.get("message", "Error"), error_obj.get("data"),
    ))
```
Практика для клиента:
```python
try:
    resp = await conn.new_session(cwd=cwd, mcp_servers=[])
except acp.RequestError as exc:
    if exc.code == -32000:
        # нужен логин — вызвать conn.authenticate(method_id=...) с одним из
        # InitializeResponse.auth_methods, затем повторить new_session
        ...
    else:
        raise
```
Код `-32000` — соглашение самого SDK/протокола (не стандартный JSON-RPC 2.0 код, это
application-specific диапазон -32000..-32099), но нет отдельного `AuthRequiredError`
подкласса — проверяй именно `exc.code == -32000`.

## 6. Примеры/тесты клиента в самом пакете

В установленном дистрибутиве (`site-packages/acp/`) НЕТ каталога `examples/` или `tests/` —
это только сама библиотека, без исходного репозитория/README с примерами. Полноценного
"дословного примера клиента" в пакете нет. Но есть модуль `acp.contrib` с готовыми
утилитами более высокого уровня — публичный API (не приватный, не `_`-префикс на уровне
модулей) каждого файла подтверждён чтением исходников:

**`acp/contrib/session_state.py`** — накопление состояния сессии из потока `session_update`
(именно то, что нужно панели, чтобы держать модель чата в актуальном виде):
```python
class ToolCallView(BaseModel): ...      # снапшот одного tool call (публичное поле-представление)
class SessionSnapshot(BaseModel): ...    # снапшот всей сессии (сообщения, tool calls, план, режим...)

class SessionAccumulator:
    def __init__(self, *, auto_reset_on_session_change: bool = True) -> None: ...
    def reset(self) -> None: ...
    def subscribe(self, callback: Callable[[SessionSnapshot, SessionNotification], None]) -> Callable[[], None]: ...
    def apply(self, notification: SessionNotification) -> SessionSnapshot: ...   # скорми сюда каждый session_update
    def snapshot(self) -> SessionSnapshot: ...
```
Практически: в `Client.session_update(self, session_id, update, **kwargs)` достаточно
собрать `SessionNotification(session_id=session_id, update=update)` (или взять готовый —
если правишь роутер, там модель уже есть) и вызвать `accumulator.apply(notification)`,
подписавшись на `subscribe(...)` для обновления UI.

**`acp/contrib/tool_calls.py`** — construction/tracking ToolCall-объектов на СТОРОНЕ АГЕНТА
(не клиента) — полезно, только если панель когда-нибудь сама будет писать агента, для клиента
неприменимо напрямую:
```python
class TrackedToolCallView(BaseModel): ...
class ToolCallTracker:
    def __init__(self, *, id_factory: Callable[[], str] | None = None) -> None: ...
    def start(self, ...) -> ToolCallStart: ...
    def progress(self, ...) -> ToolCallProgress: ...
    def append_stream_text(self, ...) -> ...: ...
    def forget(self, external_id: str) -> None: ...
    def view(self, external_id: str) -> TrackedToolCallView: ...
    def tool_call_model(self, external_id: str) -> ToolCallUpdate: ...
```

**`acp/contrib/permissions.py`** — брокер разрешений, тоже АГЕНТСКАЯ утилита (готовит
`RequestPermissionRequest`/дефолтные опции для отправки клиенту), не нужна на стороне
клиента-панели:
```python
def default_permission_options() -> tuple[PermissionOption, PermissionOption, PermissionOption]: ...
class PermissionBroker:
    def __init__(self, ...) -> None: ...
    async def request_for(self, ...) -> RequestPermissionResponse: ...
```
Вывод: для клиента панели полезен только `session_state.SessionAccumulator` — он реально
экономит написание своего редьюсера над потоком `session_update`. `tool_calls`/`permissions`
из `contrib` — вспомогательные классы для СТОРОНЫ АГЕНТА, панели не пригодятся напрямую.

Минимальный полный skeleton клиента, собранный из проверенных фрагментов выше (не дословная
цитата, а компоновка реальных сигнатур — использовать как стартовую точку):
```python
import acp

class HoudiniPanelClient:
    def __init__(self):
        self.conn: acp.Client | None = None

    def on_connect(self, conn):        # sync, вызывается ClientSideConnection.__init__
        self.agent = conn

    async def session_update(self, session_id, update, **kwargs):
        ...  # см. §4 SessionNotification — рисуем чат по update.session_update

    async def request_permission(self, session_id, tool_call, options, **kwargs):
        ...  # показать диалог, вернуть acp.RequestPermissionResponse(outcome=...)

    # write_text_file/read_text_file/create_terminal/... — только если объявили capability

async def main():
    client = HoudiniPanelClient()
    async with acp.spawn_agent_process(client, "claude-code-acp") as (conn, process):
        init = await conn.initialize(
            protocol_version=acp.PROTOCOL_VERSION,
            client_capabilities=acp.schema.ClientCapabilities(),  # или дефолт
        )
        session = await conn.new_session(cwd="/path/to/hip/project", mcp_servers=[])
        resp = await conn.prompt(
            session_id=session.session_id,
            prompt=[acp.text_block("привет")],
        )
        print(resp.stop_reason)
```

## 7. Прочее, что пригодится при реализации

- `RequestError` на КЛИЕНТСКОМ вызове (`await conn.prompt(...)` и т.п.) прилетает как
  обычное исключение из `await request_model(...)` → `Connection.send_request` → `future`
  (`acp/connection.py:144-159`, `273-289`) — оборачивать в `try/except acp.RequestError`.
- Все Python-имена — snake_case, все имена на проводе (JSON) — camelCase через `Field(alias=...)`;
  pydantic-модели строятся из snake_case kwargs (`InitializeRequest(protocol_version=..., ...)`),
  сериализация в camelCase происходит автоматически при `model_dump(by_alias=True)` внутри SDK
  (клиенту не нужно самому заботиться про алиасы — только использовать python-имена полей).
  Единственное практическое исключение — метод `Client.session_update`, где discriminator
  на проводе `sessionUpdate`, а Python-атрибут — `session_update` (тот же паттерн alias).
- `**kwargs` в сигнатурах методов и конструкторах моделей — это НЕ произвольные доп.
  параметры, они целиком уходят в `field_meta` (`_meta`) конкретного request/response —
  расширение протокола, а не способ передать undocumented-параметры мимо модели.
- Cancel — notification, не request: `await conn.cancel(session_id=...)` не ждёт ответа;
  результат отмены увидишь как `PromptResponse.stop_reason == "cancelled"` у того промпта,
  что отменяли, и/или как поток `session/update`, если агент успел что-то досказать до отмены.

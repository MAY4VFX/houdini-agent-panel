# `agent-client-protocol` (PyPI) 0.12.0 — an exact reference for the client side

Package: `agent_client_protocol-0.12.0` (`import acp`).
Verified by reading the source in
`.../scratchpad/venv/lib/python3.14/site-packages/acp/` and a live `python -c "import acp; ..."`
on the `.../scratchpad/venv/bin/python` interpreter. Line references are from the package's files.

The schema was generated from `schema/meta.json`, ref `refs/tags/schema-v1.19.0`
(see `acp/meta.py:1-2`). `PROTOCOL_VERSION = 1` (`acp/meta.py:49`).

## 0. Compatibility with Python 3.11 (Houdini 20.5)

`agent_client_protocol-0.12.0.dist-info/METADATA`:
```
Requires-Python: <3.15,>=3.10
Classifier: Programming Language :: Python :: 3.10
Classifier: Programming Language :: Python :: 3.11
Classifier: Programming Language :: Python :: 3.12
Classifier: Programming Language :: Python :: 3.13
Classifier: Programming Language :: Python :: 3.14
```
Nothing requires >=3.12. The package officially supports 3.10-3.14, so the
Python 3.11 built into Houdini 20.5 works without reservation. Modules use
`from __future__ import annotations` and `X | Y` syntax in annotations (not at
runtime) — that's also safe on 3.11 thanks to deferred annotations.

## 1. How the client spawns the agent as a subprocess

There's a ready-made async context manager, built specifically for the "we're the
client, the agent is an external CLI process" scenario — exactly what the panel
needs:

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

Usage:
```python
async with acp.spawn_agent_process(my_client_impl, "claude-code-acp") as (conn, process):
    resp = await conn.initialize(protocol_version=acp.PROTOCOL_VERSION)
    ...
```
`to_client` is either a ready-made object implementing the `Client` protocol (see §2), or a
`Callable[[Agent], Client]` factory that receives `conn` itself (as an `Agent`) — handy when the
client needs to call the agent back the other way (e.g. cancel a session from the UI).
`ClientSideConnection` calls `to_client(self)` synchronously in `__init__`
(`acp/client/connection.py:71`).

Under the hood, it's `acp/transports.py:47-119`, `spawn_stdio_transport`:
- assembles the environment via `default_environment()` (inherits only
  `HOME, LOGNAME, PATH, SHELL, TERM, USER` on POSIX — an almost-empty env, not the full `os.environ`!
  you must explicitly pass `env=` with any variable the agent needs beyond that,
  e.g. `ANTHROPIC_API_KEY` or the project's node/python PATH) and merges it with the given `env`.
- `asyncio.create_subprocess_exec(command, *args, stdin=PIPE, stdout=PIPE, stderr=PIPE, env=merged_env, cwd=...)`.
- on exiting `async with`: a graceful shutdown — `stdin.write_eof()` → `drain()` → `close()`,
  then `wait_for(process.wait(), timeout=2.0)`, on timeout `terminate()`, another timeout,
  then `kill()`. The `shutdown_timeout` parameter is configurable via `transport_kwargs`.
- the subprocess's `stderr` is by default a separate `PIPE` (`aio_subprocess.PIPE`), not merged
  into stdout; it has to be read separately, or the buffer can fill up and hang the process.

`spawn_agent_process` is a high-level helper (it builds the `ClientSideConnection` itself).
If you only need a low-level `Connection` without a ready-made bundle of methods, there's
`spawn_stdio_connection(handler, command, *args, ...)` (`acp/stdio.py:143-158`), but for
the panel's client this isn't needed: `spawn_agent_process` fits directly.

The reverse helper `spawn_client_process` (the agent spawns the client) isn't needed for this
project, the panel is specifically the client.

The default stdio buffer for the low-level `stdio_streams()` is 64KB (`asyncio.StreamReader`'s
default), but `run_agent()` (the agent side) raises the limit to 50MB
(`DEFAULT_STDIO_BUFFER_LIMIT_BYTES = 50 * 1024 * 1024`, `acp/core.py:36`) — that's a constant
for the agent side via `run_agent`; `spawn_agent_process`/`spawn_stdio_transport`
accept their own `limit` via `transport_kwargs={"limit": ...}`, but by default they do NOT raise
it to 50MB (default `limit=None` → `asyncio.create_subprocess_exec` with no `limit=` → the standard
64KB reader limit). **For large base64 images/audio from the agent, it may be necessary to
explicitly pass `transport_kwargs={"limit": 50*1024*1024}`.**

## 2. The class that implements the CLIENT (callbacks the agent calls on us)

`acp.interfaces.Client` is a `typing.Protocol` (structural typing), **not an ABC**:
```python
>>> acp.Client.__mro__
(<class 'acp.interfaces.Client'>, <class 'typing.Protocol'>, <class 'typing.Generic'>, <class 'object'>)
```
Subclassing it isn't required — routing (`acp/client/router.py`,
`_resolve_handler` in `acp/router.py:31-46`) works via `getattr(obj, attr_name)`,
pure duck typing. If an object has no such method — for request methods with `optional=True`
the router returns `default_result` instead of an error (see the list below), otherwise the
client gets a JSON-RPC `-32601 Method not found`.

The full protocol (`acp/interfaces.py:83-158`), with exact signatures (all parameters are
keyword-only, plus `**kwargs` for `_meta` fields / future fields):

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

Methods that matter for the houdini-agent-panel client: the ones genuinely required are
`session_update` (the incoming update stream — this is what draws the chat) and
`request_permission` (the permission dialog). The rest depend on what the agent needs;
if the panel doesn't implement them, the router itself returns a sensible default for terminal
methods (`optional=True, default_result={} | None`, see `acp/client/router.py:103-144`), but
`write_text_file`/`read_text_file`/`request_permission`/`session_update` are NOT optional
in the router (`route_request(...)` without `optional=True`, `acp/client/router.py:95-102`,
`163`) — if the agent calls one of these and the panel doesn't provide the method, it gets a
JSON-RPC `method not found` error. In practice: the agent only calls `fs/read_text_file` /
`fs/write_text_file` if the panel declared support in
`ClientCapabilities.fs` (see §4) — so if you're not implementing it, just don't
declare the capability.

The method **is not async, it's a plain one**: `on_connect(self, conn: Agent) -> None` — called synchronously
right in `ClientSideConnection.__init__`'s constructor (`acp/client/connection.py:83-84`),
if the client has an `on_connect` attribute. A convenient place to store `conn` (the agent
as an `Agent` object) for the client's session, if it's ever needed, e.g. to call
`conn.cancel(...)` from inside the Client object itself.

Mapping from JSON-RPC method name → Python method (`CLIENT_METHODS`, `acp/meta.py:33-48`):
```
session/request_permission -> request_permission
session/update             -> session_update
fs/write_text_file         -> write_text_file
fs/read_text_file          -> read_text_file
terminal/create            -> create_terminal
terminal/output            -> terminal_output
terminal/release            -> release_terminal
terminal/wait_for_exit      -> wait_for_terminal_exit
terminal/kill                -> kill_terminal
mcp/connect                 -> (not in the Client Protocol — internal, for elicitation/mcp transports)
mcp/message                  -> (see above)
mcp/disconnect                -> (see above)
elicitation/create             -> create_elicitation
elicitation/complete            -> complete_elicitation
```

## 3. Outgoing client calls — `ClientSideConnection`

`acp/client/connection.py` — the `ClientSideConnection` class (what
`spawn_agent_process`/`connect_to_agent` return). All methods are `async`, all parameters keyword-only,
plus `**kwargs` gets merged into the request's `_meta` (`field_meta`).

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

    async def cancel(self, session_id: str, **kwargs) -> None:  # this is a NOTIFICATION, not a request — no reply
        ...

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]: ...
    async def ext_notification(self, method: str, params: dict[str, Any]) -> None: ...

    async def close(self) -> None: ...
    async def __aenter__(self) -> "ClientSideConnection": ...
    async def __aexit__(self, exc_type, exc, tb) -> None: ...  # closes the connection
```

Important detail about `cancel`: it's a JSON-RPC *notification* (`session/cancel`), not a request —
the method doesn't block waiting for a reply and always returns `None` immediately after sending
(`await notify_model(...)`, `acp/client/connection.py:269-275`). The agent finishes a cancelled
prompt through the usual `session/update` stream + `PromptResponse.stop_reason == "cancelled"`
on the currently pending `await conn.prompt(...)`.

`session/list`, `session/fork`, `session/resume`, `session/close` are schema v1.19.0 methods,
they exist in the SDK, but in practice not every agent supports them
(depends on `AgentCapabilities.session_capabilities`, see §4). For a minimal ACP client
(Claude Code/Codex/Gemini CLI as the agent), the ones actually needed are just: `initialize`, `new_session`,
`prompt`, `cancel`, `set_session_mode`, optionally `authenticate`.

This project ended up going past minimal for `session/close` specifically:
`AcpClient.close_session()` (`client.py`, `docs/architecture.md` §6) calls
it deliberately, gated on `sessionCapabilities.close`, because with Claude
each session is a whole agent-SDK process and its own MCP server fleet —
leaving one open after the artist has moved on from it is a real resource
leak, not just an unused capability.

JSON-RPC method names for the agent (`AGENT_METHODS`, `acp/meta.py:3-32`):
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
session/delete        -> (absent from the Agent Protocol / ClientSideConnection — DeleteSessionRequest exists in the schema, but isn't wrapped by a method)
session/fork          -> fork_session
session/resume        -> resume_session
session/close         -> close_session
```

## 4. Models

All models are pydantic v2 `BaseModel`s with camelCase aliases (`Field(alias="...")`),
while the Python field names are snake_case. All of them have a `field_meta: dict|None` aliased to `_meta`
(a general protocol extension point) — don't confuse this with the `**kwargs` parameter on methods
(which is exactly what maps into this field).

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
`InitializeRequest` is lenient about a non-numeric `protocol_version` (e.g. a date string from
older Zed clients) — it converts to `1` on a parse error
(`_coerce_protocol_version`, `acp/schema.py:6349-6361`) — this is a safeguard on the AGENT
SIDE, it doesn't matter to the panel as a client, just send `acp.PROTOCOL_VERSION` (int `1`).

`Implementation` (around `acp/schema.py:1250`) is `{name: str, version: str, title: str|None}`
(not spelled out field by field — it's a trivial model like an npm package info object).

### ClientCapabilities (`acp/schema.py:5818-5947`)
```python
class ClientCapabilities(BaseModel):
    fs: FileSystemCapabilities | None = FileSystemCapabilities()   # readTextFile/writeTextFile — both default to False
    terminal: bool | None = False
    session: ClientSessionCapabilities | None = None
    plan: PlanCapabilities | None = None            # UNSTABLE: plan_update/plan_removed updates
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
If the panel wants the agent to read/write files through the client (rather than directly
through the filesystem) — it needs to set
`fs=FileSystemCapabilities(read_text_file=True, write_text_file=True)`
and implement `Client.read_text_file`/`write_text_file`. Otherwise the agent reads/writes files
itself (it has `cwd`).

### AgentCapabilities (`acp/schema.py:6412-...`, start)
```python
class AgentCapabilities(BaseModel):
    load_session: bool | None = False                          # alias "loadSession" — support for session/load
    prompt_capabilities: PromptCapabilities | None = PromptCapabilities()   # alias "promptCapabilities": image/audio/embeddedContext bool flags
    mcp_capabilities: McpCapabilities | None = McpCapabilities()            # alias "mcpCapabilities": http/sse/acp bool flags
    session_capabilities: SessionCapabilities | None = SessionCapabilities()  # alias "sessionCapabilities" — fork/resume/close/list/delete
    auth: AgentAuthCapabilities | None = ...
```
Check `agent_capabilities.prompt_capabilities.image` before sending an `ImageContentBlock`
in the prompt — if it's `False`, the agent may reject it.

### NewSessionRequest / NewSessionResponse (`acp/schema.py:5172-5219`, `6216-6265`)
```python
class NewSessionRequest(BaseModel):
    cwd: str                                    # required, an absolute path
    additional_directories: list[str] | None = None   # alias "additionalDirectories"
    mcp_servers: list[HttpMcpServer|SseMcpServer|AcpMcpServer|McpServerStdio]   # alias "mcpServers" — a list, not Optional (but ClientSideConnection.new_session itself substitutes [] if None, see §3)

class NewSessionResponse(BaseModel):
    session_id: str                             # alias "sessionId"
    modes: SessionModeState | None = None        # the agent's initial set of modes, if it supports them
    config_options: list[SessionConfigOptionSelect|SessionConfigOptionBoolean] | None = None  # alias "configOptions"
```

### MCP servers — the exact record shape (`acp/schema.py:2270-2372`)
There's no single discriminating `type` literal on the base classes (it's a Union with no
discriminator in `NewSessionRequest.mcp_servers`, meaning pydantic tries them in order/by
fields) — 4 variants:

```python
class McpServerStdio(BaseModel):
    name: str
    command: str             # absolute path to the MCP server's executable
    args: list[str]           # NOT Optional — a list of arguments (can be [])
    env: list[EnvVariable]     # NOT Optional — a list of {name: str, value: str} (see EnvVariable)

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
    server_id: str    # alias "serverId" — a unique id for the ACP-transport MCP server
```
So yes — there are http and sse variants besides stdio, plus its own "acp" transport
(an MCP server reachable over that same ACP connection). In `interfaces.py`/`connection.py` they're
exported as `HttpMcpServer`/`SseMcpServer`/`AcpMcpServer` (thin subclasses of
`McpServerHttp`/`McpServerSse`/`McpServerAcp` with no extra fields, `acp/schema.py:4387-4398`) —
these are the "public" names that should be used in the `mcp_servers` list.
`EnvVariable`: `{name: str, value: str}` (`acp/schema.py:210-228`, trivial).

### PromptRequest — content blocks (`acp/schema.py:5222-5269`, blocks — `4819-4838`)
```python
prompt: list[TextContentBlock | ImageContentBlock | AudioContentBlock | ResourceContentBlock | EmbeddedResourceContentBlock]
```
The discriminator is the `type` field (a Literal) on each variant:
```python
class TextContentBlock(TextContent):        type: Literal["text"]
class ImageContentBlock(ImageContent):        type: Literal["image"]
class AudioContentBlock(AudioContent):        type: Literal["audio"]
class ResourceContentBlock(ResourceLink):     type: Literal["resource_link"]
class EmbeddedResourceContentBlock(EmbeddedResource):  type: Literal["resource"]
```
The base fields (no discriminator, these are the parent classes):
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
Constructor helpers (`acp/helpers.py`, ready to use without importing the schema directly):
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
    # Field(discriminator="session_update")   <-- the discriminator is EXACTLY this field (alias "sessionUpdate"), NOT "type"
```
Each variant is a literal on the `session_update` field (alias `sessionUpdate`):
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
SessionInfoUpdate(_SessionInfoUpdate):  session_update: Literal["session_info_update"]   # presumed; not spelled out in detail
UsageUpdate(_UsageUpdate):              session_update: Literal["usage_update"]
```
`ContentChunk` (the base for user/agent/thought chunks):
```python
class ContentChunk(BaseModel):
    content: TextContentBlock | ImageContentBlock | AudioContentBlock | ResourceContentBlock | EmbeddedResourceContentBlock  # discriminator="type"
    message_id: str | None = None   # alias "messageId" — shared across every chunk of one message
```
A practical breakdown of an incoming `session/update` in Python:
```python
async def session_update(self, session_id, update, **kwargs):
    match update.session_update:  # this is the Python field name (alias "sessionUpdate" only on the wire)
        case "agent_message_chunk":
            text = update.content.text if update.content.type == "text" else None
        case "tool_call":
            ...  # update is a ToolCallStart, fields as in ToolCall (see below)
        case "tool_call_update":
            ...  # update is a ToolCallProgress (all fields Optional — a partial update)
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
    locations: list[ToolCallLocation] | None = None   # for "follow-along" in the UI — highlighting a file
    raw_input: Any | None = None    # alias "rawInput"
    raw_output: Any | None = None   # alias "rawOutput"

class ToolCallUpdate(BaseModel):   # ToolCallProgress = ToolCallUpdate + session_update literal
    tool_call_id: str        # required — the id of the call being updated
    kind: ToolKind | None = None
    status: ToolCallStatus | None = None
    title: str | None = None
    content: list[...] | None = None       # REPLACES the whole list on update, doesn't patch element by element
    locations: list[ToolCallLocation] | None = None
    raw_input: Any | None = None
    raw_output: Any | None = None
```
`ToolCallLocation` is roughly `{path: str, line: int|None}` (`acp/schema.py:186-209`).
Three variants of a tool call's content, discriminated by the `type` field:
```python
class ContentToolCallContent(Content):   type is inherited from Content — there's no literal at this level,
    # in practice: {"type": "content", "content": <ContentBlock>}
class FileEditToolCallContent(Diff):      # {"type": "diff", "path": str, "new_text": str, "old_text": str|None}
class TerminalToolCallContent(Terminal):  # {"type": "terminal", "terminal_id": str}
```
(see the helpers `tool_content()/tool_diff_content()/tool_terminal_ref()` in `acp/helpers.py:129-138`
— they set the correct `type` themselves.)

Helpers for quickly assembling updates (`acp/helpers.py`):
```python
start_tool_call(tool_call_id, title, *, kind=None, status=None, content=None, locations=None, raw_input=None, raw_output=None) -> ToolCallStart
start_read_tool_call(tool_call_id, title, path, *, extra_options=None) -> ToolCallStart   # kind="read", sets locations/raw_input itself
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
    entries: list[PlanEntry]   # the FULL list every time — the client replaces the plan wholesale, it doesn't patch
```
Helpers: `plan_entry(content, *, priority="medium", status="pending") -> PlanEntry`,
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
    tool_call: ToolCallUpdate   # alias "toolCall" — details of the call that needs permission
    options: list[PermissionOption]

class RequestPermissionResponse(BaseModel):
    outcome: DeniedOutcome | AllowedOutcome   # discriminator="outcome" (the "outcome" field itself is a literal inside the variant)

class DeniedOutcome(BaseModel):
    outcome: Literal["cancelled"]

class SelectedPermissionOutcome(BaseModel):    # AllowedOutcome = SelectedPermissionOutcome + outcome: Literal["selected"]
    option_id: str    # alias "optionId" — which of PermissionOption.option_id was chosen

class AllowedOutcome(SelectedPermissionOutcome):
    outcome: Literal["selected"]
```
So the reply takes one of two shapes: either `RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))`
(the user cancelled/closed the dialog), or
`RequestPermissionResponse(outcome=AllowedOutcome(outcome="selected", option_id="allow_once"))`
(the user picked one of the offered options). Note: the discriminating field is called `outcome`
BOTH at the wrapper level (`RequestPermissionResponse.outcome`) AND as a literal inside the
variant itself (`DeniedOutcome.outcome`/`AllowedOutcome.outcome`) — don't confuse these two
different `outcome`s.

### AvailableCommand / AvailableCommandsUpdate (`acp/schema.py:5084-...`, `5116-...`, `5712-5714`)
```python
class AvailableCommand(BaseModel):
    name: str              # e.g. "create_plan"
    description: str
    input: AvailableCommandInput | None = None
    # AvailableCommandInput = RootModel[UnstructuredCommandInput]
    # UnstructuredCommandInput (acp/schema.py:1795) = {"hint": str} — its only field,
    # a human-readable hint about what's expected after the command name (e.g. "/model <name>").

class _AvailableCommandsUpdate(BaseModel):
    available_commands: list[AvailableCommand]   # alias "availableCommands"

class AvailableCommandsUpdate(_AvailableCommandsUpdate):
    session_update: Literal["available_commands_update"]
```
Helper: `update_available_commands(commands: Iterable[AvailableCommand]) -> AvailableCommandsUpdate`.

### SessionModeState / SessionMode / CurrentModeUpdate (`acp/schema.py:3918-...`, `1381-...`, `1815`/`4157`)
```python
class SessionMode(BaseModel):
    id: str
    name: str
    description: str | None = None

class SessionModeState(BaseModel):
    current_mode_id: str          # alias "currentModeId"
    available_modes: list[SessionMode]   # alias "availableModes"

class CurrentModeUpdate(_CurrentModeUpdate):     # _CurrentModeUpdate, most likely {current_mode_id: str}
    session_update: Literal["current_mode_update"]
```
Helper: `update_current_mode(current_mode_id: str) -> CurrentModeUpdate`.
`SetSessionModeRequest`/`Response` — the outgoing `set_session_mode(session_id, mode_id)` call
(see §3) switches the current mode (e.g. "ask"/"code"/"architect" — the actual ids
depend on the agent and arrive via `SessionModeState.available_modes`).

### PromptResponse / StopReason (`acp/schema.py:4051-4087`, `15`)
```python
StopReason = Literal["end_turn","max_tokens","max_turn_requests","refusal","cancelled"]

class PromptResponse(BaseModel):
    stop_reason: StopReason    # alias "stopReason"
    usage: Usage | None = None   # UNSTABLE — tokens
```

## 5. The `auth_required` error

There's no separate exception class — a single `acp.RequestError(code, message, data=None)`
(`acp/exceptions.py`), a plain `Exception` with `.code`/`.data` fields:
```python
class RequestError(Exception):
    def __init__(self, code: int, message: str, data: Any | None = None) -> None: ...
    @classmethod
    def auth_required(cls, data=None) -> "RequestError":
        return cls(-32000, "Authentication required", data)
    # + parse_error(-32700), invalid_request(-32600), method_not_found(-32601),
    #   invalid_params(-32602), internal_error(-32603), resource_not_found(-32002)
```
`auth_required` is a constructor meant for the AGENT SIDE (so the agent can raise this
error from its own `initialize`/`new_session`/`prompt` if login is required). On the CLIENT
side (`ClientSideConnection`), it just arrives as a JSON-RPC error object and gets
translated into a `RequestError` with the same code (`acp/connection.py:279-289`,
`_handle_response`):
```python
if "error" in message:
    error_obj = message.get("error") or {}
    self._state.reject_outgoing(request_id, RequestError(
        error_obj.get("code", -32603), error_obj.get("message", "Error"), error_obj.get("data"),
    ))
```
The practical pattern for the client:
```python
try:
    resp = await conn.new_session(cwd=cwd, mcp_servers=[])
except acp.RequestError as exc:
    if exc.code == -32000:
        # a login is required — call conn.authenticate(method_id=...) with one of
        # InitializeResponse.auth_methods, then retry new_session
        ...
    else:
        raise
```
The code `-32000` is a convention of the SDK/protocol itself (not a standard JSON-RPC 2.0 code,
it's in the application-specific range -32000..-32099), but there's no dedicated `AuthRequiredError`
subclass — check `exc.code == -32000` specifically.

## 6. Client examples/tests inside the package itself

The installed distribution (`site-packages/acp/`) has NO `examples/` or `tests/` directory —
it's just the library itself, with no source repository/README with examples. There's no
full "literal client example" in the package. But there is an `acp.contrib` module with
ready-made, higher-level utilities — the public API (not private, no `_`-prefix at the
module level) of each file was verified by reading the source:

**`acp/contrib/session_state.py`** — accumulates session state from the `session_update`
stream (exactly what the panel needs to keep the chat model up to date):
```python
class ToolCallView(BaseModel): ...      # a snapshot of one tool call (a public representation field)
class SessionSnapshot(BaseModel): ...    # a snapshot of the whole session (messages, tool calls, plan, mode...)

class SessionAccumulator:
    def __init__(self, *, auto_reset_on_session_change: bool = True) -> None: ...
    def reset(self) -> None: ...
    def subscribe(self, callback: Callable[[SessionSnapshot, SessionNotification], None]) -> Callable[[], None]: ...
    def apply(self, notification: SessionNotification) -> SessionSnapshot: ...   # feed every session_update in here
    def snapshot(self) -> SessionSnapshot: ...
```
In practice: inside `Client.session_update(self, session_id, update, **kwargs)` it's enough to
build a `SessionNotification(session_id=session_id, update=update)` (or use a ready-made one —
if you're patching the router, the model is already there) and call
`accumulator.apply(notification)`, subscribing via `subscribe(...)` to update the UI.

**`acp/contrib/tool_calls.py`** — constructing/tracking ToolCall objects on the AGENT SIDE
(not the client) — useful only if the panel ever ends up writing its own agent, not directly
applicable to the client:
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

**`acp/contrib/permissions.py`** — a permission broker, also an AGENT-SIDE utility (it builds
`RequestPermissionRequest`/default options to send to the client), not needed on the
panel-as-client side:
```python
def default_permission_options() -> tuple[PermissionOption, PermissionOption, PermissionOption]: ...
class PermissionBroker:
    def __init__(self, ...) -> None: ...
    async def request_for(self, ...) -> RequestPermissionResponse: ...
```
Conclusion: for the panel's client, only `session_state.SessionAccumulator` is useful — it
genuinely saves writing your own reducer over the `session_update` stream. `tool_calls`/`permissions`
from `contrib` are helper classes for the AGENT SIDE, not directly useful to the panel.

A minimal, complete client skeleton assembled from the verified fragments above (not a literal
quote, but a composition of real signatures — use it as a starting point):
```python
import acp

class HoudiniPanelClient:
    def __init__(self):
        self.conn: acp.Client | None = None

    def on_connect(self, conn):        # sync, called by ClientSideConnection.__init__
        self.agent = conn

    async def session_update(self, session_id, update, **kwargs):
        ...  # see §4 SessionNotification — draw the chat based on update.session_update

    async def request_permission(self, session_id, tool_call, options, **kwargs):
        ...  # show a dialog, return acp.RequestPermissionResponse(outcome=...)

    # write_text_file/read_text_file/create_terminal/... — only if the capability was declared

async def main():
    client = HoudiniPanelClient()
    async with acp.spawn_agent_process(client, "claude-code-acp") as (conn, process):
        init = await conn.initialize(
            protocol_version=acp.PROTOCOL_VERSION,
            client_capabilities=acp.schema.ClientCapabilities(),  # or the default
        )
        session = await conn.new_session(cwd="/path/to/hip/project", mcp_servers=[])
        resp = await conn.prompt(
            session_id=session.session_id,
            prompt=[acp.text_block("hello")],
        )
        print(resp.stop_reason)
```

## 7. Other things worth knowing for the implementation

- A `RequestError` on a CLIENT call (`await conn.prompt(...)` and the like) arrives as a
  regular exception from `await request_model(...)` → `Connection.send_request` → `future`
  (`acp/connection.py:144-159`, `273-289`) — wrap it in `try/except acp.RequestError`.
- All Python names are snake_case, every name on the wire (JSON) is camelCase via `Field(alias=...)`;
  pydantic models are built from snake_case kwargs (`InitializeRequest(protocol_version=..., ...)`),
  serialization to camelCase happens automatically on `model_dump(by_alias=True)` inside the SDK
  (the client doesn't need to worry about aliases itself — just use the Python field names).
  The one practical exception is `Client.session_update`, where the discriminator
  on the wire is `sessionUpdate`, while the Python attribute is `session_update` (the same alias pattern).
- `**kwargs` in method signatures and model constructors is NOT arbitrary extra
  parameters — it all goes into a specific request/response's `field_meta` (`_meta`) —
  a protocol extension point, not a way to sneak undocumented parameters past the model.
- Cancel is a notification, not a request: `await conn.cancel(session_id=...)` doesn't wait
  for a reply; you'll see the effect of the cancellation as
  `PromptResponse.stop_reason == "cancelled"` on the prompt that was cancelled, and/or as a
  `session/update` stream, if the agent managed to say something more before the cancellation.

## 8. What real agents actually send (measured, not read off the schema)

Everything above this section comes from reading `acp`'s own source. This section is different:
it's what six real, installed agents actually put on the wire for `available_commands` and
`configOptions`, measured by launching each one for real (`runtime.launch_spec` +
`client.AcpClient`, a real `session/new`, no mocks) and logging what came back. The owner's
"the model chip is confusing" / "slash commands don't ask for arguments" reports both turned out
to hinge on agent-specific behavior the schema alone doesn't tell you, and a plausible guess about
that behavior was wrong twice in the same day — this is written down so nobody has to re-run the
probe to find out again.

**`configOptions` are real and used for the model picker on both agents that publish one.**
Claude (`claude-agent-acp` 0.64.2) sends exactly 4: `mode` (6 choices), `model` (5 choices:
`default`/`opus[1m]`/`claude-fable-5[1m]`/`sonnet`/`haiku`), `effort` (6 choices), `fast` (2
choices). Every model choice carries its own `description` (e.g. `default` →
`"Opus 5 with 1M context · Best for everyday, complex tasks"`) — `effort`'s choices don't.
**Codex has no `model` slash command at all** — it exposes model/effort/approval-mode purely
through `configOptions`, same mechanism as Claude, and nothing under `available_commands` names
a model.

**`AvailableCommand.input` is real and populated, by the agents that build on Claude Code and
Codex CLI, with genuinely useful hints — and the panel doesn't read it at all (`ui/composer.py`
before this fact was written: `_slash_query` drops the popup the instant a space follows the
command name, and `_CommandPopup.set_commands` never looks at `.input`).** Measured examples,
verbatim: `effort` → `<low|medium|high|xhigh|max|ultracode|auto>` (Claude's own built-in),
`model` → `<model>`, `fast` → `[on|off]`, `color` → `[red|blue|green|yellow|purple|orange|pink|
cyan|default]`; Codex's `review`/`review-branch`/`review-commit`/`goal` similarly carry a real
hint (`"branch name"`, `"commit sha"`, `[<objective>|clear|pause|resume]`). Not every command has
one — Claude's own `plan`, `mcp`(as a bare list action), `status`, `logout` and Codex's `plan`,
`mcp`, `skills`, `status`, `compact`, `logout` all report `input=None`.

**Gemini CLI never populates `input`, for any of its 20 commands, including ones that plainly
need an argument** (`extensions install <git-repo-or-path>` is `input=None` same as `help`). This
was stable across two independent runs. Per this project's own standing rule ("the agent doesn't
support it — the control doesn't get drawn"), an argument-hint popup for Gemini's commands has
nothing to draw from — that is Gemini's choice, not a client gap.

**`available_commands` mixes in the account's own personal skill/plugin marketplace, and this is
account-scoped, not project-scoped.** On a machine with a personal Claude Code marketplace
installed, `claude-acp`, `codex-acp` and `grok-build` all included that marketplace's entries
(e.g. a personal "ab-testing" skill) as if they were the agent's own commands. Verified this is
NOT a `cwd` artifact: re-running with `cwd` pointed at a freshly-created empty directory (instead
of `$HOME`) produced the identical, unchanged list — the source is the artist's account, not the
project folder. Practical consequence: on any real machine where the artist (or the studio image)
has personal marketplace skills installed, the `/` popup will show those alongside the agent's own
built-ins, indistinguishably in most cases. Codex is the one exception with a distinguishing,
structural marker: marketplace-sourced entries there carry a literal `$` prefix in `name`
(`$may-hub:sync`); Claude and Grok give no such marker — their marketplace and built-in entries
are lexically identical in shape.

**Not established, and not worth the cost of establishing:** which of `grok-build`'s and
`opencode`'s `available_commands` are genuinely native versus marketplace-sourced (no
distinguishing marker on either, and isolating a clean account/`$HOME` to test with would break
their own login) — same caveat, `input` presence on either could not be attributed with
confidence. `kimi-cli`'s `available_commands` are entirely unknown: it requires an interactive
`login` (`auth_required: ['login']`) that a headless probe can't complete.


## 9. What a never-configured agent does — measured, not assumed

Six agents installed onto a genuinely empty `HOME` (a temp dir set before
importing the package, so no credential of the developer's was readable),
each asked to open a session, 150s ceiling. Measured 2026-08-04.

**All six connect and then say nothing.** `initialize` succeeds and reports
the agent's real name and version — and, on the fresh machine,
`authMethods: []`. No `session/new` answer, no error, no auth request. The
panel's sign-in screen is built from those auth methods, so with an empty
list there is literally nothing to draw.

This is the shape of the trap: an agent that connects looks working, and the
artist has no way to learn that it needs `/login` — the very command that
would fix it lives inside a session the agent will not open.

Zed documents the same division and the same escape hatch: "Claude Agent
owns its own authentication and billing… open a Claude Agent thread, run
`/login`, and authenticate." So the fix is not to invent a login flow but to
say what is wrong and offer that command.

**Not established**: whether the silence is refusal or an unbounded wait —
150s is long enough to be a bad experience either way, and the panel's own
`_NEW_SESSION_GRACE_MS` fires long before it.

## 10. MCP servers: ours AND the agent's own

Zed's docs state both routes are live — "Zed-configured MCP servers may be
forwarded to External Agents over ACP. External Agents may also read their
own native MCP configuration" — and confirmed here for `claude-acp`:
handing it a single `fxhoudini` entry in `session/new` opens a session
normally on a machine whose agent config already carries servers of its own.
Nothing has to be merged or suppressed on our side.

Consequence for support: a missing tool has two possible homes. Ours goes
out on every `session/new` (`scene.mcp_servers`); the agent's live in its
own config file, which the panel never reads or writes.

---

## 11. What each agent actually says about signing in

Measured on the Linux machine, each agent launched with a throwaway `HOME`
so none of them had ever been configured (`scratchpad/authprobe.py`, run
through the deps tree's own `acp`):

| agent | `initialize.authMethods` | `session/new` |
|---|---|---|
| `claude-acp` 0.64.2 | `[]` — none at all | **succeeds**, then fails at the first prompt |
| `codex-acp` 1.1.9 | `api-key`, `chat-gpt` | **fails**: `Authentication required` |
| `opencode` 1.18.12 | `opencode-login` | **succeeds** |

Three consequences the UI depends on:

1. **An open session is not evidence of being signed in.** Two agents out
   of three open one while signed out. Anything drawn from "a session
   exists" is wrong for those two — this is how a never-configured Claude
   came to be shown a "Sign out" button.
2. **`authMethods` empty does not mean "no login" — and does not mean
   `/login` either.** This was written the second way first, from Zed's
   documentation rather than from the agent, and `claude-acp` refuted it in
   the panel: told to type `/login`, it answered "/login isn't available in
   this environment". The measurement was already in the table above —
   `claude-acp` returns an EMPTY `availableCommands` — and went unread.
   Ask the session what it has; do not assume a command exists because
   another agent has one. `claude-acp` expects credentials to already exist
   on the machine (its own CLI's login, or an API key in the environment).
3. **A completed turn is the one signal all three agree on.** None of them
   answers a prompt for an account that is not signed in. That is what the
   panel records, and it records it persistently, because otherwise the
   evidence is lost on every Houdini restart.

Not established: whether `gemini`, `grok` and `kimi` behave like Claude or
like Codex — they were not installed on the machine this was measured on.

---

## 12. What `authenticate` actually does, per method

Measured on the Linux machine, `codex-acp` 1.1.9, clean `HOME`
(`scratchpad/loginprobe.py`):

| method | result |
|---|---|
| `api-key` | fails at once: `Internal error: CODEX_API_KEY or OPENAI_API_KEY is not set` |
| `chat-gpt` | **does not return** — still pending at 45s, while a browser window opens |

So `authenticate` is not a request/response for the browser flow: it stays
open until the human finishes in the browser, and returning without raising
is the success signal. A client-side timeout on it would break a login that
is working. The panel has none, and must not grow one.

No URL is emitted anywhere the client can see it — nothing on stderr, nothing
in session updates. The agent opens the browser itself, so a "click here to
sign in" link cannot be offered for this agent: only an explanation that the
window is coming and may take a few seconds.

`api-key` cannot be completed from the panel at all. It reads the environment,
which is why `shellenv.capture` matters: a key exported in `~/.zshrc` reaches
the agent, one typed into a dialog would have nowhere to go.

Not established: whether `claude-acp`, `gemini`, `grok` and `kimi` emit a URL
their clients could open — only `codex-acp` was probed this way.

---

## 13. `gemini`, `grok` and `kimi` — the three agents left unmeasured in §11

Measured on the same Linux machine, same method as §11-12
(`scratchpad/loginprobe.py`, `scratchpad/oauthprobe.py`, `scratchpad/kimi_pty.py`),
each agent launched with a throwaway `HOME`. This closes the "not
established" note at the end of §11.

| agent | `initialize.authMethods` | `session/new` |
|---|---|---|
| `gemini` (`@google/gemini-cli` 0.53.1, `--acp`) | `oauth-personal`, `gemini-api-key`, `vertex-ai`, `gateway` | **succeeds**, 0 available commands |
| `grok-build` (`@xai-official/grok` 0.2.120, `agent stdio`) | `grok.com` | **fails**: `Authentication required` |
| `kimi` 1.49.0 | `login` (see below — not a plain id) | not reached (authenticate never returns) |

### `authenticate`, per method, exact text

| agent | method | result |
|---|---|---|
| `gemini` | `oauth-personal` | **does not return** — `TimeoutError` at both 45s and 90s. Meanwhile stderr printed `Failed to authenticate with authorization code:invalid_grant` and `Failed to authenticate with user code. Retrying...` — it drives an OAuth *device-code* flow (a user code + polling), and the polling was failing/retrying throughout the probe window. |
| `gemini` | `gemini-api-key` | returns immediately: `OK field_meta=None`. No validation happens at `authenticate` time — nothing was set in the environment for it to check. |
| `gemini` | `vertex-ai` | same: immediate `OK field_meta=None`. |
| `gemini` | `gateway` | same: immediate `OK field_meta=None`. |
| `grok-build` | `grok.com` | **does not return** — `TimeoutError` at 45s (short probe) and 90s (longer probe, see below). |

For `gemini`, a prompt of literally `/login` after `session/new` also timed
out (60s, no reply, no error) — consistent with the account still being
mid-OAuth-retry from the `authenticate` call moments before.

### Does anything emit a URL? Does a browser open?

**`gemini`**: no URL anywhere — none in stderr, none in any `session/update`
(checked by regexing every chunk seen by the client and every stderr line).
No browser-related process appeared in the process tree during the full 90s
`oauth-personal` wait (`ps -ef` filtered for `firefox`/`chromium`/`google-chrome`/
`xdg-open` found nothing, only the `npm exec @google/gemini-cli` node process
itself) — inferred, from the absence of any such process, that gemini does
not open a browser on this machine for `oauth-personal`. It only prints the
device-code retry failures quoted above; a client would have to read those
verbatim, since nothing structured (no code, no URL) travels over the wire.

**`grok-build`**: no URL either — stdout/stderr from the ACP channel were
completely empty (`stderr tail: []`) during the whole `authenticate(grok.com)`
call. But the process tree told a different story than gemini's: 20s into the
call,
```
node .../grok agent stdio
 └─ grok agent stdio
     └─ [firefox] <defunct>
```
— **grok does spawn a browser** (Firefox, at `~/.nix-profile/bin/firefox`,
version 141.0.3, confirmed present on the machine) for `grok.com` auth. It
went `<defunct>` (zombie, exited) within the same `ps` snapshot — the probe
deliberately withheld `DISPLAY` from the agent's environment (same env
allowlist as every other probe in this file), so Firefox had nowhere to
open a window and died immediately. This is inferred from the env passed
in, not observed on a screen: on a machine with a real `DISPLAY`, the same
spawn would very likely produce a visible window instead of a zombie, but
that wasn't tested. Either way, no URL crosses the ACP channel for grok —
whatever page Firefox would have opened is a browser-side argument, invisible
to the client.

### `kimi`: what it's actually waiting for — and it isn't a TTY

The pty hypothesis from §12 is **refuted**. `scratchpad/kimi_pty.py` gives
`kimi acp`'s own stdin/stdout a real pty (`pty.openpty()`, local echo
disabled, raw JSON-RPC hand-written over the master fd — the `acp` SDK's
stream helpers assume plain pipes, so this probe talks the wire protocol
directly). `authenticate(methodId="login")` still never returns — 60s with
the pty attached, same as without one — and no new child process appears in
`kimi`'s process tree in response to the call (only the `Kimi Code` worker
that was already there from `initialize`). A pty on the ACP channel itself
changes nothing.

What actually explains the wait was sitting in the `initialize` response the
whole time, verbatim (only truncated here for width):
```json
"authMethods": [{
  "_meta": {
    "terminal-auth": {
      "command": "/home/may/.local/share/houdini-agent-panel/agents/kimi/1.49.0/kimi",
      "args": ["login"],
      "label": "Kimi Code Login",
      "env": {},
      "type": "terminal"
    }
  },
  "description": "Run `kimi login` command in the terminal, then follow the instructions to finish login.",
  "id": "login",
  "name": "Login with Kimi account"
}]
```
Kimi isn't asking the ACP stdio channel for anything at all. It's telling the
client, in the one auth method it offers, to spawn a **second, independent
process** — the same `kimi` binary, invoked with `login` instead of `acp` —
attached to a real interactive terminal, outside the ACP JSON-RPC channel
entirely, and let the human finish there.

This is close to — but not quite — the protocol's own built-in shape for
exactly this case: `acp.schema.AuthMethodTerminal`/`TerminalAuthMethod`
(`schema.py:1177`, `:3867`) has `id`/`name`/`description` plus top-level
`args`/`env` and a discriminator `type: "terminal"`, meant for precisely
"run the agent binary again with these args for terminal auth." Kimi's
payload carries the same information but nested one level down, inside a
custom `_meta.terminal-auth` key, with no top-level `type` field on the auth
method object itself. Consequence: `acp`'s typed `InitializeResponse.auth_methods`
parses this entry as the fallback `AuthMethodAgent` (`schema.py:1221` — the
variant with no `type` discriminator, so it's what pydantic reaches for when
none of `EnvVarAuthMethod`/`TerminalAuthMethod` match) — `args`/`env`/`type`
never surface as typed fields. A client has to know to open `field_meta`
(`_meta`) on the auth method and look for a `terminal-auth` key by
convention; there's nothing in the schema forcing that key's name or shape.

### Consequences for the UI

1. **Gemini and grok both hang on their OAuth method with no client-visible
   progress, and neither can be helped by a client-drawn link** — gemini
   because it never emits a URL (device-code retries only, in stderr text),
   grok because whatever it would show lives inside a browser window it
   spawns itself. The one thing a client CAN say for both is "opening a
   browser / check your terminal for a code," not render a clickable link.
2. **Grok is the second agent (after Codex, §12) confirmed to open a real
   browser process for its OAuth method** — same shape as Codex's
   `chat-gpt`: a `authenticate` call that blocks and a spawned browser, no
   panel-side timeout allowed on it.
3. **Kimi needs a capability the panel doesn't have yet: running a second,
   separate, real interactive terminal for the agent binary, outside the ACP
   connection.** This is not a variant of "wait longer" or "attach a pty" —
   both were tried against the wrong channel. The actual fix has to open a
   terminal (or terminal-emulator widget) running the exact `command`+`args`
   from `_meta.terminal-auth`, which means the panel must read `field_meta`
   on auth methods at all — today nothing in the codebase does.
4. **Not every "terminal" auth method will look like kimi's.** Kimi doesn't
   use the schema's own top-level `type: "terminal"` shape, it improvised a
   `_meta` key. A client that only checks `method.type == "terminal"` will
   silently miss kimi's method entirely and must special-case (or generically
   scan `field_meta`) to catch it.

### Not established

- Whether gemini's `oauth-personal` or grok's `grok.com` succeed end-to-end
  when a human actually completes the browser step — both probes ran
  headless and unattended, by design (this is what "never configured"
  means), so neither flow was carried to a real login.
- Whether grok's Firefox spawn would show a real window given a `DISPLAY`
  — inferred from the env passed to the agent (no `DISPLAY` key in the
  allowlist) and the zombie process, not observed on a screen.
- Whether `gemini-api-key`/`vertex-ai`/`gateway` do anything at real prompt
  time when their expected env vars are actually absent — `authenticate`
  itself returned OK instantly for all three without checking; the probe
  didn't go on to prompt with each selected, so what backs the "OK" is
  unmeasured.
- Any agent besides Codex (client `create_terminal`, unrelated) actually
  using the protocol's stock `TerminalAuthMethod` with a top-level `type`
  field — kimi's variant is the only terminal-flavored one measured, and it
  doesn't use that shape.

---

## 14. What `kimi login` actually prints, and who else carries `_meta` on an auth method

Two follow-ups to §13, both measured on the same Linux machine.

### `kimi login`, run for real, killed before it could finish

§13 established that kimi's one auth method points the client at a second,
separate command (`kimi login`) instead of anything on the ACP channel. This
runs that exact command directly — the real binary,
`~/.local/share/houdini-agent-panel/agents/kimi/1.49.0/kimi login`, attached
to a pty (`scratchpad/kimi_login_capture.py`) — captured its first ~25s of
output, then `SIGTERM`'d the whole process group before it could complete
anything. No credential was entered, no login was completed.

It prints a real URL and a device code, verbatim (control codes/spinner
frames trimmed for readability, the URL itself is untouched):
```
Please visit the following URL to finish authorization.
Verification URL: https://www.kimi.com/code/authorize_device?user_code=14OI-AX7F
⠋ Waiting for user authorization...
```
then sits polling with a spinner, unbounded, until killed.

This is the one exception worth having, exactly as hypothesized before
measuring it: kimi's flow is not opaque like gemini's or grok's — a client
that runs `kimi login` in a terminal it owns (a pty it reads, not necessarily
one a human types into) can parse this exact `Verification URL: <url>` line
and show the artist a real, clickable link, instead of just "check your
terminal." The user code (`14OI-AX7F` here — presumably regenerated per run)
is also right there in the same line if a client prefers to show it
separately from the URL, device-code style.

**Not established:** whether the URL/user-code always appears on this same
line/format across runs (this is one run, one code, not a repeated sample),
and what kimi prints on success (killed before that point, deliberately).

### `_meta` on `authMethods`, raw JSON, no SDK parsing in the way

`scratchpad/rawinit.py` sends a hand-written `initialize` request over plain
pipes and prints the response before any pydantic model touches it — the
same blind spot that hid kimi's payload in §13 (the typed `auth_methods`
falls back to `AuthMethodAgent`, which has no field for arbitrary `_meta`
content beyond the raw dict). Re-probed the five reachable agents:

| agent | authMethods (raw) | `_meta` present? |
|---|---|---|
| `claude-acp` 0.64.2 | `[]` | n/a — no methods to carry one |
| `codex-acp` 1.1.9 | `api-key` (has `_meta`), `chat-gpt` (none) | **yes**, on `api-key`: `{"api-key":{"provider":"openai"}}` |
| `opencode` 1.18.12 | `opencode-login` | **no** — none at all |
| `gemini` 0.53.1 | `oauth-personal` (none), `gemini-api-key` (has `_meta`), `vertex-ai` (none), `gateway` (has `_meta`) | **yes**, on two of four: `gemini-api-key` → `{"api-key":{"provider":"google"}}`, `gateway` → `{"gateway":{"protocol":"google","restartRequired":"false"}}` |
| `grok-build` 0.2.120 | `grok.com` | **no** |

So `_meta` on an auth method is real, already-in-production infrastructure —
not a kimi-only quirk — but everyone except kimi uses it for a small typed
hint (which credential provider an `api-key` method is for, which protocol a
gateway speaks), never for kimi's "spawn this command" payload. **`opencode`
describes the identical shape of flow as kimi in plain text only** —
`"description": "Run \`opencode auth login\` in the terminal"` — **with no
structured data backing it at all.** A client that wants to offer opencode
the same "spawn it, parse the output" treatment as kimi has nothing to
`_meta`-scan for; it would have to regex a human-readable sentence to
recover the command, and nothing about that sentence's format is a
contract — the string is prose, not a schema field.

### `opencode auth login`, run for real, killed before it could finish

The follow-up question §14 originally left open: opencode describes the
same "run this in the terminal" shape as kimi, but does it behave the same
way if actually run? Ran the real binary directly,
`~/.local/share/houdini-agent-panel/agents/opencode/1.18.12/opencode auth
login`, pty attached, same method as the kimi capture
(`scratchpad/opencode_login_capture.py`) — captured output, then `SIGTERM`'d
the whole process group before any provider was selected or any credential
typed.

**It is not a URL/device-code flow. It's an interactive TUI menu that
expects keystrokes.** Reconstructed from the captured escape sequences
(cursor-positioning codes stripped, the meaning was unambiguous even in the
raw capture):
```
┌  Add credential
│
◆  Select provider
│  Search: _
│  ↑/↓ to select • Enter: confirm • Type: to search
└
```
A provider picker with a live-filtered search box, navigated with arrow keys
and confirmed with Enter — the same class of UI as an interactive CLI
wizard, not a client-parseable stream. No browser process appeared in the
process tree in the ~8s window checked before the picker settled (`ps -ef`
filtered for `firefox`/`chromium`/`brave`/`google-chrome`/`xdg-open` found
nothing) — consistent with a browser only being relevant *after* a provider
is chosen, a step this probe deliberately did not take.

This is a hard "no" for the same treatment as kimi: a client that only reads
a subprocess's output cannot drive this — it needs to send arrow-key and
Enter keystrokes chosen from a live-rendered menu, which means either a real
terminal-emulator widget with keyboard passthrough (a different, larger
feature than "spawn and read"), or nothing — the artist runs it themselves in
a real terminal and the panel just says so.

### Consequences for the UI

1. **Kimi gets a real exception to "no clickable link is possible."** A
   client that spawns `kimi login` itself (in a pty it owns, reading rather
   than asking a human to type into it) can extract the `Verification URL:`
   line and render an actual link plus the device code, closing the gap
   flagged as a consequence in §13.
2. **A generic "does this method have a spawnable terminal command"
   detector cannot rely on `_meta` alone being present** — three of five
   agents attach a `_meta` to at least one method, for reasons that have
   nothing to do with terminal auth (`api-key`'s provider tag, gateway's
   protocol tag). The detector has to specifically look for a
   `terminal-auth`-shaped key (or whatever kimi's key is called) rather than
   branching on "any `_meta` at all."
3. **Opencode cannot get the same treatment as kimi, and not only because
   its instruction is prose.** Even knowing the exact command
   (`opencode auth login`) changes nothing — it's measured now, not
   inferred: the command opens an interactive menu that needs arrow-key
   input, not a stream a client can read and turn into a link. Carrying the
   command as data in a registry (as opposed to scraping the sentence) would
   still not make this drivable from a panel that only reads output; the
   honest UI is "run this yourself in a terminal," not a spawned, parsed
   flow.

### Not established

- Whether kimi's `Verification URL:` line format is stable across versions
  or runs — sampled once.
- What either the URL or the polling loop resolves to on success — the
  process was killed well before that, by design.
- What opencode's menu does after a provider is picked (does that step show
  a URL, an API-key prompt, something else) — not reached; the probe was
  killed at the first menu, deliberately, before selecting anything.

### `claude setup-token` — the owner's own question, measured

The owner asked directly why the panel can't spawn `claude setup-token` the
way it now can spawn `kimi login`. Measured on the same machine, same
method: `npx --yes @anthropic-ai/claude-code setup-token` (the `claude` CLI
is not installed on this machine — no binary on `PATH`, no
`~/.claude/.credentials.json` — so `npx` was used deliberately, nothing gets
installed system-wide), pty attached, ~25s / ~40 lines captured, then
`SIGTERM` to the whole process group before any code was pasted or any
credential entered. Verified after: no `claude-code`/`setup-token` process
left running.

Reconstructed output (cursor-positioning escape codes stripped, content
otherwise verbatim, the spinner frames collapsed):
```
Welcome to Claude Code v2.1.222

· Opening browser to sign in…
✢ * ✶ ✻ ✽ ✻ ✶ * ✢ · ✢ * ✶ ✻ ✽ ✻ ✶ * ✢
Browser didn't open? Use the url below to sign in (c to copy)

https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e&response_type=code&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback&scope=user%3Ainference&code_challenge=NzsZ25WHsxGrQfve2qCgoKgudLvVHAXj3l_1n2FgBg4&code_challenge_method=S256&state=kaD3sxA7ljbkZpds_92UMQNm_koTDGz0kY_gVvNjwbQ

Paste code here if prompted >
```

Answering each question in order:

1. **A verification URL, yes — printed as a real, complete OAuth
   authorize URL**, not a short device code like kimi's. It's a standard
   OAuth2 PKCE authorization-code request (`code_challenge`/
   `code_challenge_method=S256`, `scope=user:inference`, a `state` nonce) —
   not an interactive menu. No separate short "user code" the way kimi has
   one; the URL itself is the whole artifact.
2. **It does not poll silently like kimi.** After printing the URL it shows
   a literal input prompt, `Paste code here if prompted >`, and waits there
   — this is a manual paste-back flow, not autonomous device-code polling.
   The `redirect_uri` points at a hosted page
   (`platform.claude.com/oauth/code/callback`), not a `localhost` port the
   CLI itself is listening on (contrast Codex's `chat-gpt` method, whose
   authorize URL — seen once already on this same machine, in an unrelated
   leftover browser tab — redirects to `http://localhost:1455/auth/callback`
   instead): completing this flow means the browser shows the user a code
   on that hosted page, which they then have to copy and paste back into
   the terminal running `setup-token`, not something that resolves itself
   in the background.
3. **No new browser process was observed spawned by `setup-token` itself.**
   One `ps -ef` snapshot taken ~12s into the run (while the spinner was
   likely still active) showed no new `firefox`/`chromium`/`brave`/
   `google-chrome`/`xdg-open` process anywhere in this process's lineage —
   only a large, already-running Brave instance on the machine, started
   over 40 minutes earlier for an unrelated reason. This is a single
   snapshot, not continuous monitoring, so treat "no browser" as inferred
   rather than exhaustively ruled out — but the tool's own output agrees:
   it explicitly says `Browser didn't open?` and falls back to the printed
   URL, which is the same "no `DISPLAY` in the env this probe passed"
   situation as gemini's and grok's methods in §13.
4. **Not established** where the token gets written. Neither the captured
   output (killed before reaching that point, by design) nor `claude
   --help`/`claude setup-token --help` names a target file. (That
   `~/.claude/.credentials.json` doesn't exist on this machine was already
   confirmed independently before this probe ran — that's a fact about
   this machine's current state, not something this measurement newly
   established about where a completed run writes to.)
5. **It needs something between kimi and opencode: no menu, but real input
   forwarding.** Unlike kimi (spawn it, read its output, done — nothing
   ever has to be typed back), completing this flow requires sending one
   line of text (the pasted authorization code) into the process's stdin
   after the human visits the URL and copies a code from the browser. A
   client that only spawns and reads output — which is all today's kimi
   treatment does — cannot finish this one; it additionally needs an input
   box wired to the spawned process's stdin. That's a materially smaller
   feature than opencode's full keystroke/arrow-key menu (§14, opencode
   section) — one text field and Enter, not a live TUI — but it is still
   something the "spawn kimi login and just read" design does not have
   today.

**Does the package expose a non-interactive path that would make any of
this unnecessary?** `npx --yes @anthropic-ai/claude-code --help`, checked
before running anything interactive: yes, but not from `setup-token`
itself — the `--bare` flag's own help text states plainly: *"Anthropic auth
is strictly `ANTHROPIC_API_KEY` or `apiKeyHelper` via `--settings` (OAuth
and keychain are never read)."* That's a property of `--bare` mode as a
whole, not a flag on `setup-token` — there is no `setup-token --api-key` or
similar (`setup-token --help` lists only `-h/--help`). If the artist already
has `ANTHROPIC_API_KEY` in their shell environment, the whole
spawn/URL/paste-back dance is beside the point for `--bare`-style
invocations; it just isn't something `setup-token` itself exposes as an
alternative — it's a wholly separate code path in the same package.

### Consequences for the UI (claude setup-token)

1. **This is a third shape, not a repeat of kimi or opencode.** Kimi: spawn,
   read, show link, done. Opencode: cannot be driven from output alone at
   all. Claude `setup-token`: spawn, read, show link — and then also accept
   one line of pasted text and forward it to the process's stdin. Treating
   it as "the kimi treatment" without the input box will get the URL onto
   screen and then hang forever at the paste prompt with no way to finish.
2. **The escape hatch is real, but it's a different code path, not a flag
   on this command.** If the panel wants to skip the whole browser/paste
   flow for artists who already export `ANTHROPIC_API_KEY`, that has to be
   wired as its own thing (`--bare` plus the env var), not as an argument
   to `setup-token`.

### Not established (claude setup-token)

- What the token artifact is or where it's written — not reached, not
  documented in `--help`.
- Whether the paste-back code, once entered, completes immediately or
  itself polls — not reached.
- Whether a real `DISPLAY` would make it open an actual browser window —
  same caveat as gemini/grok in §13, inferred from the env passed and the
  tool's own "Browser didn't open?" message, not observed on a screen.

### Does `setup-token` (and the `npx` fetch that launches it) honour `HTTPS_PROXY`?

The panel has a `proxy_url` setting and threads it into every agent
process; the concern was that spawning `claude setup-token` on a machine
that can't reach the internet directly would just hang before printing
anything, and the panel would be showing a dead button all over again.
Measured on mayfx02, not assumed.

**First, what's actually configured there.** The panel's own
`~/.local/share/houdini-agent-panel/settings.json` has:
```json
{"proxy_url": "http://127.0.0.1:8118", "no_proxy": ""}
```
None of `~/.zshrc`, `~/.bashrc`, `~/.profile`, `~/.bash_profile` export
`HTTP_PROXY`/`HTTPS_PROXY`, and a login shell's own environment has them
unset too (`$SHELL -lc 'echo $HTTPS_PROXY'` → empty) — the panel's own
setting is the *only* place a proxy exists on this machine; nothing would
reach an agent process through the shell environment alone. `ss -tlnp`
confirms something real is listening on `127.0.0.1:8118`.

**One caveat that matters before reading the rest of this: mayfx02 itself
has direct internet access, proxy or not.** `curl -x http://127.0.0.1:8118
https://www.google.com` and the identical `curl` with no `-x` at all both
returned `200`. This machine is not "reaches nothing without a proxy" the
way the owner's is described — so a same-machine "no proxy" run cannot, by
itself, prove the CLI would survive on a machine where direct really is
blocked. What it *can* do, and what was actually tested, is whether the
tooling reads and uses the proxy env vars at all when they're set, versus
silently ignoring them and going direct regardless.

**Ran `npx --yes @anthropic-ai/claude-code setup-token` twice**, pty
attached, fresh `$HOME` each time (forces a real registry fetch both times,
no cache reuse between runs) — once with `HTTPS_PROXY`/`HTTP_PROXY`/lowercase
variants exported to the panel's own `http://127.0.0.1:8118`, once with none
of them set. Both killed (`SIGTERM` to the process group) well before any
code was pasted. A mid-run snapshot (`ss -tnp`, filtered to the run's own
process tree) was taken ~4.3s in, while `npm exec` was still fetching the
package:

- **With the proxy vars set:** 7 established sockets, all
  `127.0.0.1:<random> → 127.0.0.1:8118`, owned by the `npm exec
  @anthr...` process. npm routed its own package fetch through the proxy.
- **Without them:** 7 established sockets instead, all
  `192.168.2.140:<random> → 104.16.{3,5}.34:443` (Cloudflare IPs, almost
  certainly the npm registry's CDN) — npm fell back to a direct connection.
- **Output was identical in both runs**, same shape as the single run
  above, first output byte at ~0.3s in both, same sequence (`Welcome to
  Claude Code v2.1.222` → `Opening browser to sign in…` spinner →
  `Browser didn't open?` fallback → the OAuth URL → `Paste code here if
  prompted >`). Neither run stalled; the URL printed both times.

Answering the four questions directly:

1. **`npx` itself does read and use `HTTPS_PROXY`/`HTTP_PROXY`** — this is
   the one part measured unambiguously: the connection destination flips
   from the proxy's own address to a direct Cloudflare IP depending only on
   whether those env vars were set. It is not ignoring them.
2. **The URL printed without the proxy on this machine** — but that's
   because this machine can also reach the internet directly; this run
   does not distinguish "the CLI doesn't need a proxy" from "the CLI didn't
   need this proxy specifically, because it went around it." Given (1),
   the reasonable expectation for a machine where direct really is blocked
   is that the same env-var-driven routing would carry the fetch through
   the proxy instead of failing — **but that's an extrapolation from (1),
   not something measured on a machine that actually lacks direct access.**
3. **Nothing stalled before the URL, in either condition.** No connection
   activity was observed between the fetch (~4.3s snapshot) and the URL
   appearing seconds later, consistent with the URL being generated purely
   client-side (PKCE `code_challenge` is local math) and needing no network
   call of its own — only sampled once per run, not watched continuously,
   so treat "the URL step itself needs zero network" as inferred from
   timing and the single snapshot, not exhaustively confirmed.
4. **Yes — the `npx --yes` fetch is a real, first network dependency, and
   its failure would look exactly like a login failure.** It opens real
   TCP connections (7, in both runs) before `claude setup-token` has even
   printed its first line (`Welcome to Claude Code…`); if that fetch can't
   complete — wrong proxy, proxy down, direct blocked and no working proxy
   given — the whole spawn dies at that point, and from the panel's
   perspective that's indistinguishable from "the sign-in itself failed"
   unless the panel specifically detects and reports a fetch/connectivity
   failure differently from an auth failure.

### Consequences for the UI (proxy)

1. **The panel already has the right variable to pass — `proxy_url` from
   its own settings — and passing it through as `HTTPS_PROXY`/`HTTP_PROXY`
   (plus lowercase) is confirmed to actually change where `npx` connects,
   not a no-op.** This is the one piece of the owner's concern that's
   fully resolved: the mechanism the panel already has is the correct
   mechanism.
2. **A failed `npx` fetch needs its own error message, distinct from "sign-in
   failed."** Since the fetch is a real network dependency that runs before
   anything login-related even starts, a client that only reports "couldn't
   sign in" on any non-zero exit will misattribute a proxy/connectivity
   problem to the agent's auth flow.
3. **This specific measurement cannot certify the owner's actual machine.**
   mayfx02 has working direct internet access, so "it worked without a
   proxy here" proves nothing about a machine that has none. What
   transfers is narrower and still useful: the CLI's tooling genuinely
   consults `HTTPS_PROXY`/`HTTP_PROXY` rather than ignoring them.

### Not established (proxy)

- Whether the fetch succeeds, over the proxy alone, on a machine with no
  direct route at all — not testable on this machine, which has one.
- Whether `claude setup-token` (the binary itself, after npm's fetch
  completes) makes any of its own proxied network calls beyond what was
  captured in the one mid-fetch snapshot — not watched continuously.
- Whether `no_proxy` (empty in the panel's settings here) has any effect —
  not exercised, since it was empty on this machine.

## 15. Two concurrent `session/prompt` calls on one session — measured, not assumed

The question, ahead of building a per-conversation send queue (the artist
typing and sending again while a turn is still running): does anything in
the SDK or transport stop, queue, or serialize a second `session/prompt`
for a session that already has one in flight? Answered by measurement, not
read off the schema — `session/prompt` is just another JSON-RPC request
with its own id, and JSON-RPC has no inherent concept of "one at a time
per session."

### The measurement

`AcpClient.prompt()` (`client.py`) schedules `do_prompt` as a fire-and-
forget asyncio task on the worker's own event loop (`_submit`/`worker.
submit`) — it does not await the previous call, and nothing in `AcpClient`,
`AcpWorker`, or the vendored `acp` package (`.venv/…/site-packages/acp/
connection.py`) tracks "is there already a prompt outstanding for this
session id." Confirmed live: a real subprocess speaking the real SDK
(`tests/fake_agent.py`, `stream` scenario — the same harness `test_client.py`
uses), one session, two `client.prompt(session_id, blocks)` calls back to
back with no wait for the first's `turn_finished`:

```
('chunk', 'm1', 'echo')
('chunk', 'm1', 'echo')
('turn_finished', 'sess-1', 'end_turn')
('turn_finished', 'sess-1', 'end_turn')
('chunk', 'm1', ': fi')
('chunk', 'm1', ': se')
('chunk', 'm1', 'rst')
('chunk', 'm1', 'cond')
```

Both turns' `agent_message_chunk` updates carried the *same* `message_id`
("m1" — `FakeAgent._prompt_stream` always uses that literal id), so the
client-side model stitched them into ONE entry: reconstructing it from the
chunks above gives `"echoecho: fi: sercond"` — the two replies ("echo:
first" and "echo: second") interleaved character-group by character-group
into a single garbled message. `turn_finished` fired twice back to back,
*before* several of the chunks belonging to either turn had even arrived —
completion and content genuinely raced.

### Consequences for the UI (queueing)

1. **A client that ever sends two `session/prompt` for the same session
   while one is outstanding risks a corrupted transcript, not just a
   confusing one.** This isn't specific to a hypothetical badly-behaved
   real agent — it reproduces against the SDK's own reference fake agent,
   which is about as well-behaved as an agent can be.
2. **A send queue that only ever dispatches the next message after the
   previous one's `turn_finished` (or `error`) arrives sidesteps this
   entirely** — it never depends on knowing how any particular real agent
   (`claude-agent-acp`, `codex-acp`, …) handles concurrent prompts, because
   it never sends one.
3. No agent-side behavior beyond the fake agent was measured — a real
   agent might reject a concurrent prompt outright, hang, or do something
   else not seen here. Irrelevant to the design above only because that
   design never puts it to the test.

### Not established

- What any real agent (as opposed to the SDK's reference fake one) does
  with a concurrent `session/prompt` — not measured, and, per the above,
  not needed for the queueing design that follows from this.

## 16. Where each agent's own credentials actually live on disk — measured, not assumed

The question, ahead of offering sign-in at connect time instead of after a
failed prompt (the owner's report: Claude Agent, "hi", a 1m41s wait, then
told to sign in while already signed in through the desktop app): is there
anything CONCRETE and CHECKABLE that says "this agent already has
credentials configured," so the offer never fires for an artist who is
already signed in? `settings.signed_in_agents` only knows about agents
this ONE install has watched complete a turn — worthless on a fresh
install where the artist has used the CLI directly for months. Checked
directly on maymac01 (macOS — not to be confused with mayfx02, the
separate Linux machine §14 and §18 were measured on), a machine with all
six agents in real, long-term use (`registry.FEATURED_AGENT_IDS`, the six
agent ids the panel lists by name):

| agent id | on disk | env var(s) | measured shape |
|---|---|---|---|
| `claude-acp` | `~/.claude/.credentials.json` | `ANTHROPIC_API_KEY` (already documented, `_NO_METHODS_ADVICE["claude-acp"]`) | **Absent on this machine** despite Claude being in daily use — see the Keychain entry below, which is where this machine's credentials actually are |
| `claude-acp` (macOS only) | Keychain service `"Claude Code-credentials"` | — | Found with `security dump-keychain`: one `"svce"="Claude Code-credentials"` entry, `"acct"="Claude Key"`. `security find-generic-password -s "Claude Code-credentials"` (no `-w`) returns exit 0 in 17ms without any Keychain-access prompt — it looks up the entry, never reads the secret |
| `codex-acp` | `~/.codex/auth.json` | `CODEX_API_KEY`, `OPENAI_API_KEY` (already documented — a signed-out `codex-acp` fails a prompt with exactly "CODEX_API_KEY or OPENAI_API_KEY is not set", §11) | Present, non-empty: top-level keys `auth_mode`, `OPENAI_API_KEY`, `tokens`, `last_refresh` |
| `opencode` | `~/.local/share/opencode/auth.json` | none identified | Present, non-empty: opencode's OWN multi-provider store, keyed by provider name — `{"anthropic": {"type": "oauth", "refresh": ..., "access": ..., "expires": ...}, "kimi-for-coding": {"type": "api", "key": ...}, "openrouter": {...}, "lmstudio": {...}}` on this machine. Any provider present is evidence; there is no single "opencode is signed in" flag, only "opencode has signed into at least one provider" |
| `grok-build` | `~/.grok/auth.json` | `XAI_API_KEY` (x.ai's own documented name, not independently confirmed read by this adapter) | Present, non-empty: keyed by OAuth issuer URL — `{"https://auth.x.ai::<uuid>": {...token data...}}` |
| `gemini` | `~/.gemini/oauth_creds.json` (conventional gemini-cli path) | `GEMINI_API_KEY` (the exact case `shellenv.py`'s own module docstring documents — a real report, the panel couldn't see it, the artist's terminal could), `GOOGLE_CLOUD_PROJECT` (that same report's other half, Vertex/ADC auth) | **Not populated on this machine to confirm the shape** — `~/.gemini/google_accounts.json` shows `"active": null` (an old account remembered, nothing currently active at the OAuth layer). Path checked for existence only, on "a false positive costs nothing" grounds, not confirmed against a real populated file |
| `kimi` | — | `MOONSHOT_API_KEY` (Moonshot AI's own documented name, unconfirmed against this adapter) | `~/.kimi-code/config.toml` DOES have real, non-empty credential-shaped data — `providers.<name>.api_key` — but on this machine that's `providers.openrouter.api_key`: an upstream LLM backend Kimi CLI can route requests through, not confirmed to mean the kimi-acp ADAPTER itself is signed in. §14 documents `kimi login` as a separate device-code OAuth flow; where THAT persists its own token was not identified in this pass. Deliberately not read as a signal — a provider routing key is a different fact from "this agent is authenticated" |

### Consequences for the UI (sign-in offer)

1. **File/Keychain existence and non-emptiness is the bar everywhere here
   — never validity.** A stale or revoked token still counts. That's
   correct for this specific use (deciding whether to show a QUIET,
   dismissible one-line offer, never a refusal to do anything) — the
   agent's own first prompt is still what actually proves a credential
   works, same as `AgentPanel._is_signed_in`'s own reasoning for why a
   session existing is not proof either.
2. **`claude-acp` needs the Keychain check specifically, not just the
   file.** The file-only check would have produced a false "not signed
   in" on the exact machine this was measured on, for an agent in active
   daily use — precisely the wrong-direction mistake the whole feature
   exists to avoid.
3. **`kimi` has nothing reliably checkable.** Its one on-disk value that
   looks like a credential is real but answers a different question
   (which LLM backend, not which account). Falls back entirely to
   `settings.signed_in_agents`, same as any agent with nothing checkable
   at all — this is not a gap the code works around, it's the honest
   result of measuring and finding the available signal ambiguous.

### Not established

- Whether `~/.gemini/oauth_creds.json`, when it exists, actually contains
  what its name implies — only checked for existence, never seen
  populated on a real machine in this pass.
- ~~Where kimi's own device-code OAuth login (§14) actually persists its
  token, if anywhere on disk rather than only in the running process —
  not identified.~~ **Corrected — see §22 below.** `kimi` writes it
  itself, after login, to `~/.kimi/credentials/*.json`.
- Whether `XAI_API_KEY`/`MOONSHOT_API_KEY` are the exact variable names
  `grok-build`/`kimi` read — real, vendor-documented names, not confirmed
  against these specific ACP adapters the way Codex's pair was.

## 17. Is the agent inside the panel the full Claude Code experience, or a reduced one?

The owner's own question, verbatim: "клод код агент, который у нас
работает, он имеет встроенные все системы обычного кли агента — ресерч,
воркфлоус и прочее?" (does the Claude Agent running in the panel have all
the systems a normal CLI agent has — research, workflows, etc.?). Answered
by actually running `claude-agent-acp` 0.64.2 — the same version, the
same launch command (`npx @agentclientprotocol/claude-agent-acp@0.64.2`),
the same real, already-signed-in account this panel uses — not by reading
the adapter's source and reasoning about what the Claude Agent SDK
probably does.

**Method and safety, stated up front:** a throwaway script using the
panel's own `AcpClient` (`client.py`) — the exact class the panel itself
uses, not a hand-rolled JSON-RPC client — spawned the real adapter with
`cwd` set to a throwaway scratch directory (`/tmp/…/claude-capability-
probe`, outside this repo, not committed) — never the owner's home, never
a real project, never `~/.claude*`.
`mcp_servers=[]` was passed deliberately (nothing the panel would normally
inject), specifically to see what shows up on its own. Every probe prompt
was either read-only or wrote only inside that scratch directory (a test
file, an echoed string) — nothing destructive, nothing outside it. Three
turns, real wall-clock time and real account usage, then the session was
closed.

### Tools — from real `tool_call` events, not self-report

| tool (ACP `kind`) | fired for real in this probe | how |
|---|---|---|
| Read (`read`) | yes | asked to read `test.txt`, returned its exact contents |
| Write (`edit`) | yes | created `write-test.txt` with exactly the requested text |
| Bash/Terminal (`execute`) | yes | ran `echo probe-bash-ok`, returned the exact raw output |
| Fetch (`fetch`, built-in `WebFetch`) | yes | fetched `https://example.com`, correctly reported the page title |
| ToolSearch (`other`) | yes | fired on its own, mid-turn, to pull in `WebFetch`/`WebSearch` — the SAME deferred-tool mechanism this very document's own tooling uses |

Self-reported (asked directly, not independently provoked in this probe,
listed here because the ANSWER came with the loaded/deferred tool list
verbatim rather than a vague description): the immediately-loaded set is
`Agent, Bash, Edit, Read, ReportFindings, ScheduleWakeup, Skill,
ToolSearch, Workflow, Write`; deferred (loaded on demand via `ToolSearch`)
includes `CronCreate/Delete/List, DesignSync, EnterPlanMode/Worktree,
ExitPlanMode/Worktree, ListMcpResourcesTool, Monitor, NotebookEdit,
PushNotification, Read/ListMcpResourceTool, RemoteTrigger, SendMessage,
TaskCreate/Get/List/Output/Stop/Update, WebFetch, WebSearch` plus every
tool from every connected MCP server (below).

### MCP — fxhoudini works (already established); it is NOT the only one

The question that mattered: does the session ALSO load the artist's own
MCP servers — the difference between "the panel's agent" and "my agent,
in the panel." `~/.claude.json` (read-only, existence/keys only) has a
real, populated `mcpServers` block on this machine: `puppeteer, houdini,
cognee, mcpbrowser, playwright, browsermcp, browser-use, comfy-pilot,
Parallel-Task-MCP, Parallel-Search-MCP, pointer, iggy-bus, context7,
fxhoudini`. The panel passed `mcp_servers=[]` to this probe session —
deliberately nothing — and the self-report still named, as currently
connected/available, `Parallel-Search-MCP`, `Parallel-Task-MCP`,
`iggy-bus`, and the browser-automation MCPs (`playwright`, `browsermcp`,
`browser-use`), plus named several claude.ai-hosted connectors (`Exa`,
`Gmail`, `Google Calendar`, `Google Drive`, `Mermaid Chart`, `jsoncanvas`)
as configured-but-needing-OAuth-in-an-interactive-session. **None of
these came from the panel.** They come from the artist's own account/
local Claude configuration, loaded by the SDK independently of whatever
`session/new`'s own `mcpServers` argument says.

**This is the answer to the owner's underlying question**, more directly
than the tools list: the panel is not handing the artist a walled-off
agent with only `fxhoudini` wired in. It is their own Claude account,
their own configured MCP servers, plus `fxhoudini` added on top for the
scene. Working in the panel is not "a reduced agent" in this respect —
if anything it is "my agent, in the panel, with one more capability."

### Skills, subagents, hooks, CLAUDE.md

| capability | verified how | result |
|---|---|---|
| **CLAUDE.md** | **run**: a project `CLAUDE.md` in the scratch cwd instructed the agent to prefix its first reply with a specific marker string | the marker appeared, verbatim, as the first thing in the very first reply — loaded and honoured |
| **Hooks** | **run**: a project-level `.claude/settings.json` (scratch cwd only, never `~/.claude/settings.json`) configured a `PreToolUse` hook on `Bash` that appends a marker to a log file | the log file had the marker after the probed `Bash` call — the hook fired for real |
| **Subagents** | self-report only (not independently invoked — no reason to spend a nested agent call on a measurement probe) | reports `Agent` as loaded immediately (not deferred), naming the same subagent roster this very session has (`claude`, `Explore`, `general-purpose`, `Plan`, `statusline-setup`) plus `Workflow` for multi-agent orchestration and the `TaskCreate/Get/List/Output/Stop/Update`/`SendMessage` family for background coordination |
| **Skills** | self-report only (not independently invoked — a project-level probe skill was placed at `.claude/skills/probe-skill/SKILL.md` but no prompt was crafted to force its use, so its loading specifically was not confirmed by a run) | reports `Skill` as loaded immediately, with roughly 90 skills in its own system context, by name, across the same categories this document's own tool listing shows (a marketing pack, `superpowers:*`, `may-hub:*`, and more) |

The self-reported items are not weaker evidence by accident — they are
answers about the agent's own system prompt/context, which is exactly
the kind of thing a model can report accurately (it is reading, not
guessing), but they are still self-report and are labelled as such
rather than folded in with what was independently run and checked.

### Slash commands — the empty list is a build fact, not an auth gate

§11 already measured `claude-acp` returning an empty `availableCommands`
on a NEVER-CONFIGURED machine and left open whether that was "this build
exposes none" or "none until signed in." This probe ran against a fully
authenticated, working account — the same one that opens sessions and
answers prompts normally every day — and `available_commands` was still
`[]` on `session/new`. Every other agent measured in §11/§13 returns its
command list regardless of auth state too (Codex refuses `session/new`
outright when signed out, opencode returns its real list either way), so
"only after auth" was never that agent's actual behavior either — but
this closes it definitively for Claude specifically: **the empty list is
this build/adapter simply never populating `availableCommands`, not a
symptom of being signed out.**

One more real fact from the same `session/new` response, unrelated to the
question asked but worth recording since it was sitting right there:
`available_modes` is NOT empty — six real modes came back: `auto`,
`default` ("Manual"), `acceptEdits`, `plan`, `dontAsk`,
`bypassPermissions`. `AgentInfo.auth_methods` was still `()`, consistent
with §11.

### Consequences for someone deciding panel vs. terminal

1. **Not a reduced agent.** Every capability probed — file tools,
   terminal, web fetch, the artist's own MCP servers, subagents, skills,
   hooks, CLAUDE.md — is present and, where actually run, worked exactly
   as it would from a terminal `claude` session on this same machine.
2. **The one real difference measured is the empty slash-command list**
   (§11, reconfirmed here) — nothing about "research, workflows and the
   rest" is missing, only the panel's own affordance for typing `/command`
   has nothing to autocomplete against for THIS agent specifically (the
   underlying capability — `Workflow`, `Agent`/subagents — is there
   either way, just not reachable through a slash command in this build).
3. **MCP servers configured for the artist's terminal sessions carry into
   the panel automatically**, `fxhoudini` on top. An artist who has spent
   months wiring up their own MCP servers is not starting over inside the
   panel.

### Not established

- Whether GLOBAL-level hooks/skills (`~/.claude/settings.json`,
  `~/.claude/skills/`) also load — deliberately only tested at the
  project level, in the scratch cwd, to honor "do not modify `~/.claude*`."
  Reasonable to expect they would too (nothing in the probe suggests the
  adapter distinguishes scope sources), but not run.
- Whether a subagent (`Agent`/`Task`) or a skill can actually be
  INVOKED end-to-end through this adapter, as opposed to merely being
  present in the loaded tool list and self-reported as available — not
  independently run, to keep this probe to three turns/one session
  rather than an open-ended one.
- The complete MCP tool surface (every tool name from every connected
  server) — the self-report was long and this document only captures
  what was printed within a length cap during the probe run, not a full
  transcript dump.
- Whether the account-level MCP/connector loading measured here is a
  deliberate Claude Agent SDK feature or an artifact of running under the
  same macOS user account and Keychain an interactive `claude` session on
  this machine also uses — not distinguished, and not needed for the
  question actually asked (whether the artist's OWN setup reaches the
  panel — it does, by whatever mechanism).

## 18. The stuck sign-in on mayfx02 — a newer `claude-code` build's second prompt shape

A live failure, not a reproduction: the owner pressed Sign in for Claude
on mayfx02 (the Linux machine also used for §9/§11-14 — NOT maymac01,
§16/§17's machine). The browser reached "You're all set up for Claude
Code. You can now close this window." The panel stayed on "Still working
— this can take a while over a slow connection" indefinitely: no code
field, no error, no completion. `claude setup-token` was still running
four minutes later. Investigated live, over SSH, while the stuck process
was still running — not a later reconstruction.

### What was actually running, measured directly

- `~/.claude/.credentials.json`: absent, confirming the flow genuinely
  never completed (not just a UI display bug).
- The stuck process (`ps`, `/proc/<pid>/fd`): stdin and stdout still open
  as UNCLOSED pipes (state `S`, sleeping) — not exited, not crashed.
  **Zero network sockets open.** It had already finished whatever network
  exchange it needed and was purely blocked on a local read, consistent
  with waiting on stdin for input that was never going to arrive.
- `ptrace` was not available (`ptrace_scope=1`, no passwordless sudo) —
  the live process's exact byte stream could not be captured directly.
  Nothing was written to its stdin and it was never killed.
- The installed `@anthropic-ai/claude-code` package: version **2.1.224**
  (`package.json`, next to the stuck process on disk) — a single compiled
  binary (`bin/claude.exe`, ~282MB), not a package.json/JS tree the way
  earlier versions apparently were: `grep`ing the package's `.cjs`/`.d.ts`
  files for the prompt text this module looks for found nothing at all.
  §14's own measurement did not record which version it ran against, so
  this is not confirmed to be a regression from THAT specific build — only
  confirmed to be a real, current mismatch against THIS one.
- `grep -a` directly on the compiled binary (safe: reads embedded string
  literals, never executes or attaches to anything) found the ORIGINAL
  prompt string, "Paste code here if prompted > ", verbatim, still
  present — and a SECOND, different prompt sitting right next to it in
  the binary's own strings: **"Or paste the redirect URL here: "**,
  next to `no_tty_stdin`, `"stdin isn't a terminal, so authentication
  can't be completed here"`, and `"Re-run in an interactive terminal
  (e.g. `ssh -t`) and paste the redirect URL when prompted."` in the same
  neighbourhood of the binary's minified source. This module has always
  given the child a plain, non-tty pipe (`subprocess.Popen(stdin=PIPE)`)
  — exactly the condition those neighbouring strings are about.
- A separate, independent, bounded (~20-48s, always terminated after)
  test run of the SAME installed binary — never touching the live stuck
  process — produced a real, observed network connection to Anthropic's
  own infrastructure during its OAuth exchange, confirming the network
  path itself works on this machine; it did not, within that bounded
  window, get far enough to observe either prompt string directly.

### Consequences for the fix

1. **Ruled out**: "this flow variant completes server-side and never asks
   for a code" (the second hypothesis offered before measuring). The
   process was still alive, still blocked on a local read, with no
   network activity — that is not what a completed, exited flow looks
   like.
2. **Best-supported reading**: a newer build's non-tty-stdin path uses
   different prompt text than what `_INPUT_PROMPT_MARKER` (singular, at
   the time) recognised, so `input_requested` never fired and the code
   field never appeared — while the OAuth URL/browser flow completed
   independently of what this module was managing to parse from stdout.
   Not fully closed: the exact JS control flow deciding WHICH prompt a
   given run gets was not traced through the minified bundle, and the
   live process's own byte-for-byte output was never captured (ptrace
   blocked, and reading its pipe directly without a consumer would have
   risked stealing bytes from whatever, if anything, was still going to
   read them — not done).
3. **Fixed as `ui/terminal_login.py` now does**: both prompt strings are
   recognised (`_INPUT_PROMPT_MARKERS`), ANSI is stripped before any
   match is attempted (a build willing to reformat for a non-tty stdin is
   equally free to colour it), and `\r` now flushes a line the same way
   `\n` already did — a status line redrawn via carriage return used to
   sit invisibly in an ever-growing buffer until, if ever, a literal `\n`
   arrived.
4. **Logging added** (the report's other, independent half): "not a
   single line about the terminal login" is no longer true —
   `ui/terminal_login.py` now logs the spawned command, every line
   received (redacted past 24 token-shaped characters), when a prompt is
   detected, and the exit code. The NEXT time this happens, the log
   settles which hypothesis was true instead of requiring a live SSH
   session and a compiled-binary `grep`.
5. **The UI no longer waits forever with nothing to do**: a second,
   longer timer (`_TERMINAL_LOGIN_STUCK_MS`, `ui/panel.py`) says so
   explicitly and names the manual fallback command if nothing conclusive
   (a real prompt, or the process ending) has happened in a while — the
   Cancel button was already there and already worked; it had nothing
   pointing at it before now.

### A separate, unrelated finding from the same machine

`panel.log` on mayfx02 logs, on every panel start: `fxhoudinimcp_server is
unreachable from inside the process (the plugin isn't loaded or is out of
date) — scanning 8100..8115 over HTTP; this may find SOMEONE ELSE's
Houdini instead of this one` (`scene.py`'s own documented fallback,
§ architecture.md §4). Confirmed reproducible: present at all three panel
starts captured in this investigation (12:14, 12:21, 12:36). Not
investigated further here — flagged, not fixed, and not the subject of
this section's own fix.

### Not established

- The exact JS logic in the 2.1.224 bundle that chooses which of the two
  prompt strings a given run prints, or under exactly which condition
  (non-tty stdin was the strongest neighbouring signal in the binary's
  own strings, not a traced code path).
- Byte-for-byte what the ORIGINAL live stuck process actually printed —
  only an independent, later run of the same binary was observed, not
  that exact process.
- Whether the fxhoudinimcp plugin issue above is related to this one in
  any way (nothing found connecting them) or purely coincidental to both
  showing up in the same log on the same machine.

---

## 19. The bundled Claude binary works for `setup-token` — and where it actually is

The owner's real blocker on mayfx02 was not §18's stuck prompt but the
manual fallback itself: `npx --yes @anthropic-ai/claude-code setup-token`
downloads its own ~282 MB single-file binary, and that machine's direct
link measured ~21 KB per 60s — not slow, never finishing. Asking an
artist to install the Claude CLI separately (the first fix considered) was
rejected by the owner: "either do it for everyone, or solve it without
the CLI."

The fix that shipped instead: `claude-agent-acp` (the ACP adapter this
panel drives) already bundles the real Claude CLI, to run the agent
itself, through `@anthropic-ai/claude-agent-sdk-<platform>` — a SEPARATE
npm package from the standalone `@anthropic-ai/claude-code` the manual
advice already used. Once ANY conversation with claude-acp has ever
started on a machine, that platform binary is already sitting in npx's
own cache, at no extra cost.

### The bundled binary, confirmed live (mayfx02, real run)

`timeout 15 script -qec '<path>/claude setup-token' out.log` (a real PTY
was required — piping stdin from `/dev/null` produced NO output at all,
even after 15s; the CLI appears to detect a non-interactive terminal and
either behaves differently or blocks silently before printing anything).
Killed before completing — no code was ever pasted. Output, byte for
byte what an artist would see:

```
Welcome to Claude Code v2.1.220
Opening browser to sign in…
Browser didn't open? Use the url below to sign in (c to copy)
https://claude.com/cai/oauth/authorize?...
Paste code here if prompted >
```

Identical shape to every other measurement of `setup-token` in this
document (§14, §18): the same OAuth URL format, the same input-prompt
marker `ui/terminal_login.py` already recognises. No new parsing needed.

### Finding it reliably — what's stable, what isn't

npx keys its own cache by content hash, not by package name — measured
on two real machines, not assumed:

- mayfx02 (Linux): two different hash directories existed side by side,
  `.../_npx/539edbc7afd0f13d/.../claude-agent-sdk-linux-x64/claude` and
  `.../_npx/becf7b9e49303068/.../claude-code-linux-x64/claude` (the
  LATTER is the standalone package, from an earlier manual attempt —
  confirms the two packages really are separate, separately cached).
- This Mac: THREE different hash directories all contained
  `@anthropic-ai/claude-agent-sdk-darwin-arm64/claude` — stale entries
  from earlier resolutions (a panel update, a fresh `claude-agent-acp`
  launch each re-resolving the dependency tree). All three, independently
  confirmed runnable (`--version` succeeded on every one).

So the hash is never guessable and must never be hardcoded. What IS
stable, confirmed on both machines: the cache ROOT itself — `npm config
get cache` answered `~/.npm` on both (Linux and macOS); the platform
suffix on the package directory name matches Node's own `(platform,
arch)` naming exactly (`darwin-arm64`, `linux-x64` — the same values
`node.py::node_platform()` already computes for nodejs.org's own
archives, so no second mapping table was needed); and the relative
layout inside a hash directory (`node_modules/@anthropic-ai/claude-agent-
sdk-<platform>/claude`) was identical on both.

`node.find_cached_npx_binary(scope, name_prefix, binary_name)` is the
result: globs every hash directory under the (measured, not guessed)
cache root, matches by name PREFIX within a given scope (not the full
platform-suffixed name — no need to compute the suffix at all, since a
wrong-architecture binary simply fails the next step and gets skipped),
and verifies every candidate by actually running `--version` before
trusting it — same discipline as `mcp_runtime.find()`'s own search for
the fx server's interpreter, for the same reason: a path existing proves
nothing (a half-finished download, a stale wrong-arch leftover). Ties
(more than one candidate runs) broken by mtime, newest first.

**Cost, measured, not assumed**: the full search-and-verify took ~1.7s on
this Mac (3 candidates, each independently run and checked) — far too
slow for the main thread that builds the `AuthMethod`/`TerminalAuth`
`_builtin_terminal_auth_method` returns (this project's own "Houdini is
never blocked" rule). Deferred to `TerminalLoginWorker`'s own thread via
an optional `resolve_command` callable, run once `work()` has already
started — `_builtin_terminal_auth_method` still returns the npx fallback
as a synchronous placeholder, overwritten (and reported back via a new
`command_resolved` signal, so the panel's own "run it yourself" fallback
text names what's actually running) the moment the real answer is ready.

### Command preference, and why this order

1. `claude` on PATH — skips npx entirely, and measured (§14) to be the
   only one of the three where nothing has to happen before the CLI
   prints its first byte.
2. The bundled binary above — no network at all once found; the ~1.7s
   local search-and-verify cost is nothing next to a fetch that a bad
   link may never complete.
3. `npx --yes @anthropic-ai/claude-code setup-token` — the last resort,
   kept because a machine that has never run claude-acp at all has
   nothing bundled to find yet, and this is the only one of the three
   that still works there. The one that can look exactly like a hang on
   a bad connection, which is the entire reason for the other two.

## 20. The bundled binary needs a real pty — plain pipes get zero output

§19's bundled-binary fix reached mayfx02 and hit a second, different live
failure — the owner completed the browser step and the panel never moved:

```
17:42:30  terminal login: spawning .../claude-agent-sdk-linux-x64/claude setup-token
(nothing after — no output line, no prompt, no exit)
```

§19 itself already contains the clue, easy to read past the first time:
getting ANY output out of the bundled binary required `script -qec`, and
piping stdin from `/dev/null` gave zero output even after 15s. That's not
"prints slowly" — it's "detects a non-interactive terminal and refuses to
print anything at all." `TerminalLoginWorker` gives its child plain pipes
(`subprocess.Popen(..., stdin=PIPE, stdout=PIPE)`), so the bundled binary
stays silent by design, and the panel waits forever for a prompt that will
never come. This also explains why the OLD npx-wrapped path (§14, §18)
behaved differently under the same plain-pipe spawn: it printed the
non-tty variant instead ("Or paste the redirect URL here" — found next to
the string `no_tty_stdin` inside the binary), i.e. it detects the same
condition and CHOOSES to speak anyway. The bundled binary does not.

### Confirming it's a pty requirement, not something else — A/B on mayfx02

Same binary, same `setup-token` argument, two spawns differing only in
stdio: `pty.openpty()` + `os.fork()`/`os.execv()` with the slave fd wired
to stdin/stdout/stderr, versus plain `os.pipe()`. The pipe spawn produced
nothing in 12s. The pty spawn produced real, immediate output — confirming
the requirement is specifically a controlling terminal, not a timing or
buffering difference.

### The fix: `pty.openpty()` instead of plain pipes, scoped to this one path

`TerminalLoginWorker` gained a keyword-only `use_pty: bool = False`
parameter (default preserves every existing caller's behaviour — see
Kimi below). When true, `work()` calls `pty.openpty()`, spawns via
`subprocess.Popen(stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
start_new_session=True)` (kept as `Popen`, not a raw `fork`/`exec`, so
`.pid`/`.wait()`/`.terminate()`/`.poll()` all still work the way the rest
of the class already relies on), closes the parent's copy of the slave fd,
and reads from the master fd through a small `_PtyMasterReader` adapter
giving it the same `.read(1) -> str` shape `process.stdout.read(1)`
already had for the pipe path — the rest of the read loop doesn't need to
know which one it's talking to.

Two POSIX quirks `_PtyMasterReader` exists to handle, both measured, not
assumed:

- `os.read()` on a pty master fd raises `OSError` (EIO) once the slave
  side closes — a real, documented POSIX behaviour, NOT the clean `b""`
  EOF a pipe gives. Treated as EOF explicitly; anything else re-raises.
- Multi-byte UTF-8 characters (the spinner glyphs below) can land split
  across two separate `os.read()` chunks — `text=True` on `Popen` handles
  this transparently for the pipe path, but reading raw bytes off a pty
  master fd doesn't get that for free. Fixed with
  `codecs.getincrementaldecoder("utf-8")(errors="replace")` rather than a
  naive per-chunk `.decode()`.

`panel.py`'s `_start_terminal_login` sets `use_pty = method.id ==
"claude-setup-token"` — scoped to exactly this one method, the same way
`resolve_command` is already scoped there (§19). Kimi's own `kimi login`
was re-measured directly (not inferred from the ACP-channel probe in §13,
which is a different subprocess on a different channel) and already
produces real output over plain pipes — unaffected either way, so its
path is left exactly as it was.

### Parsing under a real pty needed three fixes plain-pipe testing never surfaced

All three found and fixed by capturing real raw bytes from a live pty run
of `setup-token` on mayfx02 and feeding them through the actual parsing
functions before touching anything — not synthesized test data.

1. **The OAuth URL regex was consuming past its own terminator.** A real
   pty stream wraps the URL in an OSC 8 terminal hyperlink: `\x1b]8;id=X;
   <url>\x07<display text>\x1b]8;;\x07`. The old `https?://\S+` pattern
   is greedy across `\S`, which includes `\x07` (BEL) and `\x1b` (ESC) —
   it read straight through the terminator into the OSC-8 "display text"
   half (itself a truncated repeat of the same URL), producing a garbled
   link. Fixed by excluding `\x07`/`\x1b` from the character class on
   both `_URL_RE` and `_BARE_URL_RE`; verified clean against the same
   captured bytes.

2. **The input-prompt marker never matched.** A real capture of `Paste
   code here if prompted >` arrived, after ANSI stripping, as
   `"Pastecodehereifprompted>"` — zero spaces. The build simulates the
   visual spacing with cursor-absolute-positioning escapes (`\x1b[<N>G`,
   "move to column N"), not literal space characters; stripping the
   escapes (necessary to read anything at all) throws the spacing away
   with them. `"paste code here" in text.lower()` — the existing
   substring check — can never fire against that. Fixed with a new
   `_marker_in(marker, text)` helper that squeezes ALL whitespace out of
   both sides before comparing; verified it matches the real captured
   line.

3. **`_ANSI_RE` left two escape shapes unstripped.** `\x1b[>0q` (a device-
   attributes query this build sends on startup) uses a `>` prefix the
   old CSI parameter class (`[0-9;?]`) didn't include; `\x1b7`/`\x1b8`
   (save/restore cursor) are a different, bracket-less 2-byte escape
   family, not `\x1b[...` at all. Both confirmed present, unstripped, in
   the raw capture. Fixed by widening the regex to `r"\x1b\[[0-9;?>]*
   [ -/]*[@-~]|\x1b[78]"`.

A fourth issue was cosmetic, not a parsing bug, but worth filtering for
the same reason §18 already filters other noise: this build's "thinking"
spinner cycles a single glyph (`✢ * ✶ ✻ ✽ ✻ ✶ * ✢ ·` measured, one frame
per `\r`-redraw) that would otherwise flash through the artist's own
status field and the log as dozens of near-identical single-character
lines. `_is_spinner_noise()` drops a line that is exactly one non-word
character after stripping — verified against all six glyphs actually
seen, and confirmed it does NOT fire on any real word (the shortest,
`"a"`, still passes).

`_FORCE_FLUSH_CHARS` (§18) was checked against the real line lengths
this produces, not just assumed adequate: the OSC-8-wrapped URL line,
escape sequence and repeated display text included, measured 448
characters — only 52 below the previous 500-char threshold, and that
length tracks variable OAuth query params (`state`, `code_challenge`,
`client_id`) a future build could easily push past, silently splitting
the escape sequence mid-flush. Raised to 2000 for real headroom.

### Writing the pasted code back through a pty is safe — measured, not assumed

Two concerns, both settled by writing a fake code (never a real one) back
into a live pty session and watching what the parent process reads back:

- **Does the child's own terminal echo leak the pasted text into what we
  read?** No — this build masks its own input, printing one `*` per
  character rather than the literal text, whether the kernel's own ECHO
  flag is left on or explicitly disabled on the slave fd before spawn.
  The parent process reading the master fd never sees the actual code at
  all, only its length reflected in asterisk count. `send_line()`'s own
  docstring records this so nobody adds redaction logic for something
  that was never there to redact.
- **Does writing to the master fd work the same way `process.stdin.write`
  already does for the pipe path?** Yes, mechanically — `os.write(master_
  fd, text.encode("utf-8"))`, wrapped in `contextlib.suppress(OSError)`
  for the same reason the pipe path already tolerates a already-exited
  child. `send_line()` branches on `self._use_pty`; either path still
  only logs the character COUNT, never the content, as before.

### Windows: the pipe path stays, on purpose, not by accidental fallthrough

`pty` and `termios` don't exist on Windows at all — a guarded import sets
`_PTY_AVAILABLE = False` there, and `self._use_pty = use_pty and
_PTY_AVAILABLE` forces the pipe path regardless of what a caller (i.e.
`panel.py`'s `claude-setup-token` scoping) requests. This is a stated
constraint, not a silent gap: no Windows machine exists in this project
(same gap already noted elsewhere in this document) to measure what a
Windows build of the bundled binary actually needs — a real controlling
terminal probably means something different there (`ConPTY`, not POSIX
`pty`), and that measurement is future work, not something to guess at
here. Until then, `setup-token` on Windows stays exactly as silent as it
was before this section — no worse, not fixed either.

### A second live report: the browser page shows no code at all

Owner correction, direct observation on his own machine, not recollection:
the browser page this build sends the artist to says only that the window
can be closed — no code, nothing to copy, nothing to paste. §14's own
measured prompt text — "Paste code here **if prompted**" — already said
this in the wording; the paste-back step this whole module was built
around is conditional, not universal. A flow that completes entirely on
the server side never prints anything for a human to act on, so a panel
waiting only for `input_requested` before it considers anything
"actionable" can be waiting for something that will never come.

**Established without completing anyone's login** — a live, real process
on mayfx02 made this checkable directly, no browser ever visited:

- The owner's OWN already-stuck process (pid 3748010, spawned by the
  pre-fix, plain-pipe build) was watched, read-only, for over a minute:
  `ss -tnp` and `/proc/<pid>/fd` never showed a single network connection,
  fd count never changed (steady at 9: two pipes, an eventpoll, an
  eventfd, `/dev/urandom`, its own `/proc/<pid>/statm`), `/proc/<pid>/
  wchan` sat on `do_epoll_wait` throughout. `~/.claude/.credentials.json`
  does not exist. Plain pipes don't just suppress the CLI's OUTPUT (§20's
  main finding) — this process never even opened a connection to try to
  complete anything. It is not "waiting for a code that will never come";
  it is doing nothing at all, network included.
- A SEPARATE, fresh `setup-token` attempt (own new OAuth state, never the
  owner's own flow) spawned under a real pty, taken to the same prompt,
  then only WATCHED — the URL was never opened in a browser, nothing was
  ever written back. Within ~10 seconds of reaching the prompt, `ss -tnp`
  showed a genuine outbound HTTPS connection (port 443) to the CLI's own
  backend, held **ESTABLISHED continuously for the full 45-second
  observation window** — a long-poll/SSE-shaped wait, not a one-shot
  request. Killed at the end of the window; nothing was ever typed, no
  browser tab was ever opened.

Read together: this build has a real, working background channel for
completing the exchange without any typed code — exactly the shape the
"no code at all" report describes — and it only opens under a real
controlling terminal, same root cause as everything else in this section.
The pty fix already shipped here should let that channel do its job. What
it does NOT establish (deliberately not pushed further, per instructions:
completing this measurement for real would mean finishing someone's actual
sign-in, the owner's call to make, not ours) is what the CLI prints or
exits with once that channel actually reports success — only that it is
genuinely trying, actively, the whole time it sits at the prompt.

### The fix: check for completion, not only for a prompt

`_on_terminal_login_exited` (`ui/panel.py`) used to report every exit the
same uninformative way once a URL had been shown — "Terminal login process
ended (exit N)" — deliberately treating the exit code as neither success
nor failure (a completed ACP turn remained, and remains, the one signal
the rest of the file trusts, `_remember_signed_in`). That was fine when a
paste-back step existed to reassure the artist something was progressing;
it leaves nothing at all to look at for the no-code variant above.

Now, on a clean exit (`exit_code == 0`) specifically, it checks
`signin_evidence.has_credential_evidence` — the SAME check `_maybe_offer_
sign_in` already uses at connect time, not a second definition of "signed
in" — and reports "Signed in." when it finds something. Gated on a clean
exit so a cancelled attempt on a machine that happens to already have
older, unrelated credentials doesn't get a false "Signed in." A non-zero
exit, or a zero exit with nothing found, keeps the original neutral
message unchanged.

## 21. `claude setup-token` does not sign anything in — it mints a token

Decisive evidence, from a real, completed run on mayfx02 (the owner's own
sign-in, exit code 0) — overturning the model §14 and §20 were both built
on. The panel's own log, redaction intact:

```
terminal login line:  Your OAuth token (valid for 1 year):
terminal login line:  <79 chars redacted>
terminal login line:  <29 chars redacted>
terminal login line: <29 chars redacted>'tbeabletoseeitagain.
terminal login line: Usethistokenbysetting:<29 chars redacted>=<token>
terminal login: exited, code=0
```

**Confirmed from the real bundled binary's own string table** (`strings`
on the exact binary `_resolve_claude_terminal_command` finds — no process
run, no login attempted), not guessed:

```
Your OAuth token (valid for 1 year):
Store this token securely. You won't be able to see it again.
Use this token by setting: export CLAUDE_CODE_OAUTH_TOKEN=<token>
```

This exactly reconciles the redacted log above, including the two
different redaction lengths, which is worth spelling out because it
confirms the pty-squeezed-whitespace mechanism (§20) is STILL exactly
what's happening here, not a new one: `"Storethistokensecurely.Youwon"`
(no apostrophe — `'` isn't in `_LOOKS_LIKE_A_TOKEN_RE`'s character class,
so the match stops right there) is exactly 29 characters, leaving the
literal tail `'tbeabletoseeitagain.` unredacted — and `"export"` (6) +
`"CLAUDE_CODE_OAUTH_TOKEN"` (23), glued together with no space between
them the same way "Paste code here" lost its own spacing, is exactly 29
characters too.

**So three things this project believed were wrong:**

1. `claude setup-token` does not write `~/.claude/.credentials.json` —
   that file is what an interactive, desktop-app-style `claude login`
   writes (the earlier, wrong assumption baked into `signin_evidence.py`'s
   own comment, now corrected). `setup-token` writes NOTHING to disk. It
   prints the token once, says plainly it cannot be shown again, and
   exits. Waiting for a credentials file to appear (`_on_terminal_login_
   exited`'s own "Signed in." check, §20) could never succeed for this
   command specifically — confirmed directly: `~/.claude/` on the machine
   that just completed a real run contains only `.last-cleanup` and
   `backups/`, no credentials file, after a successful exit.
2. The "browser just says close this window, no code" shape (the report
   that opened §20's own investigation) is real, and now fully explained:
   the browser side completes the OAUTH EXCHANGE, not a login — the token
   itself only ever appears in the CLI's own terminal output, never in the
   browser.
3. **The redaction that hides the token from the log is correct and was
   never the bug** — the bug is that nothing ELSE ever captured it. An
   owner completed a real sign-in and the panel gave him nothing: the
   token existed for exactly as long as that one process's stdout did,
   then was gone.

### The variable is `CLAUDE_CODE_OAUTH_TOKEN` — and it is NOT `ANTHROPIC_API_KEY`

Owner correction: this token is tied to his Claude subscription (Pro/Max)
— a completely different wallet from `ANTHROPIC_API_KEY`, which bills per
token against a separate Anthropic Console/API account. The binary's own
strings confirm the panel had been quietly capable of steering someone
the wrong way: the interactive login flow's own React state machine
(same binary, same `strings` pass) labels the two paths itself —

```
"Login method pre-selected: Subscription Plan (Claude Pro/Max)"
"Login method pre-selected: API usage billing (Anthropic Console)"
```

`n==="setup-token"` sets the SAME internal flag (`P`) that `m==="claudeai"`
does in that state machine — i.e. `setup-token` IS the Subscription Plan
path, definitively, not a shorthand for either. `_no_methods_advice`,
`_builtin_terminal_auth_method`'s own description and `_AUTH_ADVICE` all
used to frame `ANTHROPIC_API_KEY` as simply "the simpler alternative" —
true only for someone who wants API billing; actively wrong advice for a
subscriber, who would find out at the end of the month. Rewritten to name
both mechanisms, using the CLI's own two labels above, without ranking
either as "easier."

### The fix: capture the token where it's printed, use it where the agent starts

`TerminalLoginWorker` gained a `token_captured` signal, firing when a line
matches `CLAUDE_CODE_OAUTH_TOKEN=` (anchored on the confirmed, literal
variable name — NOT a generic `KEY=VALUE` line parser: under a real pty
the "export" prefix and the variable name arrive glued together with no
space, and a generic parser would capture `"exportCLAUDE_CODE_OAUTH_
TOKEN"` as the "variable name", which is not anything real). Once this
build's own token-dump label is seen, every subsequent line is ALSO
redacted before reaching `line_received`, not only the log — the one
place in this module where the live signal and the log are no longer the
same decision, because unlike a device code or a URL, this really is a
usable secret with nothing for the artist to read it FOR.

Stored in `settings.agent_oauth_tokens` (`{agent_id: {env_var: token}}`)
— the same trust level `proxy_url`/`ca_bundle` already carry in the same
file — and injected into that agent's own launch environment by
`runtime.py::launch_spec` (`_with_oauth_tokens`, mirroring `_with_proxy`'s
own pattern exactly) the NEXT time it starts. Not retroactive: an already-
running `claude-acp` process keeps whatever env it already had — the
panel does not restart a live agent out from under the artist to apply
this. `signin_evidence._claude` now also recognises a captured, stored
token as evidence of being signed in, closing the loop with §20's own
"Signed in." exit check — without this, that check could never fire true
for a `setup-token` completion, since no credentials file and no shell
env var exists until the panel supplies one itself.

### The other five agents: not established, said plainly rather than guessed

Asked to check whether Codex, Gemini, Grok and Kimi have the same "print
a token, store it yourself" shape, or the same subscription-vs-API-key
split. What IS already measured (§11, §12): `codex-acp` advertises TWO
methods, `api-key` (reads `CODEX_API_KEY`/`OPENAI_API_KEY` — the same
per-token-billing shape as `ANTHROPIC_API_KEY`) and `chat-gpt` (browser
OAuth, `authenticate()` simply never returns until the browser step
finishes — §12's own finding). That is at least the SAME two-wallet
shape in outline. What is NOT established, for any of the four: whether
their own OAuth flow writes a credentials file, prints a token once, or
something else entirely — none of them were run under a pty for this
specific question, and guessing would repeat the exact mistake this
section exists to correct. Left for a follow-up pass with the same
discipline used here: read the real binary, or capture a real (non-
destructive) run, before writing anything about what they do.

### Independent confirmation: a second, unrelated project agrees

Everything above this subsection came from one source: reading the
bundled binary's own string table and a real completed run. A second,
independent source — the owner's own separate project `~/Github/LLMux`
(repo `MAY4VFX/LLMux`, a proxy in front of provider accounts, unrelated
codebase to this panel) — reaches the same conclusion from its own,
different vantage point. Its `docs/authentication.md` states, verbatim:

> "Use the `python cli.py --setup-token` helper to mint a long-lived
> (365-day) token after authenticating Anthropic once. The proxy saves
> the token and prints it for reuse on other machines."
>
> "Short-lived OAuth tokens include refresh tokens, so the proxy renews
> them automatically during normal operation."

This lines up exactly with what the binary's string table already showed
("Your OAuth token (valid for 1 year)"): `setup-token` mints one specific
kind of token — long-lived, manually reissued by running the command
again — as opposed to the short-lived, refresh-token-bearing session
LLMux's own browser-based OAuth exchange produces. Two independent
readings of the same distinction is why this project's conclusion stands:
**there is no refresh flow for `CLAUDE_CODE_OAUTH_TOKEN` for the panel to
implement.** A stale one isn't silently renewed — the artist reissues it
by running `setup-token` again, same as LLMux's own tip says.

**A boundary worth drawing, stated neutrally, not as an evaluation of
either approach:** LLMux calls `api.anthropic.com/v1/messages` directly,
itself, with `Authorization: Bearer <subscription token>` and the beta
header `oauth-2025-04-20` — it IS the client making the API request. This
panel does not do that: it launches the official `@agentclientprotocol/
claude-agent-acp` package from the official ACP registry and hands that
package the token via an environment variable — the package itself is
what talks to Anthropic. Two different integration shapes, both real,
neither this section takes a position on beyond describing them.

## 22. Where `kimi`'s own OAuth token actually lives on disk — the §16 gap closed

§16 measured that `kimi` had "nothing reliably checkable" beyond the
unconfirmed `MOONSHOT_API_KEY`/`KIMI_API_KEY` env vars, and left open
where `kimi login`'s own device-code OAuth flow (§14) persists its token,
if anywhere. Closed here.

Source: DeepWiki's read of `MoonshotAI/kimi-cli`, section "OAuth and
Authentication" — after a successful device-code login, the `kimi` CLI
itself writes its tokens to `~/.kimi/credentials/*.json` (mode 0o600,
atomic write).

Confirmed against a real, in-use machine (maymac01, not mayfx02 —
`kimi` was never installed on mayfx02): `~/.kimi/credentials/kimi-code.json`
exists, mode 0o600, a JSON object with keys `access_token`, `refresh_token`,
`expires_at`, `scope`, `token_type`, `expires_in`. The DeepWiki description
matched the real file on this machine exactly — path, permissions, and the
"one file per login" shape (the directory holds exactly this one file).

`signin_evidence._kimi` now checks this directory (globbing `*.json` rather
than pinning the single observed filename, since nothing establishes that
name as the CLI's only output) on the same "existence and non-emptiness,
never validity" bar every other check in that module already holds to —
see `has_credential_evidence` and `docs/design.md`'s own reasoning for why
a stale token still counts as evidence here. Previously, an artist signed
into `kimi` through the browser got no "Signed in." confirmation from the
panel at all, because `has_credential_evidence("kimi", ...)` had nothing
to check but two env vars a device-code login never sets.

**Not established:** whether `kimi-code.json` is the only filename the CLI
ever writes there (e.g. a differently-named file for a different login
mode) — the glob is deliberately not narrowed to the one observed name.

## 23. The decision on `claude-acp`'s login path, and why it isn't being reopened

This project's own issue #41 records the same reasoning about Claude
sign-in being redone five times in one week, because it was never written
down anywhere durable. This section is that write-down.

### What is measured

- The panel takes its `claude-acp` agent from the live ACP registry
  (`cdn.agentclientprotocol.com/registry/v1/latest/registry.json`).
  Checked 2026-08-07: 38 agents in the registry, exactly one Claude entry
  — `claude-acp`, resolving to npx package
  `@agentclientprotocol/claude-agent-acp@0.66.0`. There is no
  `claude-code-acp` entry in the registry.
- That package is an ACP adapter built on top of the **Claude Agent
  SDK**, not the Claude Code CLI. Confirmed on a live, running process: it
  launches `@anthropic-ai/claude-agent-sdk-<platform>/claude`.
- Anthropic's own documentation, `code.claude.com/docs/en/agent-sdk/overview`,
  states verbatim (confirmed by loading the page directly, 2026-08-07):
  "Unless previously approved, Anthropic does not allow third party
  developers to offer claude.ai login or rate limits for their products,
  including agents built on the Claude Agent SDK. Use the API key
  authentication methods described in the Quickstart instead."
- The same page's Branding guidelines section permits the name "Claude
  Agent" for menus and UI, and disallows "Claude Code" for the same use.
  The panel's own UI says "Claude Agent" — consistent with that guidance.
- Secondary context, explicitly NOT a primary source, kept only for
  completeness: a February 2026 press statement by an Anthropic engineer
  about third-party harnesses, and an April 2026 announcement that
  Pro/Max subscriptions do not cover third-party harnesses.

### The owner's decision

The login path stays as it is. Reasoning: the `@agentclientprotocol/
claude-agent-acp` package is published by the ACP organization itself,
publicly, and is used by other clients (Zed). The panel does not proxy or
resell anything — it runs on the artist's own machine, under the artist's
own subscription, using the artist's own token.

### The known asymmetry — recorded honestly, not smoothed over

The "unless previously approved" wording describes an approval granted to
a specific partner, not a property of the package itself. Whatever
approval Zed may have does not, by that text, transfer to anyone else who
installs the same package. This is a risk the owner has chosen to accept,
not a permission that has been demonstrated to exist for this project.

### A second public precedent, and what it reveals about billing

Checked 2026-08-07 by reading the source directly, not by reputation:
`NousResearch/hermes-agent` — a public agent from a well-known lab —
accepts the very same credential this panel captures.

- `plugins/model-providers/anthropic/__init__.py`:
  `env_vars=("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN")`
- `hermes_cli/runtime_provider.py` tells its own users, verbatim:
  `"run 'claude setup-token', or authenticate with 'claude /login'."`
- `agent/anthropic_adapter.py` recognises the `cc-` token prefix as
  "Claude Code OAuth access tokens (from CLAUDE_CODE_OAUTH_TOKEN)".

The more useful find is in that project's own provider documentation
(`website/docs/integrations/providers.md`), which describes its Anthropic
support as:

> "Claude Max + **extra usage credits** via OAuth; also supports Anthropic
> API key or manual setup-token"

That phrase resolves what the February/April 2026 statements actually
changed. The subscription login is not being refused; what changed is
that a third-party harness's consumption is not covered by the flat
Pro/Max subscription and bills through separately purchased extra usage
instead. Two independent sources agree on this: the April 2026
announcement, and a shipping product that documents exactly that billing
shape to its own users.

> **Superseded — billing only. See §29.** The paragraph above is wrong as
> of 2026-08-11: the mechanism it describes was announced and paused on
> the same day (2026-06-15) and has not returned, so a Pro/Max
> subscription used here still draws from that subscription's ordinary
> limits — confirmed in practice by the owner, on their own account.
> Nothing about billing goes in the sign-in UI on our initiative; the
> owner did not ask for it and does not want it (`9674fdf` reverts the
> one attempt). §23's other conclusion — the login path itself — is
> untouched by this.

This narrows the asymmetry above without erasing it. The permission
question remains as written. The **billing** question, however, is now
settled enough to act on, and it is the one that actually reaches an
artist's wallet: signing in with a subscription may spend metered extra
usage rather than the flat plan. Issue #41 already set the standard here
— silently moving a subscriber onto metered billing is not acceptable —
so that fact belongs in front of the artist at the sign-in step, stated
as a fact about money, not as a warning about rules.

## 24. Windows sign-in via ConPTY — implemented, not yet run on Windows

§20's own Windows note ended with "no Windows machine exists in this
project... that measurement is future work". This section is that work,
still without a Windows machine to run it on — the owner chose to ship
it anyway, on the strength of Microsoft's own documented Win32 sequence,
because testers with real Windows machines are available going forward
and the diagnostic logging below is built specifically so a tester's
`panel.log` is enough to tell a maintainer what happened, without remote
access to the tester's machine.

### What's implemented

- `ui/_conpty_windows.py` (new module, stdlib-only — no `pywinpty`, same
  "don't add a runtime dependency for one platform's one auth flow"
  reasoning `node.py` already applies to not bundling Node): `ctypes` +
  `kernel32.dll` bindings for the documented ConPTY sequence —
  `CreatePipe` x2 → `CreatePseudoConsole` → `STARTUPINFOEXW` with
  `PROC_THREAD_ATTRIBUTE_PSEUDOCONSOLE` via `InitializeProcThreadAttribu
  teList`/`UpdateProcThreadAttribute` → `CreateProcessW` with
  `EXTENDED_STARTUPINFO_PRESENT`. `ConPtyProcess` wraps the result behind
  the same `.pid`/`.poll()`/`.wait()`/`.terminate()` shape `subprocess.
  Popen` already gives the POSIX paths; `ConPtyReader` gives the same
  `.read(1) -> str` contract `_PtyMasterReader` gives the POSIX pty path,
  including the same incremental-UTF-8-decode treatment (never
  independently confirmed necessary on Windows, unlike its POSIX
  sibling — the cost of keeping it is the same few lines either way).
- `ui/terminal_login.py`: a new `_CONPTY_AVAILABLE` flag, kept
  deliberately SEPARATE from `_PTY_AVAILABLE` (one is POSIX-only, one is
  Windows-only, and conflating their names risks a future reader
  assuming they're interchangeable — exactly the assumption that let a
  silent pipe fallback ship once already). `TerminalLoginWorker` gained
  `self._use_conpty` alongside `self._use_pty`; `work()` gained a third
  spawn branch selected by it. `send_line()` gained a third write
  destination (`ConPtyProcess.write`). `panel.py`'s `use_pty = method.id
  == "claude-setup-token"` line is UNCHANGED — its meaning on Windows is
  now "use ConPTY" rather than "no terminal at all", exactly as planned,
  with no code change needed there.
- **No silent fallback.** This is the one requirement that could not be
  compromised on: a Windows machine with `use_pty` requested but
  `_CONPTY_AVAILABLE == False` (pre-1809, or a broken kernel32) raises a
  `RuntimeError` naming the requirement (Windows 10 1809+) instead of
  quietly running plain pipes — the exact trap §20 already found and
  fixed once on the POSIX side (a bundled binary that prints NOTHING at
  all over plain pipes). `Worker.run()` turns that into a `failed`
  signal `AgentPanel._on_terminal_login_failed` already knows how to
  show, appending the "run it yourself" fallback command it always has.
  Every step inside `_conpty_windows.spawn()` follows the same rule: the
  first failing Win32 call raises a `ConPtyError` naming that exact step
  and its `GetLastError()`/`HRESULT` code, with every handle opened so
  far closed before raising — nothing is left half-built or leaked.

### Diagnostic logging — built for a tester the maintainer cannot reach

Every step logs through the SAME `logbook.py` mechanism the rest of the
panel uses (`_log = logbook.logger("houdini_agent_panel.ui.conpty_windows")`
in the new module, alongside the existing `"houdini_agent_panel.ui.
terminal_login"` logger) — both are child loggers of the package logger
`logbook.setup()` attaches its rotating file handler to, so nothing
extra was needed to make these lines land in `panel.log`:

- Windows build (`sys.getwindowsversion()`, e.g. `10.0.22631`) and
  whether `CreatePseudoConsole` was found in `kernel32` at all
  (`conpty_available()`).
- Both `CreatePipe` calls (as one "pipes created" line — the two are
  never independently interesting to a reader).
- `CreatePseudoConsole`'s `HRESULT`, and the console size requested.
- `InitializeProcThreadAttributeList`/`UpdateProcThreadAttribute`
  success, or the specific one that failed with its `GetLastError()`.
- `CreateProcessW`'s pid on success, or `GetLastError()` plus the
  command that was attempted on failure.
- The first output chunk ever read (`ConPtyReader`, byte count plus a
  REDACTED preview) — the single most useful line for telling "ConPTY
  came up and the child is printing something" from "spawned fine, dead
  silence", which is exactly the failure mode plain pipes produced on
  POSIX before §20's fix.
- Everything downstream of the read loop was ALREADY platform-neutral
  and needed no changes: input-prompt detection, OAuth token capture,
  and exit code are logged by the same lines `terminal_login.py` already
  had for the POSIX paths (`"terminal login: input prompt detected"`,
  `"terminal login: OAuth token captured (...)"`, `"terminal login:
  exited, code=..."`).

**Redaction is not new or weakened here.** `_conpty_windows.py` cannot
import `terminal_login.py`'s `_redact_for_log`/`_LOOKS_LIKE_A_TOKEN_RE`
(the two modules would import each other — `terminal_login.py` already
imports `_conpty_windows`), so a small, self-contained copy lives in
`_conpty_windows.py` too, same shape, same reasoning `bugreport.py`'s
own docstring gives for porting rather than importing its sibling
service's redaction list. Every raw-content log line in the new module
(the first-chunk preview) goes through it before being written.

### Bug report integration — already works, nothing new needed

`ui/panel.py`'s `_open_bug_report` already calls `bugreport.read_log_tail
(logbook.log_path())`, reading the SAME rotating `panel.log` file both
`terminal_login.py` and `_conpty_windows.py` log into (confirmed directly:
`tests/test_logbook.py::test_conpty_diagnostics_land_in_the_same_log_
bugreport_reads`). A tester pressing the in-panel bug report button
after a failed Windows sign-in attempt gets the ConPTY diagnostic lines
in the report body automatically, redacted the same way everything else
in that flow already is (`bugreport.redact_secrets`, a second,
independent pass over whatever's in the editable field). The 60-line
default tail (`bugreport._LOG_TAIL_MAX_LINES`) may not fit the ENTIRE
ConPTY step sequence plus surrounding context on a long attempt — for a
maintainer who needs the full picture, the raw `panel.log` file itself
(path shown in the panel's own diagnostics/pypanel, `logbook.log_path()`)
can be attached by hand; this was not changed, since the existing
constant already serves every other flow the same way.

### Tests — what they prove, and what they cannot

- `tests/test_conpty_windows.py` (37 tests): exercises `_conpty_windows.
  spawn()`/`conpty_available()`/`ConPtyProcess`/`ConPtyReader` against an
  injected FAKE `kernel32` object (`_FakeKernel32`, plain Python, no
  `ctypes.WinDLL` involved) — every call site in `spawn()` uses `ctypes.
  pointer(x)` rather than `ctypes.byref(x)` specifically so a pure-Python
  fake function can write through it (`byref` is a call-only proxy;
  `pointer` supports `ptr[0] = value` from ordinary Python). Also
  discovered and fixed along the way: `ctypes.get_last_error()`/`ctypes.
  set_last_error()` do not exist at all outside Windows (confirmed:
  `AttributeError` on macOS) — the module calls `kernel32.GetLastError()`
  explicitly instead, which is itself dependency-injectable exactly like
  every other kernel32 function used here, and is a legitimate,
  independently-correct way to read the Win32 last-error value (used by
  ctypes-based Windows tooling before `use_last_error=True` existed).
  These tests prove the WRAPPING logic — call order, argument shapes,
  error propagation and handle cleanup on every documented failure step.
  They do NOT prove a real `kernel32.dll` accepts these exact calls.
- `tests/test_terminal_login_worker_windows.py` (8 tests): one layer up
  — `TerminalLoginWorker.__init__`'s flag selection (`_use_pty`/`_use_
  conpty` are mutually exclusive, `_use_conpty` only true when POSIX pty
  genuinely isn't available), the "ConPTY unavailable raises a clear
  failure, never falls back to pipes" requirement, and an end-to-end run
  of `work()`'s ConPTY branch against a scripted fake process
  (`_ScriptedConPtyProcess`) — URL parsing, input-prompt detection,
  `send_line` round-tripping, token capture/redaction, `stop()`
  terminating the process, and `close()` being called on exit. Achieved
  by monkeypatching `terminal_login_mod._PTY_AVAILABLE = False` and
  `.platform.system` to return `"Windows"` (this machine's own real
  `_PTY_AVAILABLE` is `True` — it's POSIX — so this is what stands in
  for "no POSIX pty here"), plus replacing `terminal_login_mod._conpty_
  windows` with a fake module whose `.spawn()` returns the scripted
  process (`.ConPtyReader`/`.ConPtyError` are the REAL classes — no
  reason to fake them, they only need a `.read(n) -> bytes` object).
  These tests prove `TerminalLoginWorker`'s OWN logic is correct given a
  ConPTY-shaped process. They do NOT prove `_conpty_windows.spawn()`
  itself works against a real Windows `kernel32.dll`.
- `tests/test_logbook.py::test_conpty_diagnostics_land_in_the_same_log_
  bugreport_reads`: confirms the bug-report integration claim above for
  real rather than by inspection alone.

### Not established — no Windows machine in this project

- Whether the documented ConPTY sequence actually succeeds against a
  real `kernel32.dll` at all — every structure layout, constant, and
  call order follows Microsoft's own C sample, but none of it has ever
  executed on Windows.
- Whether the bundled `claude` binary genuinely needs a controlling
  terminal on Windows the same way §20 measured for Linux/macOS (the
  working assumption this whole section is built on — plausible, since
  the binary is cross-platform, but not measured on this platform).
- Whether the child's own input masking (§20's own POSIX finding: this
  build prints one `*` per character rather than echoing the real text)
  behaves identically through a ConPTY as through a POSIX pty.
- Whether a Windows console's default code page / narrator / IME
  interacts with `CREATE_UNICODE_ENVIRONMENT` or the ConPTY's own
  handling of the command line in any way not covered by `subprocess.
  list2cmdline`'s own (POSIX-tested-only, here) quoting.
- The exact wording/order the Windows build of `claude setup-token`
  prints — assumed identical to Linux/macOS per §19 ("identical shape to
  every other measurement... no new parsing needed"), never confirmed
  for Windows specifically.

### What a tester's `panel.log` needs to show for each of the above to move from "not established" to "verified"

1. **ConPTY comes up at all**: `conpty: CreatePseudoConsole ok, size=...`
   followed by `conpty: proc thread attribute list initialised` and
   `conpty: CreateProcessW ok, pid=...`, with no `ConPtyError` in
   between. Absence + a specific failing step (`CreatePipe`/`CreatePseudo
   Console`/`InitializeProcThreadAttributeList`/`UpdateProcThreadAttribu
   te`/`CreateProcessW`) plus its `GetLastError()`/HRESULT tells a
   maintainer exactly where the documented sequence breaks on real
   Windows, without needing the machine.
2. **The binary actually needs a terminal (§20's assumption, ported)**:
   `conpty: first output chunk (N bytes): ...` appearing SOON after
   `CreateProcessW ok` — if this line never appears at all despite a
   clean spawn, that's the Windows analogue of §20's original "zero
   output over plain pipes" finding, just under ConPTY instead, and
   would mean the assumption this section is built on doesn't hold.
3. **Input masking parity**: not directly loggable (the panel
   deliberately never logs the artist's typed code — see `send_line`'s
   own docstring) — this one can only be confirmed by a tester's own
   report of what they SAW after pasting a code (garbled text? asterisks?
   nothing at all?), not by the log alone.
4. **Command-line/quoting correctness**: `terminal login: spawning ...`
   (the existing, platform-neutral line) shows what was ASKED for;
   `conpty: CreateProcessW ok, pid=...` with no error confirms Windows
   accepted the quoted command line `_build_environment_block`/
   `list2cmdline` produced. A `ConPtyError` at `CreateProcessW` with
   `GetLastError=123` (`ERROR_INVALID_NAME`) or `2`
   (`ERROR_FILE_NOT_FOUND`) would point straight at this.
5. **Output shape matches §19's assumption**: `terminal login: input
   prompt detected` firing (from the SAME marker logic §18/§20 already
   established) is the confirmation — if ConPTY comes up cleanly, output
   arrives, but this line never fires, that means the Windows build's
   prompt text differs from what's already recognised, the same class of
   mismatch §18 found once on Linux.

A single successful run showing all of items 1, 2 and 5 in `panel.log`,
plus the tester confirming they could read/paste a code normally (item
3) and that a real sign-in completed (`terminal login: OAuth token
captured (...)` or `Signed in.` in the feed), would close every "not
established" item above except the general "never independently run
against a real kernel32.dll" caveat, which by definition stops applying
once one has.

## 25. The pty had no width, so the token arrived cut in half

Measured on the owner's Linux machine (mayfx02, 2026-08-08), on 0.8.10 —
after §21's capture bug was already fixed and the token was, for the
first time, actually being stored.

Sign-in succeeded. `terminal login: OAuth token captured` appeared in the
log, `settings.agent_oauth_tokens` held a token, the panel returned to
the transcript. The first prompt came back:

```
Failed to authenticate. API Error: 401 OAuth access token is invalid.
```

The log says why, in two lines that had been sitting there since the
first captured run:

```
terminal login line:  <79 chars redacted>
terminal login line:  <29 chars redacted>
```

79 + 29 = 108, and a leading space plus 79 characters is exactly 80.
`pty.openpty()` returns a terminal with **no size set**, which an
Ink-based build reads as the 80-column fallback and hard-wraps to. The
minted token is 108 characters; the panel captured the first line and
stored it as the whole thing.

Nothing in the output marks a line as continued — no trailing backslash,
no indent, no escape. From the reader's side a wrapped token and a
complete one are indistinguishable, so no downstream parser can repair
this. It has to be prevented at the source: `_set_pty_size` now sets
`TIOCSWINSZ` on the slave fd to `_PTY_COLUMNS` (1000) before the child
is spawned, and the Windows path passes the same width to
`CreatePseudoConsole` instead of relying on its own separate default of
120 — two independent defaults drifting apart is how one platform ends
up quietly truncating a secret while the other doesn't.

Worth noting what this cost: the failure reported as a *successful*
sign-in. The panel said "Signed in.", the evidence check passed (a token
was present), and the only symptom reached the artist one prompt later
as an authentication error with nothing pointing back at sign-in. §21's
own rule — that storing a wrong value is worse than storing none —
applies to a truncated value exactly as much as to a placeholder.

Still open, and now clearly urgent rather than theoretical: the panel
checks that a token EXISTS, never that it WORKS. A 401 from the agent is
the one signal that distinguishes them, and nothing currently listens
for it.

## 26. Corrected: a frame-diffing screen, not an incomplete CSI — the first read of this measurement was wrong

Measured on mayfx02, 2026-08-08, by the diagnostic added in 0.8.12 —
after §25's wrapping fix had already made the token arrive on one line.

Sign-in succeeded, a 107-character token was stored, and the agent's
first prompt returned `401 Invalid bearer token`. Two requests with the
same token through the machine's own proxy settled what was wrong:

| token | API response |
| --- | --- |
| as stored, 107 chars, `sk-ant-at01-` | `authentication_error: Invalid bearer token` |
| with one `o` re-inserted, 108 chars, `sk-ant-oat01-` | `rate_limit_error` |

A rate-limit error is returned *after* authentication succeeds, so the
token was correct and the panel was corrupting it — that part still
stands. What follows it originally did not.

### What this section used to say, and why it was wrong

The original version of this section read `_shape_for_log`'s output as
`'\x1b[1C\x1b[<9>\x1b[<103>'` and, reasoning from the two masked lengths
alone, concluded the build had emitted an incomplete `\x1b[10` (parameters,
no final byte) immediately before the token, and that `_ANSI_RE`'s CSI
final-byte class — the full standard `[@-~]` range — had terminated that
incomplete sequence on the token's own `"o"` (`sk-ant-oat01-o…` read as
`\x1b[10o`, syntactically a valid CSI) and discarded it. The fix shipped
on that reasoning restricted `_ANSI_RE`'s final-byte class to a hand-picked
set of "assigned" CSI finals (`_CSI_FINAL`, 0.8.12-0.8.13).

The reasoning had a real gap: `_SHAPE_HEAD` (the run's first few
characters, shown alongside its length) was added in a diagnostic commit
that landed only AFTER the incomplete-CSI fix had already shipped on the
strength of the two-integer reconstruction alone — the tool that could
have told an incomplete CSI apart from a complete one by *content*, not
just by arithmetic, did not exist yet when that conclusion was drawn.
Re-measuring with the run heads in place, on a fresh capture, gives a
different, unambiguous shape:

```
'\x1b[1C\x1b[<9:2Bsk…>\x1b[<103:10Ga…>'
```

Decoded: `\x1b[1C` (cursor forward 1), `\x1b[2B` (cursor DOWN 2 rows —
not part of the original reconstruction at all), then `sk-ant-`, then a
**complete, well-formed** `\x1b[10G` (move to column 10 — final byte `G`,
present and correct), then text starting with `a` (`at01-…`). There is no
incomplete CSI anywhere in the real capture. `o` is not eaten by a
regex; it simply never appears in this particular frame's own bytes.

### The real cause

The bundled binary is Ink-based and repaints its output as a **diff
between frames**: each redraw sends only the runs of text that changed,
moving the cursor between them with absolute/relative position escapes
rather than resending everything. `\x1b[2B` then `\x1b[10G` after writing
`sk-ant-` (7 characters, ending at column 9) jumps straight to column 10
— column 9 is simply never touched by *this* frame's own bytes, because
an **earlier** frame already painted the token's `"o"` there and this
frame's diff had no reason to repeat a cell that didn't change.

`TerminalLoginWorker.work`'s read loop only ever handed `_token_value_in`
the text of one flush's own buffer (`_strip_ansi(buffer)`, reset to `""`
after every `\r`/`\n`) — a plain concatenation of whatever characters
arrived between two separators. That has no way to remember a character
painted by a frame several flushes earlier. No regex over a single
buffer, however carefully its final-byte class is tuned, can recover a
character that was never in that buffer's own bytes at all — which is
exactly why the 0.8.12-0.8.13 restriction changed nothing real: it
fixed a bug that had never actually happened.

### The fix

`_TerminalScreen` (`python/houdini_agent_panel/ui/terminal_login.py`) is
a small, persistent model of cursor position and screen cells, fed the
same character stream the read loop already reads, in parallel with the
existing buffer-based line detection — never reset between flushes. When
a line is flushed, `_token_value_in` reads the token candidate from the
screen model's row (the text of the row the cursor was on when the flush
happened), not from that flush's own buffer. A character several frames
old is still sitting in its cell, because the model never forgets it.
`_ANSI_RE`'s final-byte class was reverted to the plain standard range —
see its own comment for why the restriction bought nothing and the
`_CSI_FINAL` class was removed entirely.

Three different mechanisms have now corrupted this one secret in a day:
the wrong anchor (§21), line wrapping (§25), and this one — a screen
diff, not a regex. All three reported the failure as a *successful*
sign-in, because the panel checks that a token EXISTS and never that it
WORKS. §27 is that check.

## 27. Checking that a token WORKS, not just that one exists

Three different faults have now shipped a token that was structurally
plausible and completely unusable — the wrong anchor (§21), truncation by
line wrapping (§25), and a character eaten by an escape sequence (§26).
All three were announced to the artist as a **successful sign-in**,
because the only question the panel ever asked was whether a token
existed.

The owner's own account is what settled the design:

> "the time before last, I checked the chat before signing in and it
>  worked, and after signing in it broke"

That is the real cost, and it is worse than a failed sign-in. Capture
overwrote `settings.agent_oauth_tokens` unconditionally, so an artist
holding a working credential who signs in again — the obvious thing to
try when something looks wrong — destroyed the one good token they had.

### The check, measured

`GET https://api.anthropic.com/v1/models` with `Authorization: Bearer
<token>` and `anthropic-beta: oauth-2025-04-20`, from the owner's machine
through its own proxy, 2026-08-08:

| token | response |
| --- | --- |
| whole, 108 characters | HTTP 200, the model list |
| one character short (§26) | HTTP 401 `authentication_error` |

`/v1/models` is the right question: it authenticates exactly as a prompt
does and invokes no model, so a check on every sign-in costs the artist
nothing. `/v1/messages` would have billed them for it.

### What each outcome does

- **200 → store.** The token works.
- **401 → do not store, and say so.** Whatever was already there is left
  untouched. This is the whole point: a broken capture can no longer
  destroy a working credential.
- **Anything else → store, unverified.** Offline, proxy down, timeout,
  and also 403 `Request not allowed` — that last one is an answer about
  the *request*, not the token, and the API only produces it after
  reading the credential successfully. Being unable to ask is never a
  reason to throw a token away; an artist with no connection still
  deserves the one they just minted.

The check runs on the worker thread after the child process has finished
printing, so a slow network can neither stall the read loop nor touch the
UI thread.

A note on the test suite: `conftest`'s `no_real_network` guard could not
see this, because `token_check.verify` builds its own request (it needs
headers `urlopen_fetch` cannot carry). It is stubbed there too — without
that, the next sign-in test written would quietly call the real API.

## 28. Enter is a carriage return, and the panel had never sent one

Measured on the owner's machine, 2026-08-08. He pasted a code into the
sign-in field, the child echoed a row of `*` — one per character, its own
input masking — and then nothing happened, ever. The process was still
alive an hour later.

The log makes the cause unmissable once you look for it:

```
$ grep -c "artist input submitted" panel.log
1
```

**One.** In the entire history of that machine, across every successful
sign-in, the artist had never typed anything into this field. Claude's
`setup-token` completes through the browser on its own — the prompt it
prints says "Paste code here **if** prompted", and until now the "if" had
never been true. So the submit path had never once been exercised, and it
was broken from the day it was written.

`send_line` terminated the text with `\n`. A keyboard has no line-feed
key: pressing Enter transmits `\r`, and this build — as this module's own
docstring already recorded — "puts the pty into raw mode itself once it
reaches an actual input prompt". Raw mode is precisely the mode with no
`ICRNL` translation, so the `\n` arrived as a line feed and was never a
submit. The characters landed (hence the echo); the Enter never did.

Now `\r` on both terminal paths. A pty in canonical mode still has
`ICRNL` on and turns it into the `\n` a cooperative reader expects, so
nothing that worked before stops working. Only the plain-pipe path, which
has no line discipline at all, still needs a literal `\n`.

Worth naming the shape of this one: it is the fourth fault in this single
flow that shipped because the code was measured against a stand-in rather
than the real build. The stand-in read `sys.stdin.readline()`, which is
canonical-mode and perfectly happy with `\n`. The test passed for months
and proved nothing about the program it was standing in for.

## 29. §23's billing conclusion, updated — the change it relied on was paused before it shipped

Checked 2026-08-11, for issue #41's remaining "billing line in the
sign-in UI" item, before writing any UI copy — §23 already concluded one
should exist ("that fact belongs in front of the artist at the sign-in
step"), but its own sourcing ("secondary context, explicitly NOT a
primary source": a February 2026 press statement and an April 2026
announcement) turned out to be stale by the time it was written, not
just weak. A directly relevant, more authoritative, more recent source
existed and wasn't found: Anthropic's own support article,
`support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-
your-claude-plan`, page itself dated "Last Updated: June 16, 2026" —
eight days before §23's own "checked 2026-08-07".

**What that page says, verbatim:** Anthropic proposed, for June 15,
2026, exactly the mechanism §23 concluded was already true — "Claude
Agent SDK and `claude -p` usage no longer counts toward your Claude
plan's usage limits" — but then paused it on the day it was due to take
effect: **"Update June 15: We're pausing the changes to Claude Agent SDK
usage described below."** And, load-bearing for this project specifically:
**"For now, nothing has changed: Claude Agent SDK, `claude -p`, and
third-party app usage still draw from your subscription's usage
limits."** The promised monthly credit ("Agent SDK credit": $20 Pro,
$100 Max 5x, $200 Max 20x) explicitly "isn't available" as a result.
Anthropic says it is "working to update the plan" and will "share
updates before anything takes effect" — no relaunch date given.

Corroborated independently by press covering the same pause, not just
the original announcement: Zed's own blog (`zed.dev/blog/anthropic-
subscription-changes` — Zed ships another ACP client in the same
position this panel is in, and states the June 15 mechanism would have
applied to "Claude Code through ACP, in Zed or anywhere else," i.e. this
project too, had it shipped), TechCrunch, VentureBeat, and The New
Stack. One secondary search summary claimed the change "went live July
10" — traced back to no actual article content on re-fetch (the page
returned only navigation chrome, no article body), contradicted by every
source that DID yield real content including Anthropic's own, and not
trusted here for exactly the reason this section exists: an unverified
claim is not a fact because it appears confident.

**What this means for #41's checklist item:** the specific claim briefed
("a subscription used through a third-party client like this one spends
metered extra usage instead of the flat subscription rate") is not
something the best currently-available evidence supports as true RIGHT
NOW — the mechanism that would have made it true was announced, then
explicitly paused before taking effect, with no confirmed date since.
§23's own reasoning for surfacing this in the UI at all — "silently
moving a subscriber onto metered billing is not acceptable" — still
holds if and when Anthropic actually ships something like it; it just
was not, as of this check, currently true to state as settled fact in
front of an artist about to sign in.

Not established: whether the April 2026 OpenClaw-specific enforcement
(cutting that one product's Pro/Max coverage, ahead of and separate from
the May/June "Agent SDK credit" proposal for everyone) is still in
effect for OpenClaw specifically, or whether it generalizes to any
third-party harness independent of the paused mechanism above — the
sources found describe it as a targeted action against one named
product, not a general policy statement with its own text to quote.
Anthropic's billing stance here has changed more than once in a few
months; treat anything written here as dated the moment it's read, and
re-check the support article directly (not secondary coverage of it)
before relying on this again.

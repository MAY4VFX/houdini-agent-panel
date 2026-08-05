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

"""A real, minimal ACP agent for `AcpClient` tests.

The only honest way to test the panel's protocol layer is to talk to a real
`acp.run_agent`, not a mock: JSON-RPC serialization, pydantic discriminators,
message ordering — all of that is easy to break with a stub without
noticing. So this file runs as a separate process
(`sys.executable tests/fake_agent.py`), like a regular ACP agent over stdio.

Behavior is selected via the ``FAKE_AGENT_SCENARIO`` environment variable:

- ``stream`` (default) — a normal multi-chunk streamed reply.
- ``auth`` — `prompt` raises `auth_required` until `authenticate` is called;
  also supports `logout` — verifies the "login required -> logged in ->
  logged out -> login required again" cycle (issue #6).
- ``permission`` — asks for permission before replying, echoes the chosen option.
- ``modes`` — offers `availableModes`/`currentModeId`, listens for `set_session_mode`.
- ``plan`` — sends a plan and `tool_call`/`tool_call_update` before replying.
- ``slow`` — hangs in `prompt` until a `session/cancel` arrives (for the cancel test).
- ``load`` — declares `loadSession: true`; `session/load` replays one
  remembered exchange as ordinary `session_update` notifications (per the
  ACP spec, agentclientprotocol.com/protocol/session-setup: the Agent MUST
  replay the whole conversation before answering `session/load`) and then
  answers; a `prompt` after that continues the SAME session id.
- ``load-fail`` — declares `loadSession: true`, but `session/load` always
  errors with `resource_not_found` — the "the agent said yes and then
  couldn't" case a real restart can produce.
- ``load-slow`` — declares `loadSession: true`; `session/load` replays two
  chunks with real delays between them and before answering, wide enough a
  window to observe replay-vs-response ordering for real rather than
  assume it from protocol wording.
- ``steer`` — advertises `_meta.steering.supported` on `initialize` (the
  extension `claude-agent-acp` carries, docs/facts/acp-sdk.md §31) and
  implements `_session/steering` for real: while a `prompt` is in flight,
  `ext_method("session/steering", ...)` records the injected text and lets
  the prompt continue with a reply built from it, returning `{"outcome":
  "injected"}`; with nothing in flight it answers `{"outcome":
  "promptRequired", "reason": "noRunningTurn"}` when opted in, matching the
  real adapter's own two documented outcomes for this codebase's client.
"""

from __future__ import annotations

import asyncio
import json
import os

import acp
from acp.agent.connection import AgentSideConnection
from acp.exceptions import RequestError
from acp.helpers import plan_entry, start_tool_call, text_block, update_plan, update_tool_call
from acp.meta import AGENT_METHODS
from acp.schema import (
    AgentAuthCapabilities,
    AgentCapabilities,
    AgentMessageChunk,
    AgentThoughtChunk,
    AuthMethodAgent,
    CurrentModeUpdate,
    Implementation,
    LogoutCapabilities,
    LogoutRequest,
    PermissionOption,
    PromptCapabilities,
    SessionMode,
    SessionModeState,
    ToolCallUpdate,
)
from acp.stdio import stdio_streams

SCENARIO = os.environ.get("FAKE_AGENT_SCENARIO", "stream")

_AUTH_METHOD_ID = "apikey"


def _message_chunk(text: str, message_id: str) -> AgentMessageChunk:
    return AgentMessageChunk(
        session_update="agent_message_chunk", content=text_block(text), message_id=message_id
    )


def _thought_chunk(text: str, message_id: str) -> AgentThoughtChunk:
    return AgentThoughtChunk(
        session_update="agent_thought_chunk", content=text_block(text), message_id=message_id
    )


def _split(text: str, parts: int) -> list[str]:
    size = max(1, -(-len(text) // parts))  # ceiling division, so we don't lose the tail
    return [text[i : i + size] for i in range(0, len(text), size)] or [text]


class FakeAgent:
    """Implements `acp.interfaces.Agent` via duck typing (a protocol, not an ABC)."""

    def __init__(self) -> None:
        self._client = None  # filled in on_connect with the real Client proxy
        self._authenticated = SCENARIO != "auth"
        self._sessions: dict[str, str | None] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._session_counter = 0
        # ``steer`` scenario only — see `_prompt_steer`/`ext_method`.
        self._turns_in_flight: set[str] = set()
        self._steer_events: dict[str, asyncio.Event] = {}
        self._steer_injected: dict[str, str] = {}

    # --- ACP Agent protocol --------------------------------------------

    def on_connect(self, conn) -> None:
        self._client = conn

    async def initialize(
        self, protocol_version, client_capabilities=None, client_info=None, **kwargs
    ):
        auth_methods = []
        auth_caps = None
        if SCENARIO == "auth":
            auth_methods = [
                AuthMethodAgent(
                    id=_AUTH_METHOD_ID, name="API Key", description="test login method"
                )
            ]
            # LogoutCapabilities() is an empty but non-None object: that's
            # exactly how an agent declares "I support logout" (see
            # client.py::_agent_info_from and docs/architecture.md §6 —
            # None here would mean "I don't support it").
            auth_caps = AgentAuthCapabilities(logout=LogoutCapabilities())
        prompt_caps = PromptCapabilities(image=True, audio=False, embedded_context=True)
        return acp.InitializeResponse(
            protocol_version=acp.PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(
                load_session=SCENARIO in ("load", "load-fail", "load-slow"),
                prompt_capabilities=prompt_caps,
                auth=auth_caps,
            ),
            auth_methods=auth_methods,
            agent_info=Implementation(name="fake-agent", version="0.0.1"),
            # Top-level `_meta`, sibling of `agent_capabilities` — exactly
            # where `claude-agent-acp` advertises it (docs/facts/acp-sdk.md
            # §31), not part of the typed schema.
            field_meta={"steering": {"supported": True}} if SCENARIO == "steer" else None,
        )

    async def authenticate(self, method_id, **kwargs):
        if SCENARIO == "auth" and method_id == _AUTH_METHOD_ID:
            self._authenticated = True
        return None

    async def logout(self, **kwargs) -> None:
        """The agent logically returns to the "login required" state — exactly
        what `AcpClient.do_logout` expects after a successful call."""
        self._authenticated = False

    async def new_session(self, cwd, additional_directories=None, mcp_servers=None, **kwargs):
        self._session_counter += 1
        session_id = f"sess-{self._session_counter}"
        # `**kwargs` here is exactly `_meta` unpacked (acp/router.py:104-107:
        # `params.update(meta)`) — the ONLY way a test can see what actually
        # crossed the wire, as opposed to what the client THOUGHT it sent.
        # Folded into the session_id, which every scenario already returns
        # and every test already reads, rather than a new signal: appended
        # only when kwargs is non-empty, so no scenario that never sends any
        # (the vast majority) changes shape.
        if kwargs:
            session_id += f"|meta={json.dumps(kwargs, sort_keys=True)}"
        modes = None
        if SCENARIO in ("modes", "modes-no-echo"):
            modes = SessionModeState(
                current_mode_id="ask",
                available_modes=[
                    SessionMode(id="ask", name="Ask"),
                    SessionMode(id="code", name="Code"),
                ],
            )
        self._sessions[session_id] = "ask" if modes else None
        return acp.NewSessionResponse(session_id=session_id, modes=modes)

    async def load_session(
        self, cwd, session_id, mcp_servers=None, additional_directories=None, **kwargs
    ):
        """`session/load` — only reachable when `SCENARIO` declared
        `loadSession` in `initialize`. Per the ACP spec, the whole
        conversation is replayed as `session_update` notifications BEFORE
        this ever answers, so the client can rebuild the exact same
        transcript a live turn would have produced.

        ``load-slow`` is the same replay, deliberately spread out with real
        `asyncio.sleep`s between chunks and before the response — wide
        enough a window for a test to observe whether replay notifications
        are genuinely visible before `session_loaded` fires, not just
        assumed to be from protocol wording alone.
        """
        if SCENARIO == "load-fail":
            raise RequestError.resource_not_found(session_id)
        self._sessions[session_id] = None
        if SCENARIO == "load-slow":
            await asyncio.sleep(0.2)
            await self._client.session_update(
                session_id=session_id,
                update=_message_chunk("earlier: rotor pyro setup", "replay-1"),
            )
            await asyncio.sleep(0.2)
            await self._client.session_update(
                session_id=session_id,
                update=_message_chunk("earlier: second reply", "replay-2"),
            )
            await asyncio.sleep(0.2)
            return acp.LoadSessionResponse()
        await self._client.session_update(
            session_id=session_id, update=_message_chunk("earlier: rotor pyro setup", "replay-1")
        )
        if kwargs:
            # Same reasoning as `new_session` above — `session/load` has no
            # spare response field to fold this into (`LoadSessionResponse`
            # only carries `modes`/`config_options`), so an extra chunk in
            # the replay it already sends is the observable this test double
            # has. Only emitted when kwargs is non-empty, same as above.
            await self._client.session_update(
                session_id=session_id,
                update=_message_chunk(f"meta={json.dumps(kwargs, sort_keys=True)}", "replay-meta"),
            )
        return acp.LoadSessionResponse()

    async def set_session_mode(self, session_id, mode_id, **kwargs):
        self._sessions[session_id] = mode_id
        if self._client is not None and SCENARIO != "modes-no-echo":
            update = CurrentModeUpdate(
                session_update="current_mode_update", current_mode_id=mode_id
            )
            await self._client.session_update(session_id=session_id, update=update)
        return acp.SetSessionModeResponse()

    async def cancel(self, session_id, **kwargs) -> None:
        event = self._cancel_events.get(session_id)
        if event is not None:
            event.set()

    async def prompt(self, session_id, prompt, **kwargs):
        if not self._authenticated:
            raise RequestError.auth_required()

        text = "".join(block.text for block in prompt if getattr(block, "type", None) == "text")

        handler = {
            "permission": self._prompt_permission,
            "plan": self._prompt_plan,
            "slow": self._prompt_slow,
            "steer": self._prompt_steer,
        }.get(SCENARIO, self._prompt_stream)
        return await handler(session_id, text)

    async def ext_method(self, method: str, params: dict) -> dict:
        """`_session/steering`, for real — the `steer` scenario only.
        `ext_method` being defined at all auto-wires it for EVERY scenario
        (`acp.agent.router.build_agent_router`, since `Agent` protocol
        declares `ext_method` — `acp/agent/router.py:105-107`), so every
        other scenario has to actively refuse it here to still stand in for
        "this agent never advertised the extension at all," the same as a
        real agent lacking it entirely would."""
        if SCENARIO != "steer" or method != "session/steering":
            raise RequestError.method_not_found(f"_{method}")
        session_id = params.get("sessionId")
        prompt = params.get("prompt") or []
        text = "".join(
            block.get("text", "")
            for block in prompt
            if isinstance(block, dict) and block.get("type") == "text"
        )
        idle_behavior = ((params.get("_meta") or {}).get("steering") or {}).get("idleBehavior")
        if session_id not in self._turns_in_flight:
            if idle_behavior == "promptRequired":
                return {"outcome": "promptRequired", "reason": "noRunningTurn"}
            return {"outcome": "startedNewTurn"}
        self._steer_injected[session_id] = text
        event = self._steer_events.get(session_id)
        if event is not None:
            event.set()
        return {"outcome": "injected"}

    # --- scenarios --------------------------------------------------------

    async def _prompt_stream(self, session_id: str, text: str):
        reply = f"echo: {text}" if text else "hi there"
        await self._client.session_update(
            session_id=session_id, update=_thought_chunk("thinking...", "t1")
        )
        for chunk in _split(reply, 3):
            update = _message_chunk(chunk, "m1")
            await self._client.session_update(session_id=session_id, update=update)
            await asyncio.sleep(0)
        return acp.PromptResponse(stop_reason="end_turn")

    async def _prompt_permission(self, session_id: str, text: str):
        options = [
            PermissionOption(option_id="allow_once", name="Allow", kind="allow_once"),
            PermissionOption(option_id="reject_once", name="Reject", kind="reject_once"),
        ]
        tool_call = ToolCallUpdate(tool_call_id="tc1", title="rm -rf /tmp/x", kind="execute")
        response = await self._client.request_permission(
            session_id=session_id, tool_call=tool_call, options=options
        )
        outcome = response.outcome
        if getattr(outcome, "outcome", None) == "selected":
            reply = f"permission: {outcome.option_id}"
        else:
            reply = "permission: cancelled"
        await self._client.session_update(session_id=session_id, update=_message_chunk(reply, "m1"))
        return acp.PromptResponse(stop_reason="end_turn")

    async def _prompt_plan(self, session_id: str, text: str):
        await self._client.session_update(
            session_id=session_id,
            update=update_plan([plan_entry("step 1", status="in_progress"), plan_entry("step 2")]),
        )
        await self._client.session_update(
            session_id=session_id,
            update=start_tool_call("tc1", "Reading scene.py", kind="read", status="in_progress"),
        )
        await asyncio.sleep(0)
        tool_update = update_tool_call("tc1", status="completed")
        await self._client.session_update(session_id=session_id, update=tool_update)
        done = _message_chunk("done", "m1")
        await self._client.session_update(session_id=session_id, update=done)
        return acp.PromptResponse(stop_reason="end_turn")

    async def _prompt_slow(self, session_id: str, text: str):
        event = asyncio.Event()
        self._cancel_events[session_id] = event
        try:
            await asyncio.wait_for(event.wait(), timeout=30)
            return acp.PromptResponse(stop_reason="cancelled")
        except asyncio.TimeoutError:
            return acp.PromptResponse(stop_reason="end_turn")
        finally:
            self._cancel_events.pop(session_id, None)

    async def _prompt_steer(self, session_id: str, text: str):
        """Stays "in flight" (per `ext_method`'s own check) until either a
        steer lands or a cancel/timeout ends it — long enough for a test to
        call `_session/steering` mid-turn, the way §31 was measured against
        the real adapter."""
        self._turns_in_flight.add(session_id)
        steer_event = asyncio.Event()
        self._steer_events[session_id] = steer_event
        cancel_event = asyncio.Event()
        self._cancel_events[session_id] = cancel_event
        try:
            await self._client.session_update(
                session_id=session_id, update=_message_chunk("working...", "m1")
            )
            done, _pending = await asyncio.wait(
                [asyncio.ensure_future(steer_event.wait()), asyncio.ensure_future(cancel_event.wait())],
                timeout=30,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_event.is_set():
                return acp.PromptResponse(stop_reason="cancelled")
            injected = self._steer_injected.pop(session_id, "")
            if injected:
                await self._client.session_update(
                    session_id=session_id,
                    update=_message_chunk(f"steered-reply: {injected}", "m2"),
                )
            else:
                await self._client.session_update(
                    session_id=session_id, update=_message_chunk("done", "m1")
                )
            return acp.PromptResponse(stop_reason="end_turn")
        finally:
            self._turns_in_flight.discard(session_id)
            self._steer_events.pop(session_id, None)
            self._cancel_events.pop(session_id, None)


async def _main() -> None:
    # Normally this would just be `await acp.run_agent(FakeAgent())`. But
    # agent-client-protocol 0.12.0 declares AGENT_METHODS["logout"] and the
    # LogoutRequest/LogoutResponse schema, yet the route for "logout" in
    # build_agent_router() (used internally by both `run_agent` and
    # `AgentSideConnection`) isn't registered at all — verified by reading
    # acp/agent/router.py: it's route_request calls for every method,
    # line by line, and "logout" simply isn't among them. That means ANY
    # agent on this SDK version would get method_not_found on a real
    # logout, no matter what it implements itself. So here we unroll
    # `run_agent` by hand (this is exactly its own code) and add the
    # missing route via the router's public method, so the "auth" scenario
    # can genuinely exercise logout.
    agent = FakeAgent()
    output_stream, input_stream = await stdio_streams(limit=50 * 1024 * 1024)
    conn = AgentSideConnection(agent, input_stream, output_stream, listening=False)
    router = conn._conn._handler
    router.route_request(AGENT_METHODS["logout"], LogoutRequest, agent, "logout")
    try:
        await conn.listen()
    finally:
        await asyncio.shield(conn.close())


if __name__ == "__main__":
    asyncio.run(_main())

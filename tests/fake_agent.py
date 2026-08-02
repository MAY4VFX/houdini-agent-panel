"""Настоящий минимальный ACP-агент для тестов `AcpClient`.

Единственный честный способ проверить протокольный слой панели — говорить с
настоящим `acp.run_agent`, а не с макетом: JSON-RPC сериализация, дискриминаторы
pydantic, порядок сообщений — всё это легко сломать заглушкой незаметно для
себя. Поэтому этот файл запускается отдельным процессом
(`sys.executable tests/fake_agent.py`), как обычный ACP-агент через stdio.

Поведение выбирается переменной окружения ``FAKE_AGENT_SCENARIO``:

- ``stream`` (по умолчанию) — обычный стриминг ответа несколькими чанками.
- ``auth`` — `prompt` кидает `auth_required`, пока не позвали `authenticate`.
- ``permission`` — просит разрешение перед ответом, эхает выбранную опцию.
- ``modes`` — предлагает `availableModes`/`currentModeId`, слушает `set_session_mode`.
- ``plan`` — шлёт план и `tool_call`/`tool_call_update` перед ответом.
- ``slow`` — висит в `prompt`, пока не придёт `session/cancel` (для теста отмены).
"""

from __future__ import annotations

import asyncio
import os

import acp
from acp.exceptions import RequestError
from acp.helpers import plan_entry, start_tool_call, text_block, update_plan, update_tool_call
from acp.schema import (
    AgentCapabilities,
    AgentMessageChunk,
    AgentThoughtChunk,
    AuthMethodAgent,
    CurrentModeUpdate,
    Implementation,
    PermissionOption,
    PromptCapabilities,
    SessionMode,
    SessionModeState,
    ToolCallUpdate,
)

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
    size = max(1, -(-len(text) // parts))  # ceil-деление, чтобы не терять хвост
    return [text[i : i + size] for i in range(0, len(text), size)] or [text]


class FakeAgent:
    """Реализует `acp.interfaces.Agent` через duck-typing (протокол, не ABC)."""

    def __init__(self) -> None:
        self._client = None  # заполняется в on_connect настоящим Client-прокси
        self._authenticated = SCENARIO != "auth"
        self._sessions: dict[str, str | None] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._session_counter = 0

    # --- ACP Agent protocol --------------------------------------------

    def on_connect(self, conn) -> None:
        self._client = conn

    async def initialize(
        self, protocol_version, client_capabilities=None, client_info=None, **kwargs
    ):
        auth_methods = []
        if SCENARIO == "auth":
            auth_methods = [
                AuthMethodAgent(
                    id=_AUTH_METHOD_ID, name="API Key", description="тестовый метод входа"
                )
            ]
        prompt_caps = PromptCapabilities(image=True, audio=False, embedded_context=True)
        return acp.InitializeResponse(
            protocol_version=acp.PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(
                load_session=False, prompt_capabilities=prompt_caps
            ),
            auth_methods=auth_methods,
            agent_info=Implementation(name="fake-agent", version="0.0.1"),
        )

    async def authenticate(self, method_id, **kwargs):
        if SCENARIO == "auth" and method_id == _AUTH_METHOD_ID:
            self._authenticated = True
        return None

    async def new_session(self, cwd, additional_directories=None, mcp_servers=None, **kwargs):
        self._session_counter += 1
        session_id = f"sess-{self._session_counter}"
        modes = None
        if SCENARIO == "modes":
            modes = SessionModeState(
                current_mode_id="ask",
                available_modes=[
                    SessionMode(id="ask", name="Ask"),
                    SessionMode(id="code", name="Code"),
                ],
            )
        self._sessions[session_id] = "ask" if modes else None
        return acp.NewSessionResponse(session_id=session_id, modes=modes)

    async def set_session_mode(self, session_id, mode_id, **kwargs):
        self._sessions[session_id] = mode_id
        if self._client is not None:
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
        }.get(SCENARIO, self._prompt_stream)
        return await handler(session_id, text)

    # --- сценарии --------------------------------------------------------

    async def _prompt_stream(self, session_id: str, text: str):
        reply = f"эхо: {text}" if text else "привет"
        await self._client.session_update(
            session_id=session_id, update=_thought_chunk("думаю...", "t1")
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
            reply = f"разрешение: {outcome.option_id}"
        else:
            reply = "разрешение: отменено"
        await self._client.session_update(session_id=session_id, update=_message_chunk(reply, "m1"))
        return acp.PromptResponse(stop_reason="end_turn")

    async def _prompt_plan(self, session_id: str, text: str):
        await self._client.session_update(
            session_id=session_id,
            update=update_plan([plan_entry("шаг 1", status="in_progress"), plan_entry("шаг 2")]),
        )
        await self._client.session_update(
            session_id=session_id,
            update=start_tool_call("tc1", "Читаю scene.py", kind="read", status="in_progress"),
        )
        await asyncio.sleep(0)
        tool_update = update_tool_call("tc1", status="completed")
        await self._client.session_update(session_id=session_id, update=tool_update)
        done = _message_chunk("готово", "m1")
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


async def _main() -> None:
    await acp.run_agent(FakeAgent())


if __name__ == "__main__":
    asyncio.run(_main())

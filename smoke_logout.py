import asyncio
import subprocess
import sys

import acp
from acp.schema import AgentCapabilities, AgentAuthCapabilities, ClientCapabilities, LogoutCapabilities


class MinimalClient:
    def on_connect(self, conn):
        self.agent = conn

    async def session_update(self, session_id, update, **kwargs):
        pass

    async def request_permission(self, session_id, tool_call, options, **kwargs):
        raise NotImplementedError


class FakeAgent:
    def __init__(self):
        self._authenticated = False

    def on_connect(self, conn):
        self._client = conn

    async def initialize(self, protocol_version, client_capabilities=None, client_info=None, **kwargs):
        return acp.InitializeResponse(
            protocol_version=acp.PROTOCOL_VERSION,
            agent_capabilities=AgentCapabilities(
                auth=AgentAuthCapabilities(logout=LogoutCapabilities())
            ),
        )

    async def authenticate(self, method_id, **kwargs):
        self._authenticated = True

    async def logout(self, **kwargs):
        print("AGENT: logout() called!", flush=True)
        self._authenticated = False

    async def new_session(self, cwd, additional_directories=None, mcp_servers=None, **kwargs):
        return acp.NewSessionResponse(session_id="s1")

    async def prompt(self, session_id, prompt, **kwargs):
        if not self._authenticated:
            raise acp.RequestError.auth_required()
        return acp.PromptResponse(stop_reason="end_turn")

    async def cancel(self, session_id, **kwargs):
        pass


AGENT_SCRIPT = '''
import asyncio
import sys
sys.path.insert(0, "/Users/may/Github/houdini-agent-panel")
from smoke_logout import FakeAgent
from acp.agent.connection import AgentSideConnection
from acp.meta import AGENT_METHODS
from acp.schema import LogoutRequest
from acp.stdio import stdio_streams

async def main():
    agent = FakeAgent()
    output_stream, input_stream = await stdio_streams(limit=50 * 1024 * 1024)
    conn = AgentSideConnection(agent, input_stream, output_stream, listening=False)
    router = conn._conn._handler
    router.route_request(AGENT_METHODS["logout"], LogoutRequest, agent, "logout")
    try:
        await conn.listen()
    finally:
        await asyncio.shield(conn.close())

asyncio.run(main())
'''

with open("/private/tmp/claude-501/-Users-may-Github-houdini-agent-panel/a7fef1ea-538a-4f89-9c36-c8786f7e330e/scratchpad/_agent_script.py", "w") as f:
    f.write(AGENT_SCRIPT)


async def main():
    client = MinimalClient()
    env = dict(acp.default_environment())
    async with acp.spawn_agent_process(
        client,
        sys.executable,
        "/private/tmp/claude-501/-Users-may-Github-houdini-agent-panel/a7fef1ea-538a-4f89-9c36-c8786f7e330e/scratchpad/_agent_script.py",
        env=env,
    ) as (conn, process):
        init = await conn.initialize(protocol_version=acp.PROTOCOL_VERSION, client_capabilities=ClientCapabilities())
        print("supports_logout raw:", init.agent_capabilities.auth)
        session = await conn.new_session(cwd="/tmp", mcp_servers=[])

        try:
            await conn.prompt(session_id=session.session_id, prompt=[acp.text_block("hi")])
        except acp.RequestError as e:
            print("expected auth_required:", e.code)

        await conn.authenticate(method_id="whatever")
        resp = await conn.prompt(session_id=session.session_id, prompt=[acp.text_block("hi")])
        print("after auth, stop_reason:", resp.stop_reason)

        # now log out via low-level send_request, same as client.py will do
        result = await conn._conn.send_request(acp.AGENT_METHODS["logout"], {})
        print("logout result:", result)

        try:
            await conn.prompt(session_id=session.session_id, prompt=[acp.text_block("hi")])
            print("ERROR: prompt succeeded after logout, should have failed")
        except acp.RequestError as e:
            print("expected auth_required again after logout:", e.code)


asyncio.run(main())

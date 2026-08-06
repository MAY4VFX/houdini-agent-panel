"""Tests for `AcpClient` against a real ACP agent (`tests/fake_agent.py`).

The only honest way to test the protocol layer is a real subprocess speaking
ACP (see docs/architecture.md §11). Signals are awaited with a
`processEvents()` loop and a timeout: a test must fail on timeout rather than
hang forever if something in the interaction broke.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from houdini_agent_panel.client import AcpClient
from houdini_agent_panel.ui.qt import QtCore

FAKE_AGENT = Path(__file__).parent / "fake_agent.py"

#: Two different waits, two different ceilings — this file used to have one
#: `_TIMEOUT` for both, which is what made it flaky under load (issue #28).
#:
#: `_CONNECT_TIMEOUT` covers `client.start()`: spawning a whole subprocess,
#: a cold Python interpreter importing `houdini_agent_panel`/`acp`, and a
#: JSON-RPC `initialize` round trip. Measured directly (`_pump_until` printed
#: its own elapsed time for a few runs under 12 CPU-bound processes pinning
#: every core): this step normally lands under 1.3s, but the tail is heavy —
#: several runs cleared 2s, one hit 4.66s, a hair under the old 5s ceiling
#: for EVERY wait in the file. Process creation is exactly what OS scheduling
#: contention hits hardest, so it gets the generous budget.
#:
#: `_RESPONSE_TIMEOUT` covers everything else: a session, a prompt's reply, a
#: mode change, a permission decision, a cancel — all requests to an agent
#: process that is ALREADY UP AND RUNNING. The same measurement never saw one
#: of these clear 0.34s, load or no load — an in-memory event loop tick isn't
#: exposed to the process-creation cost that makes `_CONNECT_TIMEOUT` need
#: headroom, so this one keeps the original 5s: not raising it is deliberate
#: — inflating it "to be safe" would hide a real protocol regression behind
#: a much longer wait before the test ever reports it.
_CONNECT_TIMEOUT = 20.0
_RESPONSE_TIMEOUT = 5.0


@dataclass
class _Spec:
    """Mirrors the shape of `runtime.LaunchSpec` without depending on the
    runtime.py module.

    `client.py` only ever touches `.command`/`.args`/`.env` (duck typing),
    so the tests don't need the real `LaunchSpec`.
    """

    command: str
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)


def _spec(scenario: str) -> _Spec:
    return _Spec(
        command=sys.executable,
        args=[str(FAKE_AGENT)],
        env={"FAKE_AGENT_SCENARIO": scenario},
    )


def _pump_until(qapp, predicate, what: str, *, timeout: float = _RESPONSE_TIMEOUT) -> None:
    """Pump the Qt loop until `predicate` holds, or fail saying what we were
    waiting for — "condition did not become true" alone doesn't tell you
    whether the agent never started or started and then never answered,
    and those are two different bugs."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        qapp.processEvents(QtCore.QEventLoop.AllEvents, 50)
    raise AssertionError(f"timed out after {timeout}s waiting for: {what}")


def _pump_for(qapp, duration: float) -> None:
    """Pump Qt events for a fixed duration without checking anything.

    Needed where there's no signal to wait for yet (the scenario hasn't sent
    anything observable), and we just need to give the background process
    time to reach a certain point — e.g. `await event.wait()` in the "slow"
    scenario, before sending cancel.
    """
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        qapp.processEvents(QtCore.QEventLoop.AllEvents, 50)


class _Recorder:
    """Collects the arguments of every signal call — without QSignalSpy, so
    we don't have to pull QtTest in on top of `ui/qt.py`."""

    def __init__(self, signal) -> None:
        self.calls: list[tuple] = []
        signal.connect(self._on_emit)

    def _on_emit(self, *args) -> None:
        self.calls.append(args)


@pytest.fixture
def make_client(qapp):
    clients: list[AcpClient] = []

    def _make() -> AcpClient:
        client = AcpClient()
        clients.append(client)
        return client

    yield _make

    for client in clients:
        client.stop()


def _connect(qapp, client: AcpClient, scenario: str, tmp_path) -> _Recorder:
    connected = _Recorder(client.connected)
    failed = _Recorder(client.failed)
    client.start(_spec(scenario), cwd=str(tmp_path))
    _pump_until(
        qapp,
        lambda: connected.calls or failed.calls,
        f"the {scenario!r} agent process to start (spawn + initialize)",
        timeout=_CONNECT_TIMEOUT,
    )
    assert not failed.calls, f"agent failed to come up: {failed.calls}"
    return connected


def _new_session(qapp, client: AcpClient, tmp_path) -> str:
    started = _Recorder(client.session_started)
    client.new_session(cwd=str(tmp_path), mcp_servers=[])
    _pump_until(qapp, lambda: started.calls, "a running agent to answer session/new")
    session_id, state = started.calls[0]
    assert state.session_id == session_id
    return session_id


# --- connect / AgentInfo ------------------------------------------------


def test_connect_reports_agent_info_from_initialize(qapp, make_client, tmp_path):
    client = make_client()
    connected = _connect(qapp, client, "stream", tmp_path)

    info = connected.calls[0][0]
    assert info.name == "fake-agent"
    assert info.version == "0.0.1"
    assert info.protocol_version == 1
    assert info.supports_image is True
    assert info.supports_audio is False
    assert info.supports_embedded_context is True
    assert info.supports_load_session is False
    assert info.supports_logout is False
    assert info.auth_methods == ()

    assert client.is_running() is True
    assert client.agent_info() == info


def test_client_comes_back_up_after_stop(qapp, make_client, tmp_path):
    """Switching agents must not kill the client for good.

    `AgentPanel._on_agent_chosen` stops the shared client and starts the new
    agent on the SAME object — every panel's slots are wired to it. A worker
    whose asyncio loop has been closed can never accept work again
    ("Event loop is closed"), so the second launch used to go nowhere: the
    header chip named the new agent and nothing else ever happened, no new
    conversation, no reply, until Houdini was restarted.
    """
    client = make_client()
    _connect(qapp, client, "stream", tmp_path)
    first = _new_session(qapp, client, tmp_path)

    client.stop()
    assert client.is_running() is False

    _connect(qapp, client, "stream", tmp_path)
    second = _new_session(qapp, client, tmp_path)

    assert client.is_running() is True
    assert second and first


# --- reply streaming ---------------------------------------------------------


def test_prompt_streams_thought_then_message_and_finishes(qapp, make_client, tmp_path):
    client = make_client()
    _connect(qapp, client, "stream", tmp_path)
    session_id = _new_session(qapp, client, tmp_path)

    thoughts = _Recorder(client.thought_chunk)
    messages = _Recorder(client.message_chunk)
    finished = _Recorder(client.turn_finished)

    client.prompt(session_id, [{"type": "text", "text": "hi"}])
    # IMPORTANT: `turn_finished` is the reply to the JSON-RPC `session/prompt`
    # request, while `session_update` notifications (chunks) in
    # `agent-client-protocol` 0.12.0 are dispatched THROUGH A SEPARATE queue
    # and their own tasks (see `acp/task/dispatcher.py::_dispatch_notification`),
    # whereas a request's reply is resolved immediately and synchronously in
    # the receive loop — these two paths aren't serialized against each
    # other in any way. That means `turn_finished` can arrive BEFORE the
    # last chunk of the reply: this is a confirmed race in the SDK itself,
    # not a client bug. `docs/facts/acp-sdk.md` doesn't document this — we
    # wait for the final text, not for its order relative to `turn_finished`.
    _pump_until(
        qapp,
        lambda: finished.calls and "".join(c[2] for c in messages.calls) == "echo: hi",
        "the turn to finish with the full 'echo: hi' reply",
    )

    assert thoughts.calls, "the agent should have sent an agent_thought_chunk"
    assert "".join(c[2] for c in thoughts.calls) == "thinking..."

    # all chunks of the same message share a message_id — as required by §8 on stitching
    assert len({c[1] for c in messages.calls}) == 1

    assert finished.calls[0] == (session_id, "end_turn")


# --- auth_required -----------------------------------------------------------


def test_prompt_before_auth_emits_auth_required_then_succeeds_after(qapp, make_client, tmp_path):
    client = make_client()
    _connect(qapp, client, "auth", tmp_path)
    session_id = _new_session(qapp, client, tmp_path)

    auth_required = _Recorder(client.auth_required)
    finished = _Recorder(client.turn_finished)

    client.prompt(session_id, [{"type": "text", "text": "hi"}])
    _pump_until(
        qapp, lambda: auth_required.calls, "an auth_required signal for the unauthenticated prompt"
    )

    methods = auth_required.calls[0][0]
    assert [m.id for m in methods] == ["apikey"]

    # the connection must not have dropped because of auth_required
    assert client.is_running() is True

    client.authenticate("apikey")
    finished.calls.clear()
    client.prompt(session_id, [{"type": "text", "text": "hi"}])
    _pump_until(qapp, lambda: finished.calls, "the turn to finish after authenticating")

    assert finished.calls[0] == (session_id, "end_turn")


def test_logout_cycle_requires_auth_again(qapp, make_client, tmp_path):
    """issue #6: login works, and so does logout. The full cycle: "login
    required -> logged in -> logged out -> login required again"."""
    client = make_client()
    connected = _connect(qapp, client, "auth", tmp_path)
    session_id = _new_session(qapp, client, tmp_path)

    # The agent declared AgentAuthCapabilities(logout=LogoutCapabilities()) —
    # not None, so supports_logout must be True (UI rule: the agent doesn't
    # support it -> the logout button isn't drawn; here it does support it).
    assert connected.calls[0][0].supports_logout is True

    auth_required = _Recorder(client.auth_required)
    finished = _Recorder(client.turn_finished)

    # 1. Login required.
    client.prompt(session_id, [{"type": "text", "text": "hi"}])
    _pump_until(qapp, lambda: auth_required.calls, "the first auth_required signal")
    methods_before = [m.id for m in auth_required.calls[0][0]]
    assert methods_before == ["apikey"]

    # 2. Logged in.
    client.authenticate("apikey")
    client.prompt(session_id, [{"type": "text", "text": "hi"}])
    _pump_until(qapp, lambda: finished.calls, "the turn to finish after logging in")
    assert finished.calls[0] == (session_id, "end_turn")

    # 3. Logged out — reuse auth_required as the "agent logged out" signal:
    # the login screen should appear again with the same authMethods.
    auth_required.calls.clear()
    client.logout()
    _pump_until(qapp, lambda: auth_required.calls, "an auth_required signal after logout")
    assert [m.id for m in auth_required.calls[0][0]] == methods_before

    # the connection must not have dropped because of the logout
    assert client.is_running() is True

    # 4. Login required again.
    finished.calls.clear()
    client.prompt(session_id, [{"type": "text", "text": "hi"}])
    _pump_until(
        qapp, lambda: len(auth_required.calls) >= 2, "a second auth_required after logging out"
    )
    assert not finished.calls, "prompt must not go through without logging in again"


# --- permissions ---------------------------------------------------------------


def test_permission_request_waits_for_ui_answer(qapp, make_client, tmp_path):
    client = make_client()
    _connect(qapp, client, "permission", tmp_path)
    session_id = _new_session(qapp, client, tmp_path)

    requested = _Recorder(client.permission_requested)
    finished = _Recorder(client.turn_finished)
    messages = _Recorder(client.message_chunk)

    client.prompt(session_id, [{"type": "text", "text": "hi"}])
    _pump_until(qapp, lambda: requested.calls, "a permission_requested signal")

    request_key, req_session_id, tool_call, options = requested.calls[0]
    assert req_session_id == session_id
    assert tool_call.title == "rm -rf /tmp/x"
    assert [o.option_id for o in options] == ["allow_once", "reject_once"]

    # the prompt must not finish until the panel has answered
    assert not finished.calls

    client.answer_permission(request_key, "allow_once")
    # see the comment in test_prompt_streams_... — turn_finished and the
    # last chunk aren't serialized with each other in the SDK, wait for both
    # conditions.
    expected = "permission: allow_once"
    _pump_until(
        qapp,
        lambda: finished.calls and "".join(c[2] for c in messages.calls) == expected,
        "the turn to finish after allowing the permission",
    )


def test_permission_cancelled_when_answered_with_none(qapp, make_client, tmp_path):
    client = make_client()
    _connect(qapp, client, "permission", tmp_path)
    session_id = _new_session(qapp, client, tmp_path)

    requested = _Recorder(client.permission_requested)
    finished = _Recorder(client.turn_finished)
    messages = _Recorder(client.message_chunk)

    client.prompt(session_id, [{"type": "text", "text": "hi"}])
    _pump_until(qapp, lambda: requested.calls, "a permission_requested signal")

    request_key = requested.calls[0][0]
    client.answer_permission(request_key, None)
    expected = "permission: cancelled"
    _pump_until(
        qapp,
        lambda: finished.calls and "".join(c[2] for c in messages.calls) == expected,
        "the turn to finish after cancelling the permission",
    )


# --- modes --------------------------------------------------------------------


def test_modes_from_new_session_and_set_mode_update(qapp, make_client, tmp_path):
    client = make_client()
    _connect(qapp, client, "modes", tmp_path)

    started = _Recorder(client.session_started)
    client.new_session(cwd=str(tmp_path), mcp_servers=[])
    _pump_until(qapp, lambda: started.calls, "a running agent to answer session/new")
    session_id, state = started.calls[0]

    assert state.current_mode_id == "ask"
    assert [m.id for m in state.available_modes] == ["ask", "code"]

    modes_changed = _Recorder(client.modes_changed)
    client.set_mode(session_id, "code")
    _pump_until(qapp, lambda: modes_changed.calls, "a modes_changed signal after set_mode")

    changed_session_id, mode_state = modes_changed.calls[0]
    assert changed_session_id == session_id
    assert mode_state.current_mode_id == "code"
    assert [m.id for m in mode_state.available_modes] == ["ask", "code"]


def test_successful_set_mode_is_acknowledged_when_agent_sends_no_update(
    qapp, make_client, tmp_path
):
    """Codex returns success but does not emit ``current_mode_update``."""
    client = make_client()
    _connect(qapp, client, "modes-no-echo", tmp_path)
    session_id = _new_session(qapp, client, tmp_path)
    modes_changed = _Recorder(client.modes_changed)

    client.set_mode(session_id, "code")
    _pump_until(qapp, lambda: modes_changed.calls, "set_mode RPC acknowledgement")

    changed_session_id, mode_state = modes_changed.calls[-1]
    assert changed_session_id == session_id
    assert mode_state.current_mode_id == "code"
    assert [m.id for m in mode_state.available_modes] == ["ask", "code"]


# --- plan and tool_call ------------------------------------------------------


def test_plan_and_tool_call_events(qapp, make_client, tmp_path):
    client = make_client()
    _connect(qapp, client, "plan", tmp_path)
    session_id = _new_session(qapp, client, tmp_path)

    plan_changed = _Recorder(client.plan_changed)
    tool_call = _Recorder(client.tool_call)
    tool_call_update = _Recorder(client.tool_call_update)
    finished = _Recorder(client.turn_finished)

    client.prompt(session_id, [{"type": "text", "text": "hi"}])
    # see the comment in test_prompt_streams_... about the notification/response
    # race in the SDK itself — wait for all expected signals, not for their
    # order relative to turn_finished.
    def _all_arrived() -> bool:
        return bool(
            finished.calls and plan_changed.calls and tool_call.calls and tool_call_update.calls
        )

    _pump_until(
        qapp, _all_arrived, "turn_finished plus plan_changed, tool_call and tool_call_update"
    )

    plan_session_id, entries = plan_changed.calls[0]
    assert plan_session_id == session_id
    assert [e.content for e in entries] == ["step 1", "step 2"]

    assert tool_call.calls[0][1].tool_call_id == "tc1"
    assert tool_call.calls[0][1].status == "in_progress"

    assert tool_call_update.calls[0][1].status == "completed"


# --- cancel --------------------------------------------------------------------


def test_cancel_stops_slow_prompt(qapp, make_client, tmp_path):
    client = make_client()
    _connect(qapp, client, "slow", tmp_path)
    session_id = _new_session(qapp, client, tmp_path)

    finished = _Recorder(client.turn_finished)
    client.prompt(session_id, [{"type": "text", "text": "hi"}])

    # give the agent time to actually reach the cancel wait point — a short
    # pause before cancel via the same event pump, not a bare time.sleep().
    _pump_for(qapp, 0.2)
    client.cancel(session_id)

    _pump_until(qapp, lambda: finished.calls, "the turn to finish as cancelled")
    assert finished.calls[0] == (session_id, "cancelled")


# --- stop -------------------------------------------------------------------


def test_stop_is_clean_and_reports_running_false(qapp, make_client, tmp_path):
    client = make_client()
    _connect(qapp, client, "stream", tmp_path)

    disconnected = _Recorder(client.disconnected)
    client.stop()

    assert client.is_running() is False
    assert client.agent_info() is None
    assert disconnected.calls and disconnected.calls[0] == ("",)


# --- orphans.py wiring (may-hub task, 2026-08-04) ---------------------------
#
# `AcpClient.stop()` is what runs on `aboutToQuit`/`atexit` — neither fires
# on a SIGKILLed or crashed Houdini, so the process `do_start` spawns can
# outlive it. `orphans.record_started`/`record_stopped` are the record half
# of the fix (see orphans.py for the sweep half); these two tests check
# that `client.py` actually calls them, with a real fake-agent subprocess,
# not a mock standing in for the whole launch.


def test_starting_an_agent_records_it_for_orphans_and_stopping_removes_it(qapp, tmp_path):
    from houdini_agent_panel import orphans

    client = AcpClient(agent_id="test-agent")
    try:
        _connect(qapp, client, "stream", tmp_path)

        records = orphans._load()
        assert len(records) == 1
        record = next(iter(records.values()))
        assert record.agent_id == "test-agent"
        assert record.command == sys.executable
        assert record.args == [str(FAKE_AGENT)]
        assert record.cwd == str(tmp_path)
        assert record.started_at

        client.stop()

        assert orphans._load() == {}
    finally:
        client.stop()


def test_a_launch_that_fails_to_come_up_does_not_leave_an_orphans_record(qapp, tmp_path):
    """`_cleanup()` (the rollback path for a launch that never reached
    `connected`) calls `_terminate_process()` too — same `_forget_process`
    call, so a failed launch doesn't leave a phantom entry for the next
    boot to puzzle over. A process that dies instantly (same shape as
    `test_client_dead_agent.py`), not the fake agent — the point here is
    the rollback path, not a real handshake."""
    from houdini_agent_panel import orphans

    client = AcpClient(agent_id="test-agent")
    try:
        failed = _Recorder(client.failed)
        spec = _Spec(command=sys.executable, args=["-c", "import sys; sys.exit(1)"])
        client.start(spec, cwd=str(tmp_path))
        _pump_until(qapp, lambda: failed.calls, "the agent to fail to start")

        assert orphans._load() == {}
    finally:
        client.stop()
#
# docs/facts/houdini.md §9: inside Houdini, `asyncio.new_event_loop()`
# (through the active policy) returns `haio.HoudiniEventLoop`, whose
# `run_forever()` requires the main thread, while `asyncio.create_subprocess_exec`
# there also blows up on `get_child_watcher()` -> NotImplementedError. Outside
# Houdini the policy is the stock one, and both calls work — so ordinary
# tests don't catch this. Here we install a policy with exactly the same
# behavior and run a real fake_agent.py under it: `AcpClient` must still
# work, because the loop is taken directly from the class (bypassing the
# policy), and the process is spawned with `subprocess.Popen` (bypassing the
# child watcher) — see the client.py module docstring.


class _HaioLikeEventLoop(asyncio.SelectorEventLoop):
    """Imitates the one observable haio behavior that matters to us:
    `run_forever()` off the main thread raises RuntimeError."""

    def run_forever(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("Current thread is not the main thread")
        super().run_forever()


class _HaioLikeEventLoopPolicy(asyncio.DefaultEventLoopPolicy):
    def new_event_loop(self) -> asyncio.AbstractEventLoop:
        return _HaioLikeEventLoop()

    def get_child_watcher(self):
        raise NotImplementedError("haio does not support child watchers")


@pytest.fixture
def haio_like_policy():
    """Swaps out the global asyncio policy for the duration of the test and
    restores it afterward — otherwise every test running after this one in
    the same pytest run would break (the policy is global to the process)."""
    original = asyncio.get_event_loop_policy()
    asyncio.set_event_loop_policy(_HaioLikeEventLoopPolicy())
    try:
        yield
    finally:
        asyncio.set_event_loop_policy(original)


def test_client_works_under_a_haio_like_event_loop_policy(
    qapp, haio_like_policy, tmp_path, make_client
):
    """Would fail red without the workaround in client.py: `asyncio.new_event_loop()`
    would get `_HaioLikeEventLoop`, whose `run_forever()` fails on the worker
    thread — `connected` would never arrive, and the test would fail on timeout."""
    client = make_client()
    connected = _connect(qapp, client, "stream", tmp_path)
    assert connected.calls[0][0].name == "fake-agent"

    session_id = _new_session(qapp, client, tmp_path)

    finished = _Recorder(client.turn_finished)
    messages = _Recorder(client.message_chunk)
    client.prompt(session_id, [{"type": "text", "text": "hi"}])
    _pump_until(
        qapp,
        lambda: finished.calls and "".join(c[2] for c in messages.calls) == "echo: hi",
        "the turn to finish with the full 'echo: hi' reply",
    )

    assert finished.calls[0] == (session_id, "end_turn")

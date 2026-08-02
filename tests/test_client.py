"""Тесты `AcpClient` против настоящего ACP-агента (`tests/fake_agent.py`).

Единственный честный способ проверить протокольный слой — реальный
подпроцесс, говорящий по ACP (см. docs/architecture.md §11). Ожидание
сигналов — циклом с `processEvents()` и таймаутом: тест обязан падать по
таймауту, а не висеть вечно, если что-то во взаимодействии сломалось.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from houdini_agent_panel.client import AcpClient
from houdini_agent_panel.ui.qt import QtCore

FAKE_AGENT = Path(__file__).parent / "fake_agent.py"

#: Таймаут ожидания сигналов в отдельных проверках. Подпроцесс + JSON-RPC
#: туда-обратно — не бесплатно, но 5с с большим запасом на любую машину CI.
_TIMEOUT = 5.0


@dataclass
class _Spec:
    """Дублирует форму `runtime.LaunchSpec`, не завися от модуля runtime.py.

    `client.py` обращается только к `.command`/`.args`/`.env` (duck typing),
    так что настоящий `LaunchSpec` тестам не нужен.
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


def _pump_until(qapp, predicate, *, timeout: float = _TIMEOUT) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        qapp.processEvents(QtCore.QEventLoop.AllEvents, 50)
    raise AssertionError(f"условие не выполнилось за {timeout}s")


def _pump_for(qapp, duration: float) -> None:
    """Прокачать события Qt заданное время, ничего не проверяя.

    Нужно там, где нельзя дождаться сигнала (сценарий ещё не прислал
    ничего наблюдаемого), а просто дать фоновому процессу время дойти до
    нужной точки — например, до `await event.wait()` в сценарии "slow"
    перед тем, как отправить cancel.
    """
    deadline = time.monotonic() + duration
    while time.monotonic() < deadline:
        qapp.processEvents(QtCore.QEventLoop.AllEvents, 50)


class _Recorder:
    """Копит аргументы каждого вызова сигнала — без QSignalSpy, чтобы не
    тянуть QtTest поверх `ui/qt.py`."""

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
    _pump_until(qapp, lambda: connected.calls or failed.calls)
    assert not failed.calls, f"агент не поднялся: {failed.calls}"
    return connected


def _new_session(qapp, client: AcpClient, tmp_path) -> str:
    started = _Recorder(client.session_started)
    client.new_session(cwd=str(tmp_path), mcp_servers=[])
    _pump_until(qapp, lambda: started.calls)
    session_id, state = started.calls[0]
    assert state.session_id == session_id
    return session_id


# --- подключение / AgentInfo ------------------------------------------------


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


# --- стриминг ответа ---------------------------------------------------------


def test_prompt_streams_thought_then_message_and_finishes(qapp, make_client, tmp_path):
    client = make_client()
    _connect(qapp, client, "stream", tmp_path)
    session_id = _new_session(qapp, client, tmp_path)

    thoughts = _Recorder(client.thought_chunk)
    messages = _Recorder(client.message_chunk)
    finished = _Recorder(client.turn_finished)

    client.prompt(session_id, [{"type": "text", "text": "hi"}])
    # ВАЖНО: `turn_finished` — это ответ на JSON-RPC `session/prompt`, а
    # `session_update`-нотификации (чанки) в `agent-client-protocol` 0.12.0
    # диспетчеризуются ЧЕРЕЗ ОТДЕЛЬНУЮ очередь и свои задачи (см.
    # `acp/task/dispatcher.py::_dispatch_notification`), тогда как ответ на
    # запрос резолвится немедленно и синхронно в приёмном цикле — эти два
    # пути ничем не сериализованы друг с другом. Значит `turn_finished` может
    # прийти РАНЬШЕ последнего чанка отчёта: это подтверждённая гонка в самом
    # SDK, не баг клиента. `docs/facts/acp-sdk.md` этого не документирует —
    # ждём финального текста, а не порядка относительно `turn_finished`.
    _pump_until(qapp, lambda: finished.calls and "".join(c[2] for c in messages.calls) == "эхо: hi")

    assert thoughts.calls, "агент должен был прислать agent_thought_chunk"
    assert "".join(c[2] for c in thoughts.calls) == "думаю..."

    # все чанки одного сообщения делят message_id — как и требует §8 склейки
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
    _pump_until(qapp, lambda: auth_required.calls)

    methods = auth_required.calls[0][0]
    assert [m.id for m in methods] == ["apikey"]

    # соединение не должно было упасть из-за auth_required
    assert client.is_running() is True

    client.authenticate("apikey")
    finished.calls.clear()
    client.prompt(session_id, [{"type": "text", "text": "hi"}])
    _pump_until(qapp, lambda: finished.calls)

    assert finished.calls[0] == (session_id, "end_turn")


# --- разрешения ---------------------------------------------------------------


def test_permission_request_waits_for_ui_answer(qapp, make_client, tmp_path):
    client = make_client()
    _connect(qapp, client, "permission", tmp_path)
    session_id = _new_session(qapp, client, tmp_path)

    requested = _Recorder(client.permission_requested)
    finished = _Recorder(client.turn_finished)
    messages = _Recorder(client.message_chunk)

    client.prompt(session_id, [{"type": "text", "text": "hi"}])
    _pump_until(qapp, lambda: requested.calls)

    request_key, req_session_id, tool_call, options = requested.calls[0]
    assert req_session_id == session_id
    assert tool_call.title == "rm -rf /tmp/x"
    assert [o.option_id for o in options] == ["allow_once", "reject_once"]

    # промпт не должен завершиться, пока панель не ответила
    assert not finished.calls

    client.answer_permission(request_key, "allow_once")
    # см. комментарий в test_prompt_streams_... — turn_finished и последний
    # чанк не сериализованы между собой в SDK, ждём оба условия.
    expected = "разрешение: allow_once"
    _pump_until(qapp, lambda: finished.calls and "".join(c[2] for c in messages.calls) == expected)


def test_permission_cancelled_when_answered_with_none(qapp, make_client, tmp_path):
    client = make_client()
    _connect(qapp, client, "permission", tmp_path)
    session_id = _new_session(qapp, client, tmp_path)

    requested = _Recorder(client.permission_requested)
    finished = _Recorder(client.turn_finished)
    messages = _Recorder(client.message_chunk)

    client.prompt(session_id, [{"type": "text", "text": "hi"}])
    _pump_until(qapp, lambda: requested.calls)

    request_key = requested.calls[0][0]
    client.answer_permission(request_key, None)
    expected = "разрешение: отменено"
    _pump_until(qapp, lambda: finished.calls and "".join(c[2] for c in messages.calls) == expected)


# --- режимы --------------------------------------------------------------------


def test_modes_from_new_session_and_set_mode_update(qapp, make_client, tmp_path):
    client = make_client()
    _connect(qapp, client, "modes", tmp_path)

    started = _Recorder(client.session_started)
    client.new_session(cwd=str(tmp_path), mcp_servers=[])
    _pump_until(qapp, lambda: started.calls)
    session_id, state = started.calls[0]

    assert state.current_mode_id == "ask"
    assert [m.id for m in state.available_modes] == ["ask", "code"]

    modes_changed = _Recorder(client.modes_changed)
    client.set_mode(session_id, "code")
    _pump_until(qapp, lambda: modes_changed.calls)

    changed_session_id, mode_state = modes_changed.calls[0]
    assert changed_session_id == session_id
    assert mode_state.current_mode_id == "code"
    assert [m.id for m in mode_state.available_modes] == ["ask", "code"]


# --- план и tool_call ------------------------------------------------------


def test_plan_and_tool_call_events(qapp, make_client, tmp_path):
    client = make_client()
    _connect(qapp, client, "plan", tmp_path)
    session_id = _new_session(qapp, client, tmp_path)

    plan_changed = _Recorder(client.plan_changed)
    tool_call = _Recorder(client.tool_call)
    tool_call_update = _Recorder(client.tool_call_update)
    finished = _Recorder(client.turn_finished)

    client.prompt(session_id, [{"type": "text", "text": "hi"}])
    # см. комментарий в test_prompt_streams_... про гонку notification/response
    # в самом SDK — ждём все ожидаемые сигналы, а не порядок с turn_finished.
    def _all_arrived() -> bool:
        return bool(
            finished.calls and plan_changed.calls and tool_call.calls and tool_call_update.calls
        )

    _pump_until(qapp, _all_arrived)

    plan_session_id, entries = plan_changed.calls[0]
    assert plan_session_id == session_id
    assert [e.content for e in entries] == ["шаг 1", "шаг 2"]

    assert tool_call.calls[0][1].tool_call_id == "tc1"
    assert tool_call.calls[0][1].status == "in_progress"

    assert tool_call_update.calls[0][1].status == "completed"


# --- отмена --------------------------------------------------------------------


def test_cancel_stops_slow_prompt(qapp, make_client, tmp_path):
    client = make_client()
    _connect(qapp, client, "slow", tmp_path)
    session_id = _new_session(qapp, client, tmp_path)

    finished = _Recorder(client.turn_finished)
    client.prompt(session_id, [{"type": "text", "text": "hi"}])

    # даём агенту время реально дойти до ожидания отмены — небольшая пауза
    # перед cancel через тот же насос событий, не голый time.sleep().
    _pump_for(qapp, 0.2)
    client.cancel(session_id)

    _pump_until(qapp, lambda: finished.calls, timeout=_TIMEOUT)
    assert finished.calls[0] == (session_id, "cancelled")


# --- останов -------------------------------------------------------------------


def test_stop_is_clean_and_reports_running_false(qapp, make_client, tmp_path):
    client = make_client()
    _connect(qapp, client, "stream", tmp_path)

    disconnected = _Recorder(client.disconnected)
    client.stop()

    assert client.is_running() is False
    assert client.agent_info() is None
    assert disconnected.calls and disconnected.calls[0] == ("",)

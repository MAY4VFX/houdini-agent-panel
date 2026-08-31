"""Shared state gets ONE writer, however many tabs are wired to the agent.

`c4a1231` moved a session's transcript and its conversation id out of the
tab and into a process-wide, per-agent registry (`sessions.models`), because
a live session belongs to the agent process, not to whichever tab happens to
be showing it. What stayed on the tab was the *applying*: every panel wired
to that agent's shared client runs its own `_on_message_chunk`,
`_on_error`, ... against that one shared model. With a single tab open the
two are indistinguishable. With two, every append lands twice.

Reported from the owner's own store (2026-08-31, the first day in the whole
log with two panels in one Houdini process): 52 of 111 agent messages in one
conversation came out with roughly half their text duplicated —

    'V54 — соседние фасеты разошлись по рефлексу: разошлись по рефлексу: …'

— and six of the artist's OWN messages were written twice in a row, from
`_on_steered` recreating a row the sibling tab had already promoted.

`transcript_model._is_repeated_message` is why the START of each message
looked fine: it catches a chunk that repeats everything accumulated so far,
which is true for exactly the first chunk of a message and never again.
"""

from __future__ import annotations

import pytest

from houdini_agent_panel import sessions
from houdini_agent_panel import settings as settings_mod
from houdini_agent_panel.ui import panel as panel_mod


@pytest.fixture(autouse=True)
def isolated(qapp, monkeypatch):
    monkeypatch.setattr(panel_mod.scene, "hip_dir", lambda: "/tmp")
    monkeypatch.setattr(
        panel_mod.scene, "mcp_servers",
        lambda: [{"name": "fxhoudini", "command": "python", "args": [], "env": []}],
    )
    monkeypatch.setattr(panel_mod._RefreshWorker, "start", lambda self: None)
    current = settings_mod.load()
    current.default_agent = "claude-acp"
    current.autostart_agent = False  # tests fake the live session themselves
    settings_mod.save(current)
    panel_mod.reset_shared_state_for_tests()
    yield
    panel_mod.reset_shared_state_for_tests()


def _state(session_id: str, title: str = "Conversation") -> sessions.SessionState:
    return sessions.SessionState(session_id=session_id, title=title, cwd="/tmp", created_at=0.0)


def _tabs(qapp, count: int) -> list:
    tabs = []
    for _ in range(count):
        tabs.append(panel_mod.AgentPanel())
        qapp.processEvents()
    return tabs


def _open_session(qapp, tabs: list, session_id: str = "s1") -> object:
    client = panel_mod.shared_client(tabs[0]._agent_id)
    client.session_started.emit(session_id, _state(session_id, "Rotor pyro"))
    qapp.processEvents()
    return client


def _shape(tab, session_id: str) -> list[tuple[str, str]]:
    """What the feed actually says — kind and text, without the ids, which
    are uuids for errors and queued rows and so differ run to run."""
    return [(e.kind, e.text) for e in tab._model(session_id).entries()]


class _Option:
    """`PermissionOption` by shape: option_id / name / kind."""

    def __init__(self, option_id: str, name: str, kind: str) -> None:
        self.option_id = option_id
        self.name = name
        self.kind = kind


def test_two_tabs_do_not_double_a_streamed_answer(qapp):
    first, second = _tabs(qapp, 2)
    client = _open_session(qapp, [first, second])

    for chunk in ("V54 — соседние фасеты ", "разошлись по рефлексу: ", "зоны ловят разный кусок неба."):
        client.message_chunk.emit("s1", "m1", chunk)
        qapp.processEvents()

    assert [text for kind, text in _shape(first, "s1") if kind == "agent"] == [
        "V54 — соседние фасеты разошлись по рефлексу: зоны ловят разный кусок неба."
    ]
    first.shutdown()
    second.shutdown()


def test_two_tabs_do_not_double_a_streamed_thought(qapp):
    first, second = _tabs(qapp, 2)
    client = _open_session(qapp, [first, second])

    for chunk in ("Смотрю на нормали фасетов, ", "проверяю развал."):
        client.thought_chunk.emit("s1", "m1", chunk)
        qapp.processEvents()

    assert [text for kind, text in _shape(first, "s1") if kind == "thought"] == [
        "Смотрю на нормали фасетов, проверяю развал."
    ]
    first.shutdown()
    second.shutdown()


def test_two_tabs_do_not_double_an_error_row(qapp):
    first, second = _tabs(qapp, 2)
    client = _open_session(qapp, [first, second])

    client.error.emit("s1", "the agent lost its connection")
    qapp.processEvents()

    assert [text for kind, text in _shape(first, "s1") if kind == "error"] == [
        "the agent lost its connection"
    ]
    first.shutdown()
    second.shutdown()


def test_two_tabs_do_not_double_the_agent_stopped_note(qapp):
    first, second = _tabs(qapp, 2)
    client = _open_session(qapp, [first, second])

    client.turn_finished.emit("s1", "max_tokens")
    qapp.processEvents()

    assert [text for kind, text in _shape(first, "s1") if kind == "error"] == [
        "Agent stopped: max_tokens"
    ]
    first.shutdown()
    second.shutdown()


def test_two_tabs_do_not_double_a_permission_row(qapp):
    first, second = _tabs(qapp, 2)
    client = _open_session(qapp, [first, second])

    client.permission_requested.emit(
        "req-1", "s1", object(), [_Option("allow_once", "Allow once", "allow_once")]
    )
    qapp.processEvents()

    assert len([kind for kind, _text in _shape(first, "s1") if kind == "permission"]) == 1
    first.shutdown()
    second.shutdown()


def test_two_tabs_do_not_duplicate_a_steered_message(qapp):
    """The artist's own words, written twice.

    The tab that sent the steer is the only one holding its blocks
    (`_steering_blocks`), but the SIBLING tab promotes the queued row first
    — they share one model. The sender then finds no queued row, believes
    the message was removed mid-flight, and recreates it from its blocks.
    """
    first, second = _tabs(qapp, 2)
    client = _open_session(qapp, [first, second])

    entry_id = "queued-1"
    text = "давай ещё чуть жёстче блик"
    second._model("s1").queue_message(entry_id, text, [])
    second._steering_blocks[entry_id] = [{"type": "text", "text": text}]

    client.steered.emit("s1", entry_id, "injected")
    qapp.processEvents()

    assert [t for kind, t in _shape(first, "s1") if kind == "user"] == [text]
    first.shutdown()
    second.shutdown()


def test_a_whole_turn_through_two_tabs_reads_the_same_as_through_one(qapp):
    """The net under every future handler.

    Anything that appends to the shared model has to be applied once per
    EVENT, not once per tab. Rather than name the handlers one by one — the
    next one added would not be on that list — drive a whole realistic turn
    both ways and require the feed to come out identical.
    """
    def run(tab_count: int) -> list[tuple[str, str]]:
        panel_mod.reset_shared_state_for_tests()
        tabs = _tabs(qapp, tab_count)
        client = _open_session(qapp, tabs)

        client.thought_chunk.emit("s1", "t1", "Проверяю развал нормалей.")
        client.message_chunk.emit("s1", "m1", "Смотрю сцену. ")
        client.message_chunk.emit("s1", "m1", "Нашёл три материала с opacity 0.")
        qapp.processEvents()
        client.permission_requested.emit(
            "req-1", "s1", object(), [_Option("allow_once", "Allow once", "allow_once")]
        )
        qapp.processEvents()
        client.error.emit("s1", "the fx server stopped answering")
        qapp.processEvents()
        client.turn_finished.emit("s1", "refusal")
        qapp.processEvents()

        shape = _shape(tabs[0], "s1")
        for tab in tabs:
            tab.shutdown()
        return shape

    assert run(2) == run(1)


def test_the_next_tab_takes_over_when_the_owner_closes(qapp):
    """Closing a tab must not leave the conversation with no writer at all."""
    first, second = _tabs(qapp, 2)
    client = _open_session(qapp, [first, second])

    first.shutdown()
    qapp.processEvents()

    client.message_chunk.emit("s1", "m1", "ответ после закрытия первой вкладки")
    qapp.processEvents()

    assert [text for kind, text in _shape(second, "s1") if kind == "agent"] == [
        "ответ после закрытия первой вкладки"
    ]
    second.shutdown()

"""Интеграция панели.

Здесь проверяется не отрисовка, а склейка: кто кого переживает, что происходит
со вторым табом, куда уходит ответ на разрешение. Клиент берём настоящий — но
не запускаем: его Qt-сигналы настоящие, и эмитить их из теста честнее, чем
подсовывать заглушку с похожим именем.
"""

from __future__ import annotations

import pytest

from houdini_agent_panel import sessions
from houdini_agent_panel.ui import panel as panel_mod


@pytest.fixture(autouse=True)
def isolated_panel_state(qapp, monkeypatch):
    """Каждому тесту — свой процессный синглтон и никакой сети из _boot."""
    monkeypatch.setattr(panel_mod.scene, "hip_dir", lambda: "/tmp")
    monkeypatch.setattr(
        panel_mod.scene,
        "mcp_servers",
        lambda: [{"name": "fxhoudini", "command": "python", "args": [], "env": []}],
    )
    monkeypatch.setattr(panel_mod._RefreshWorker, "start", lambda self: None)
    panel_mod.reset_shared_state_for_tests()
    yield
    panel_mod.reset_shared_state_for_tests()


def _make_panel(qapp):
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    return widget


def _session(session_id: str = "s1") -> sessions.SessionState:
    return sessions.SessionState(
        session_id=session_id, title="Новый разговор", cwd="/tmp", created_at=0.0
    )


def test_without_default_agent_panel_opens_on_agents_screen(qapp):
    """Первое открытие: агент не выбран, значит человеку показывают, из чего
    выбирать, а не пустую ленту без единого объяснения."""
    widget = _make_panel(qapp)
    widget._boot()

    assert widget._pages.currentIndex() == panel_mod.AgentPanel.PAGE_AGENTS
    widget.shutdown()


def test_two_panels_share_one_client_and_one_pool(qapp):
    """Два таба — один процесс агента. Это прямое требование design.md:
    «Один агент, много сессий»."""
    first = _make_panel(qapp)
    second = _make_panel(qapp)

    assert first._pool is second._pool
    assert panel_mod.shared_client() is panel_mod.shared_client()

    first.shutdown()
    second.shutdown()


def test_closing_one_tab_leaves_the_other_receiving_updates(qapp):
    """Самое дорогое место интеграции.

    Клиент общий, поэтому наивный `signal.disconnect()` в shutdown() одного
    таба отписал бы заодно и соседний: тот продолжал бы выглядеть живым и
    молчать в ответ на каждый промпт.
    """
    first = _make_panel(qapp)
    second = _make_panel(qapp)

    client = panel_mod.shared_client()
    state = _session()
    client.session_started.emit(state.session_id, state)
    qapp.processEvents()

    first.shutdown()
    qapp.processEvents()

    client.message_chunk.emit(state.session_id, "m1", "привет")
    qapp.processEvents()

    entries = second._model(state.session_id).entries()
    assert [entry.text for entry in entries if entry.kind == "agent"] == ["привет"]

    second.shutdown()


def test_last_tab_closing_stops_the_agent(qapp):
    """Пока жив хоть один таб, разговор продолжается; когда закрылся
    последний — процесс агента держать не за чем."""
    first = _make_panel(qapp)
    second = _make_panel(qapp)

    first.shutdown()
    assert panel_mod._shared_client is not None

    second.shutdown()
    assert panel_mod._shared_client is None


def test_shutdown_is_idempotent(qapp):
    """Houdini может позвать onDestroyInterface повторно; второй вызов не
    должен ни падать, ни гасить чужой клиент."""
    widget = _make_panel(qapp)
    widget.shutdown()
    widget.shutdown()


def test_permission_answer_reaches_client_and_resolves_in_transcript(qapp, monkeypatch):
    widget = _make_panel(qapp)
    client = panel_mod.shared_client()

    state = _session()
    client.session_started.emit(state.session_id, state)
    qapp.processEvents()

    answered: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        client, "answer_permission", lambda key, option: answered.append((key, option))
    )

    # У объекта вызова нет .title — панель обязана подставить свой текст,
    # а не показать пустую строку с кнопками неизвестно к чему.
    tool_call = object()

    # PermissionOption по форме: option_id / name / kind.
    class _Option:
        def __init__(self, option_id, name, kind):
            self.option_id = option_id
            self.name = name
            self.kind = kind

    options = [_Option("allow_once", "Разрешить один раз", "allow_once")]
    client.permission_requested.emit("req-1", state.session_id, tool_call, options)
    qapp.processEvents()

    entries = widget._model(state.session_id).entries()
    permission_entries = [entry for entry in entries if entry.kind == "permission"]
    assert len(permission_entries) == 1

    widget._on_permission_answered("req-1", "allow_once")

    assert answered == [("req-1", "allow_once")]
    assert permission_entries[0].permission.answered == "allow_once"

    widget.shutdown()


def test_blocking_announcement_blocks_input_but_not_the_transcript(qapp):
    """Прямой запрет из design.md: лента читается, панель закрывается, Houdini
    работает — блокируется только поле ввода."""
    from houdini_agent_panel.announcements import Announcement, Button

    widget = _make_panel(qapp)
    ann = Announcement(
        id="a1",
        severity="blocking",
        title="Важное",
        body="Текст",
        buttons=(Button(label="Понятно", url=""),),
    )

    class _Result:
        announcements = [ann]
        updates: list = []

    widget._on_refresh_done(_Result())
    qapp.processEvents()

    assert widget._composer.is_input_blocked()
    assert widget._transcript.isEnabled()

    widget._on_blocking_action("a1", "")
    qapp.processEvents()

    assert not widget._composer.is_input_blocked()
    assert "a1" in widget._settings.seen_announcements

    widget.shutdown()


def test_chunk_for_background_session_does_not_touch_the_visible_transcript(qapp):
    """Стриминг в невидимую сессию не должен трогать виджеты той, которую
    человек сейчас читает."""
    widget = _make_panel(qapp)
    client = panel_mod.shared_client()

    visible = _session("visible")
    background = _session("background")
    client.session_started.emit(visible.session_id, visible)
    client.session_started.emit(background.session_id, background)
    qapp.processEvents()
    widget._pool.set_current(visible.session_id)

    refreshed: list = []
    widget._transcript.refresh = lambda entry_id=None: refreshed.append(entry_id)

    client.message_chunk.emit(background.session_id, "m1", "фон")
    qapp.processEvents()

    assert refreshed == []
    assert widget._model(background.session_id).entries()

    widget.shutdown()

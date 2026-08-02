"""Интеграция панели.

Здесь проверяется не отрисовка, а склейка: кто кого переживает, что происходит
со вторым табом, куда уходит ответ на разрешение. Клиент берём настоящий — но
не запускаем: его Qt-сигналы настоящие, и эмитить их из теста честнее, чем
подсовывать заглушку с похожим именем.
"""

from __future__ import annotations

from types import SimpleNamespace

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


def test_buddy_selection_is_saved_and_restored(qapp):
    from houdini_agent_panel import settings as settings_mod

    first = _make_panel(qapp)
    first._composer.buddy_selected.emit("squid")
    assert settings_mod.load().buddy == "squid"
    first.shutdown()

    second = _make_panel(qapp)
    assert second._composer._buddy._key == "squid"
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
    widget.resize(900, 700)
    widget.show()
    qapp.processEvents()
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
    assert widget._permission_popover is not None
    anchor = widget._composer.popover_anchor_rect(widget)
    popover = widget._permission_popover
    assert abs(popover.geometry().center().x() - anchor.center().x()) <= 1
    assert popover.geometry().bottom() < anchor.top()
    assert "req-1" not in widget._transcript._rows

    widget._on_permission_answered("req-1", "allow_once")

    assert answered == [("req-1", "allow_once")]
    assert permission_entries[0].permission.answered == "allow_once"
    assert widget._permission_popover is None

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


def test_registry_reaches_the_agents_screen(qapp):
    """Экран «Агенты» сам в сеть не ходит.

    Его `refresh_from_registry` синхронный, и вызов с главного потока
    заморозил бы Houdini ровно на время сетевого таймаута. Записи обязаны
    приезжать готовыми из фонового обхода.
    """
    from houdini_agent_panel.registry import AgentEntry, BinaryDistribution

    widget = _make_panel(qapp)
    entry = AgentEntry(
        id="opencode",
        name="OpenCode",
        version="1.18.11",
        binaries={"darwin-aarch64": BinaryDistribution(
            archive="https://example.test/a.zip", cmd="./opencode", args=[], sha256="0" * 64
        )},
    )

    shown = []
    widget._agents_view.set_agents = lambda entries, updates=None: shown.append((entries, updates))

    class _Result:
        announcements: list = []
        updates: list = []

    widget._on_refresh_done(_Result(), [entry])

    assert shown and shown[0][0] == [entry]
    widget.shutdown()


def test_telemetry_consent_asked_once_and_remembered(qapp):
    """Отказ тоже запоминается.

    Иначе человек, сказавший «не надо», получал бы тот же вопрос при каждом
    открытии панели — это уже не вопрос, а выклянчивание.
    """
    from houdini_agent_panel import settings as settings_mod

    widget = _make_panel(qapp)
    widget._boot()
    assert widget._consent.isVisibleTo(widget)

    widget._on_telemetry_answer(False)

    saved = settings_mod.load()
    assert saved.telemetry_consent_asked is True
    assert saved.telemetry is False

    widget.shutdown()

    second = _make_panel(qapp)
    second._boot()
    assert not second._consent.isVisibleTo(second)
    second.shutdown()


def test_telemetry_consent_yes_turns_it_on(qapp):
    from houdini_agent_panel import settings as settings_mod

    widget = _make_panel(qapp)
    widget._on_telemetry_answer(True)

    saved = settings_mod.load()
    assert saved.telemetry is True
    assert saved.telemetry_consent_asked is True

    widget.shutdown()


def test_consent_strip_does_not_block_input(qapp):
    """Вопрос про статистику не имеет права мешать работать."""
    widget = _make_panel(qapp)
    widget._boot()

    assert not widget._composer.is_input_blocked()
    assert widget._transcript.isEnabled()

    widget.shutdown()


def test_turn_drives_activity_burst_tool_reset_and_completion(qapp, monkeypatch):
    widget = _make_panel(qapp)
    client = panel_mod.shared_client()
    state = _session()
    client.session_started.emit(state.session_id, state)
    monkeypatch.setattr(client, "prompt", lambda _session_id, _blocks: None)

    widget._on_submitted([{"type": "text", "text": "построй тестовую геометрию"}])
    activity_rows = [
        row for row in widget._transcript._rows.values() if hasattr(row, "indicator")
    ]
    assert len(activity_rows) == 1
    indicator = activity_rows[0].indicator
    first_verb = indicator._verb
    assert indicator.is_active()
    assert widget._composer._buddy._action_elapsed == 0

    call = SimpleNamespace(
        tool_call_id="tc1",
        title="Create geometry",
        kind="edit",
        status="pending",
        content=None,
        locations=None,
    )
    client.tool_call.emit(state.session_id, call)
    assert indicator.is_active()
    assert indicator._verb != first_verb

    client.turn_finished.emit(state.session_id, "end_turn")
    assert not indicator.is_active()
    assert indicator._status._text.startswith("Worked for ")

    widget.shutdown()


def test_auth_buttons_follow_the_client_across_a_restart(qapp, monkeypatch):
    """Кнопки входа не должны говорить с покойником.

    Клиент общий и пересоздаётся при смене агента. Прямая подписка
    `view.method_chosen.connect(shared_client().authenticate)` навсегда
    запомнила бы тот экземпляр, что был в момент сборки виджета.
    """
    widget = _make_panel(qapp)

    # Имитируем то, что делает смена агента: старый клиент гасится, новый
    # создаётся. Именно гасится, а не бросается — иначе его рабочий поток
    # остаётся крутиться без владельца, и Qt справедливо ругается
    # «QThread: Destroyed while thread is still running». В Houdini такой
    # осиротевший поток переживает закрытие панели.
    old = panel_mod.shared_client()
    old.stop()
    panel_mod._shared_client = None
    fresh = panel_mod.shared_client()

    seen: list = []
    monkeypatch.setattr(fresh, "authenticate", lambda mid: seen.append(("auth", mid)))
    monkeypatch.setattr(fresh, "logout", lambda: seen.append(("logout",)))

    widget._auth_view.method_chosen.emit("oauth")
    widget._auth_view.logout_requested.emit()
    qapp.processEvents()

    assert seen == [("auth", "oauth"), ("logout",)]
    widget.shutdown()

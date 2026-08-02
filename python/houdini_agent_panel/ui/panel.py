"""Корневой виджет панели — место, где всё сходится.

Здесь три решения, определяющие поведение всего остального.

**Один агент на процесс Houdini, много сессий.** Процесс агента и соединение
живут в модуле, а не в виджете: второй таб панели обязан видеть тот же
разговор и не поднимать второй процесс. Виджеты приходят и уходят, соединение
переживает их.

**Ни одной сетевой и ни одной долгой операции на главном потоке.** Houdini
рисует интерфейс тем же потоком, что и вьюпорт; секунда, потраченная здесь на
ожидание PyPI, — это секунда замёрзшего Houdini.

**`hou` — только отсюда и только синхронно.** Всё, что нужно от сцены, берётся
в момент создания панели и в ответ на пользовательские действия, то есть
заведомо с главного потока. Рабочий поток ACP-клиента `hou` не касается.
"""

from __future__ import annotations

import weakref
from typing import Any

from .. import client as acp_client
from .. import refresh, scene, sessions, settings as settings_mod
from ..transcript_model import PermissionView, TranscriptModel
from .announcement import BlockingNotice, ConsentStrip, NoticeStrip
from .chips import HeaderBar
from .composer import Composer
from .qt import QtCore, QtWidgets, Signal
from .transcript import TranscriptView

#: Соединение с агентом на весь процесс Houdini. Не атрибут виджета — иначе
#: закрытие одного таба уносило бы разговор, открытый в другом.
_shared_client: acp_client.AcpClient | None = None

#: Живые панели. Слабые ссылки: Qt удаляет виджеты сам, и держать их сильной
#: ссылкой значило бы не давать им умереть.
_live_panels: "weakref.WeakSet[AgentPanel]" = weakref.WeakSet()


def shared_client() -> acp_client.AcpClient:
    global _shared_client
    if _shared_client is None:
        _shared_client = acp_client.AcpClient()
    return _shared_client


def reset_shared_state_for_tests() -> None:
    """Сбросить процессные синглтоны. Только для тестов."""
    global _shared_client
    if _shared_client is not None:
        _shared_client.stop()
    _shared_client = None
    _live_panels.clear()
    sessions.reset_pool_for_tests()


class _RefreshWorker(QtCore.QThread):
    """Один поход в сеть на все нужды панели, в стороне от главного потока.

    Отдельный поток, а не таймер с блокирующим вызовом: даже когда сети нет,
    urllib честно ждёт таймаут, и на главном потоке это выглядит как зависшая
    Houdini.

    Реестр забирается здесь же, а не отдельно экраном «Агенты», по двум
    причинам. Дизайн обещает один суточный запрос на версии, оповещения и
    агентов. И без записей реестра `updates.check` физически не может сравнить
    версии установленных агентов — плашка «есть обновление» появлялась бы
    только для самой панели и fx, но никогда для того, чем художник
    пользуется.
    """

    done = Signal(object, object)  # RefreshResult | None, list[AgentEntry]

    def __init__(self, current: settings_mod.Settings, parent=None) -> None:
        super().__init__(parent)
        self._settings = current

    def run(self) -> None:  # pragma: no cover - проверяется через refresh.py
        entries: list = []
        try:
            from .. import registry

            entries = registry.fetch_registry()
        except Exception:  # noqa: BLE001 - без реестра панель обязана работать
            entries = []

        result = None
        try:
            result = refresh.daily_refresh(
                settings=self._settings,
                panel_version=settings_mod._panel_version(),
                entries=entries,
            )
        except Exception:  # noqa: BLE001 - фид не имеет права ломать панель
            result = None

        self.done.emit(result, entries)


class AgentPanel(QtWidgets.QWidget):
    """То, что возвращает ``onCreateInterface()``."""

    PAGE_TRANSCRIPT = 0
    PAGE_AGENTS = 1
    PAGE_SETTINGS = 2
    PAGE_AUTH = 3

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._settings = settings_mod.load()
        self._pool = sessions.pool()
        self._models: dict[str, TranscriptModel] = {}
        self._pending_permissions: dict[str, str] = {}
        self._refresh_worker: _RefreshWorker | None = None
        self._closed = False

        self._build()
        self._wire_client()
        self._wire_pool()

        _live_panels.add(self)

        # Boot откладываем на следующий проход цикла событий: Houdini ждёт
        # возврата виджета из onCreateInterface, и всё, что мы успеем сделать
        # до возврата, задерживает открытие таба.
        QtCore.QTimer.singleShot(0, self._boot)

    # ------------------------------------------------------------------ UI

    def _build(self) -> None:
        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._header = HeaderBar(self)
        self._notice = NoticeStrip(self)
        self._consent = ConsentStrip(self)
        self._pages = QtWidgets.QStackedWidget(self)
        self._composer = Composer(self)
        self._blocking = BlockingNotice(self)

        self._transcript = TranscriptView(self)
        self._pages.insertWidget(self.PAGE_TRANSCRIPT, self._transcript)
        self._pages.insertWidget(self.PAGE_AGENTS, self._make_agents_view())
        self._pages.insertWidget(self.PAGE_SETTINGS, self._make_settings_view())
        self._pages.insertWidget(self.PAGE_AUTH, self._make_auth_view())

        layout.addWidget(self._header)
        layout.addWidget(self._notice)
        layout.addWidget(self._consent)
        layout.addWidget(self._pages, 1)
        layout.addWidget(self._blocking)
        layout.addWidget(self._composer)

        self._header.agent_clicked.connect(lambda: self._show_page(self.PAGE_AGENTS))
        self._header.settings_clicked.connect(lambda: self._show_page(self.PAGE_SETTINGS))
        self._header.new_session_clicked.connect(self._start_new_session)
        self._header.session_selected.connect(self._pool.set_current)

        self._composer.submitted.connect(self._on_submitted)
        self._composer.cancelled.connect(self._on_cancelled)
        self._composer.mode_selected.connect(self._on_mode_selected)

        self._transcript.permission_answered.connect(self._on_permission_answered)

        self._notice.action_clicked.connect(self._on_notice_action)
        self._notice.dismissed.connect(self._on_notice_dismissed)
        self._blocking.action_clicked.connect(self._on_blocking_action)
        self._consent.answered.connect(self._on_telemetry_answer)

    def _make_agents_view(self) -> QtWidgets.QWidget:
        from .agents import AgentsView

        view = AgentsView(self)
        self._agents_view = view
        view.agent_chosen.connect(self._on_agent_chosen)
        view.closed.connect(lambda: self._show_page(self.PAGE_TRANSCRIPT))
        return view

    def _make_settings_view(self) -> QtWidgets.QWidget:
        from .settings_view import SettingsView

        view = SettingsView(self)
        self._settings_view = view
        view.changed.connect(self._on_settings_changed)
        view.closed.connect(lambda: self._show_page(self.PAGE_TRANSCRIPT))
        return view

    def _make_auth_view(self) -> QtWidgets.QWidget:
        from .auth_view import AuthView

        view = AuthView(self)
        self._auth_view = view
        # Через свои методы, а не напрямую в shared_client().authenticate:
        # прямая подписка навсегда запомнила бы ТОТ экземпляр клиента, что
        # существовал в момент сборки виджета. После смены агента клиент
        # пересоздаётся, и кнопки входа молча начали бы говорить с покойником.
        view.method_chosen.connect(self._on_auth_method_chosen)
        view.logout_requested.connect(self._on_logout_requested)
        return view

    def _show_page(self, index: int) -> None:
        self._pages.setCurrentIndex(index)
        # Писать агенту с экрана настроек или установки бессмысленно: ответ
        # придёт в ленту, которой человек в этот момент не видит.
        self._composer.setVisible(index == self.PAGE_TRANSCRIPT)

    # --------------------------------------------------------------- boot

    def _boot(self) -> None:
        self._header.set_cwd(scene.hip_dir())
        self._refresh_worker = _RefreshWorker(self._settings, self)
        self._refresh_worker.done.connect(self._on_refresh_done)
        self._refresh_worker.start()
        self._ask_telemetry_consent_once()

        client = shared_client()
        if client.is_running():
            # Мы второй таб: соединение уже поднято, показываем то, что есть.
            self._adopt_running_client()
            return

        agent_id = self._settings.default_agent
        if not agent_id:
            self._show_page(self.PAGE_AGENTS)
            return
        if not self._settings.autostart_agent:
            self._note("Агент не запущен. Нажми «+», чтобы начать разговор.")
            return
        self._start_agent(agent_id)

    def _adopt_running_client(self) -> None:
        info = shared_client().agent_info()
        if info is not None:
            self._header.set_agent(info.name, None)
            self._composer.set_capabilities(info, self._settings.whisper_endpoint)
        self._refresh_sessions()
        current = self._pool.current()
        if current is None:
            self._start_new_session()
        else:
            self._show_session(current.session_id)
        self._show_page(self.PAGE_TRANSCRIPT)

    def _start_agent(self, agent_id: str) -> None:
        try:
            spec = self._launch_spec(agent_id)
        except Exception as exc:  # noqa: BLE001 - причина уходит в ленту
            self._note(f"Не удалось подготовить агента {agent_id}: {exc}")
            self._show_page(self.PAGE_AGENTS)
            return
        self._note(f"Запускаю {agent_id}…")
        shared_client().start(spec, cwd=scene.hip_dir())

    def _launch_spec(self, agent_id: str):
        from .. import registry, runtime

        for custom in self._settings.custom_agents:
            if custom.id == agent_id:
                return runtime.custom_launch_spec(custom)
        entries = registry.fetch_registry()
        for entry in entries:
            if entry.id == agent_id:
                return runtime.launch_spec(entry)
        raise LookupError(f"агента {agent_id} нет ни в реестре, ни среди своих")

    # ------------------------------------------------------------- client

    def _wire_client(self) -> None:
        """Подписаться на общий клиент, запомнив КАЖДУЮ пару сигнал-слот.

        Запоминаем именно пары, потому что клиент общий на все табы: голый
        ``signal.disconnect()`` при закрытии одного таба отписал бы заодно и
        соседний, и тот перестал бы получать ответы агента, продолжая
        выглядеть живым.
        """
        client = shared_client()
        wiring = (
            (client.connected, self._on_connected),
            (client.disconnected, self._on_disconnected),
            (client.failed, self._on_failed),
            (client.auth_required, self._on_auth_required),
            (client.session_started, self._on_session_started),
            (client.modes_changed, self._on_modes_changed),
            (client.commands_changed, self._on_commands_changed),
            (client.message_chunk, self._on_message_chunk),
            (client.thought_chunk, self._on_thought_chunk),
            (client.tool_call, self._on_tool_call),
            (client.tool_call_update, self._on_tool_call_update),
            (client.plan_changed, self._on_plan_changed),
            (client.usage_changed, self._on_usage_changed),
            (client.turn_finished, self._on_turn_finished),
            (client.error, self._on_error),
            (client.permission_requested, self._on_permission_requested),
        )
        for signal, slot in wiring:
            signal.connect(slot)
        self._client_wiring = wiring

    def _on_connected(self, info: Any) -> None:
        self._header.set_agent(info.name, None)
        self._composer.set_capabilities(info, self._settings.whisper_endpoint)
        self._show_page(self.PAGE_TRANSCRIPT)
        if self._pool.current() is None:
            self._start_new_session()

    def _on_disconnected(self, reason: str) -> None:
        self._composer.set_busy(False)
        self._composer.set_capabilities(None, self._settings.whisper_endpoint)
        self._note(f"Агент отключился: {reason}" if reason else "Агент остановлен.")

    def _on_failed(self, message: str) -> None:
        self._note(f"Агент не запустился: {message}")
        self._show_page(self.PAGE_AGENTS)

    def _on_auth_required(self, methods: list) -> None:
        info = shared_client().agent_info()
        self._auth_view.set_methods(
            methods, can_logout=bool(info and info.supports_logout)
        )
        self._show_page(self.PAGE_AUTH)

    def _on_session_started(self, session_id: str, state: Any) -> None:
        self._models.setdefault(session_id, TranscriptModel())
        self._pool.add(state)
        self._pool.set_current(session_id)
        self._show_page(self.PAGE_TRANSCRIPT)

    def _on_modes_changed(self, session_id: str, mode_state: Any) -> None:
        state = self._pool.get(session_id)
        if state is not None:
            state.available_modes = list(getattr(mode_state, "available_modes", []) or [])
            state.current_mode_id = getattr(mode_state, "current_mode_id", None)
            self._pool.mark_changed(session_id)
        if self._is_current(session_id):
            self._composer.set_modes(state.available_modes, state.current_mode_id)

    def _on_commands_changed(self, session_id: str, commands: list) -> None:
        state = self._pool.get(session_id)
        if state is not None:
            state.available_commands = list(commands)
            self._pool.mark_changed(session_id)
        if self._is_current(session_id):
            self._composer.set_commands(list(commands))

    def _on_message_chunk(self, session_id: str, message_id: str, text: str) -> None:
        entry = self._model(session_id).apply_chunk(message_id, text)
        self._touch(session_id, entry.id)

    def _on_thought_chunk(self, session_id: str, message_id: str, text: str) -> None:
        entry = self._model(session_id).apply_chunk(message_id, text, thought=True)
        self._touch(session_id, entry.id)

    def _on_tool_call(self, session_id: str, call: Any) -> None:
        entry = self._model(session_id).apply_tool_call(call)
        self._touch(session_id, entry.id)

    def _on_tool_call_update(self, session_id: str, update: Any) -> None:
        entry = self._model(session_id).apply_tool_update(update)
        if entry is not None:
            self._touch(session_id, entry.id)

    def _on_plan_changed(self, session_id: str, entries: list) -> None:
        entry = self._model(session_id).apply_plan(entries)
        self._touch(session_id, entry.id)

    def _on_usage_changed(self, session_id: str, usage: Any) -> None:
        state = self._pool.get(session_id)
        if state is not None:
            state.usage = usage
        if self._is_current(session_id):
            self._composer.set_usage(usage)

    def _on_turn_finished(self, session_id: str, stop_reason: str) -> None:
        state = self._pool.get(session_id)
        if state is not None:
            state.busy = False
        if self._is_current(session_id):
            self._composer.set_busy(False)
        if stop_reason and stop_reason not in ("end_turn", "cancelled"):
            entry = self._model(session_id).append_error(f"Агент остановился: {stop_reason}")
            self._touch(session_id, entry.id)

    def _on_error(self, session_id: str, message: str) -> None:
        target = session_id or (self._pool.current().session_id if self._pool.current() else "")
        if not target:
            self._note(message)
            return
        entry = self._model(target).append_error(message)
        self._touch(target, entry.id)
        if self._is_current(target):
            self._composer.set_busy(False)

    def _on_permission_requested(
        self, request_key: str, session_id: str, tool_call: Any, options: list
    ) -> None:
        view = PermissionView(
            request_key=request_key,
            tool_title=getattr(tool_call, "title", "") or "Действие агента",
            options=[
                (option.option_id, option.name, option.kind) for option in options
            ],
        )
        self._pending_permissions[request_key] = session_id
        entry = self._model(session_id).apply_permission(view)
        self._touch(session_id, entry.id)

    def _on_permission_answered(self, request_key: str, option_id: str) -> None:
        session_id = self._pending_permissions.pop(request_key, "")
        shared_client().answer_permission(request_key, option_id or None)
        if session_id:
            entry = self._model(session_id).resolve_permission(request_key, option_id or None)
            if entry is not None:
                self._touch(session_id, entry.id)

    # ------------------------------------------------------------ sessions

    def _wire_pool(self) -> None:
        self._pool.added.connect(lambda _sid: self._refresh_sessions())
        self._pool.removed.connect(lambda _sid: self._refresh_sessions())
        self._pool.changed.connect(lambda _sid: self._refresh_sessions())
        self._pool.current_changed.connect(self._show_session)

    def _refresh_sessions(self) -> None:
        current = self._pool.current()
        self._header.set_sessions(
            self._pool.all(), current.session_id if current else None
        )

    def _show_session(self, session_id: str) -> None:
        state = self._pool.get(session_id)
        self._transcript.set_model(self._model(session_id))
        self._transcript.refresh(None)
        if state is not None:
            self._composer.set_busy(state.busy)
            self._composer.set_usage(state.usage)
            self._composer.set_commands(list(state.available_commands))
            self._composer.set_modes(state.available_modes, state.current_mode_id)
        self._refresh_sessions()

    def _start_new_session(self) -> None:
        client = shared_client()
        if not client.is_running():
            agent_id = self._settings.default_agent
            if agent_id:
                self._start_agent(agent_id)
            else:
                self._show_page(self.PAGE_AGENTS)
            return
        client.new_session(cwd=scene.hip_dir(), mcp_servers=scene.mcp_servers())

    def _model(self, session_id: str) -> TranscriptModel:
        return self._models.setdefault(session_id, TranscriptModel())

    def _is_current(self, session_id: str) -> bool:
        current = self._pool.current()
        return current is not None and current.session_id == session_id

    def _touch(self, session_id: str, entry_id: str) -> None:
        """Перерисовать одну запись — и только если человек смотрит эту сессию.

        Иначе стриминг в фоновой сессии заставлял бы Qt перекладывать виджеты
        ленты, которую никто не видит.
        """
        if self._is_current(session_id):
            self._transcript.refresh(entry_id)

    # ------------------------------------------------------------- ввод

    def _on_submitted(self, blocks: list) -> None:
        current = self._pool.current()
        if current is None:
            self._start_new_session()
            return
        text = " ".join(
            block.get("text", "") for block in blocks if block.get("type") == "text"
        ).strip()
        if text:
            entry = self._model(current.session_id).append_user(text)
            self._touch(current.session_id, entry.id)
            if current.title in ("", "Новый разговор"):
                current.title = text.splitlines()[0][:60]
                self._pool.mark_changed(current.session_id)
        current.busy = True
        self._composer.set_busy(True)
        shared_client().prompt(current.session_id, blocks)

    def _on_cancelled(self) -> None:
        current = self._pool.current()
        if current is not None:
            shared_client().cancel(current.session_id)

    def _on_mode_selected(self, mode_id: str) -> None:
        current = self._pool.current()
        if current is not None:
            shared_client().set_mode(current.session_id, mode_id)

    # ------------------------------------------------- оповещения и версии

    def _on_refresh_done(self, result: Any, entries: Any = ()) -> None:
        # Экран «Агенты» сам в сеть не ходит: его `refresh_from_registry`
        # синхронный, а значит на главном потоке заморозил бы Houdini ровно на
        # время таймаута, если сети нет. Записи приезжают уже готовыми.
        agents_view = getattr(self, "_agents_view", None)
        if agents_view is not None and entries:
            from .. import registry

            # Именно featured(), а не весь реестр: там под сорок записей, и
            # вываливать их художнику значит подменить выбор списком, в котором
            # он не разбирается. Всё остальное — через «Свой агент».
            agents_view.set_agents(
                registry.featured(entries),
                updates=list(getattr(result, "updates", []) or []),
            )

        for announcement in getattr(result, "announcements", []):
            if announcement.severity == "blocking":
                self._blocking.show_notice(announcement)
                self._composer.block_input(announcement.title)
                return
            self._notice.show_notice(announcement)
            return
        for update in getattr(result, "updates", []):
            self._notice.show_update(update)
            return

    def _on_notice_action(self, announcement_id: str, url: str) -> None:
        self._open_url(url)
        self._remember_seen(announcement_id)

    def _on_notice_dismissed(self, announcement_id: str) -> None:
        self._remember_seen(announcement_id)

    def _on_blocking_action(self, announcement_id: str, url: str) -> None:
        self._open_url(url)
        self._remember_seen(announcement_id)
        self._blocking.hide_notice()
        self._composer.unblock_input()

    def _open_url(self, url: str) -> None:
        if not url:
            return
        from .qt import QtGui

        QtGui.QDesktopServices.openUrl(QtCore.QUrl(url))

    def _remember_seen(self, announcement_id: str) -> None:
        if announcement_id and announcement_id not in self._settings.seen_announcements:
            self._settings.seen_announcements.append(announcement_id)
            settings_mod.save(self._settings)

    def _on_auth_method_chosen(self, method_id: str) -> None:
        shared_client().authenticate(method_id)

    def _on_logout_requested(self) -> None:
        """Выход возвращает панель туда же, откуда пришёл вход.

        Клиент после успешного logout поднимает `auth_required` с теми же
        методами, что были в `initialize`, — экран входа покажется сам, и
        отдельной ветки здесь не нужно. Если агент выйти не смог, придёт
        `error`, и человек останется там же, где был: молча делать вид, что
        вышли, нельзя.
        """
        shared_client().logout()

    def _ask_telemetry_consent_once(self) -> None:
        """Спросить про телеметрию ровно один раз за всё время.

        Флаг «спрашивали» пишется независимо от ответа — иначе человек,
        отказавшийся один раз, получал бы тот же вопрос при каждом открытии
        панели, а это уже не вопрос, а выклянчивание.
        """
        if self._settings.telemetry_consent_asked:
            return
        self._consent.ask(
            "Разрешить отправлять анонимную статистику? Только версии панели, "
            "агента и ОС плюс факты падений. Никогда — сцены, промпты и пути."
        )

    def _on_telemetry_answer(self, allowed: bool) -> None:
        self._settings = settings_mod.load()
        self._settings.telemetry = bool(allowed)
        self._settings.telemetry_consent_asked = True
        settings_mod.save(self._settings)

    def _on_settings_changed(self) -> None:
        self._settings = settings_mod.load()
        info = shared_client().agent_info()
        self._composer.set_capabilities(info, self._settings.whisper_endpoint)

    def _on_agent_chosen(self, agent_id: str) -> None:
        self._settings.default_agent = agent_id
        settings_mod.save(self._settings)
        client = shared_client()
        if client.is_running():
            client.stop()
        self._start_agent(agent_id)

    def _note(self, text: str) -> None:
        current = self._pool.current()
        session_id = current.session_id if current else "__idle__"
        entry = self._model(session_id).append_error(text)
        if current is None:
            self._transcript.set_model(self._model(session_id))
            self._transcript.refresh(None)
        else:
            self._touch(session_id, entry.id)

    # ---------------------------------------------------------- завершение

    def shutdown(self) -> None:
        """Закрытие ЭТОГО таба.

        Соединение с агентом гасим только когда закрылся последний таб: пока
        жив хоть один, разговор должен продолжаться. Иначе художник, закрывший
        одну из двух панелей, терял бы обе.
        """
        if self._closed:
            return
        self._closed = True
        _live_panels.discard(self)

        worker = self._refresh_worker
        if worker is not None:
            worker.requestInterruption()
            worker.wait(2000)
            self._refresh_worker = None

        for signal, slot in getattr(self, "_client_wiring", ()):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass
        self._client_wiring = ()

        if not _live_panels:
            global _shared_client
            if _shared_client is not None:
                _shared_client.stop()
                _shared_client = None

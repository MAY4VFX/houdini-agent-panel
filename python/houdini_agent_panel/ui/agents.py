"""Экран «Агенты»: реестр ACP плюс «Свой агент», установка/обновление/удаление.

Контракт (`docs/architecture.md` §10) отдаёт наружу всего два сигнала:
`agent_chosen` (человек выбрал агента для работы) и `closed` (обратно в чат).
Всё остальное — поставить/обновить/удалить/прогресс — экран делает сам через
`registry.py`/`runtime.py`/`settings.py` напрямую. Это то же однонаправленное
разделение слоёв, что и у `ui/announcement.py` с `announcements.Announcement`:
UI имеет право знать про реестр и рантайм, они про UI — нет (design.md,
таблица «Четыре слоя»).

Агент, недоступный на этой платформе (`AgentEntry.unavailable_reason()` не
пустая — например Kimi CLI на darwin-x86_64), показывается строкой с этой
причиной, а не прячется — прямое требование design.md.

Установка/обновление — в `QThread` (`_InstallWorker`): `runtime.install_agent`
бьёт по сети и диску, GUI-поток не должен ждать ни то, ни другое.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import registry, runtime
from .. import settings as settings_module
from .qt import QtCore, QtWidgets, Signal

if TYPE_CHECKING:
    from ..network import Fetcher
    from ..registry import AgentEntry
    from ..updates import Update


class _InstallWorker(QtCore.QThread):
    """Ставит (или обновляет) одного агента на фоновом потоке."""

    progressed = Signal(int, object, str)  # done, total|None, note
    succeeded = Signal(object)  # runtime.LaunchSpec — сам экран его не использует
    failed = Signal(str)

    def __init__(self, entry: "AgentEntry", *, fetch: "Fetcher | None", parent=None) -> None:
        super().__init__(parent)
        self._entry = entry
        self._fetch = fetch

    def run(self) -> None:  # noqa: D102 - переопределение QThread.run
        try:
            spec = runtime.install_agent(
                self._entry,
                progress=lambda done, total, note: self.progressed.emit(done, total, note),
                fetch=self._fetch,
            )
        except runtime.InstallError as exc:
            self.failed.emit(str(exc))
            return
        self.succeeded.emit(spec)


def _state_text(installed, update: "Update | None") -> str:
    if installed is None:
        return "не поставлен"
    if update is not None:
        return f"поставлен {installed.version} — есть {update.latest}"
    return f"поставлен {installed.version}"


def _clear_layout(layout: "QtWidgets.QLayout") -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            # `setParent(None)` — сразу, а не только `deleteLater()`: иначе
            # старая строка ещё числится ребёнком до следующего цикла событий
            # и попадает в `findChildren`/повторный подсчёт кнопок.
            widget.setParent(None)
            widget.deleteLater()


class _AgentRow(QtWidgets.QWidget):
    """Одна строка списка: реестровый агент или запись «Своего агента»."""

    install_requested = Signal()
    update_requested = Signal()
    uninstall_requested = Signal()
    use_requested = Signal()
    remove_custom_requested = Signal()

    def __init__(
        self,
        *,
        title: str,
        state_text: str,
        unavailable_reason: str = "",
        is_installed: bool = False,
        has_update: bool = False,
        is_custom: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)

        layout.addWidget(QtWidgets.QLabel(title), 1)

        self._state_label = QtWidgets.QLabel(unavailable_reason or state_text)
        if unavailable_reason:
            self._state_label.setStyleSheet("color: gray;")
        layout.addWidget(self._state_label)

        self._progress = QtWidgets.QProgressBar()
        self._progress.setVisible(False)
        self._progress.setMaximumWidth(120)
        layout.addWidget(self._progress)

        self.unavailable = bool(unavailable_reason)
        if self.unavailable:
            # Показан с причиной, но ставить его всё равно нечем — ни одной
            # кнопки действия (design.md: недоступный агент не прячется).
            return

        if is_custom:
            use_btn = QtWidgets.QPushButton("Использовать")
            use_btn.clicked.connect(self.use_requested.emit)
            layout.addWidget(use_btn)
            remove_btn = QtWidgets.QPushButton("Удалить")
            remove_btn.clicked.connect(self.remove_custom_requested.emit)
            layout.addWidget(remove_btn)
            return

        if not is_installed:
            install_btn = QtWidgets.QPushButton("Поставить")
            install_btn.clicked.connect(self.install_requested.emit)
            layout.addWidget(install_btn)
            return

        use_btn = QtWidgets.QPushButton("Использовать")
        use_btn.clicked.connect(self.use_requested.emit)
        layout.addWidget(use_btn)

        if has_update:
            update_btn = QtWidgets.QPushButton("Обновить")
            update_btn.clicked.connect(self.update_requested.emit)
            layout.addWidget(update_btn)

        remove_btn = QtWidgets.QPushButton("Удалить")
        remove_btn.clicked.connect(self.uninstall_requested.emit)
        layout.addWidget(remove_btn)

    # --- прогресс скачивания -------------------------------------------------

    def set_progress(self, done: int, total: int | None, note: str) -> None:
        self._progress.setVisible(True)
        if total:
            self._progress.setMaximum(total)
            self._progress.setValue(done)
        else:
            self._progress.setMaximum(0)  # total неизвестен — неопределённый прогресс
        self._progress.setToolTip(note)

    def clear_progress(self) -> None:
        self._progress.setVisible(False)

    def set_state_text(self, text: str) -> None:
        self._state_label.setText(text)


class AgentsView(QtWidgets.QWidget):
    """Экран «Агенты»: список из реестра плюс «Свой агент»."""

    agent_chosen = Signal(str)
    closed = Signal()

    def __init__(self, parent=None, *, fetch: "Fetcher | None" = None) -> None:
        super().__init__(parent)
        self._fetch = fetch
        self._entries: list["AgentEntry"] = []
        self._updates_by_target: dict[str, "Update"] = {}
        # Ссылки на живые потоки держим здесь — иначе Python соберёт QThread
        # мусорщиком раньше, чем он реально завершится.
        self._threads: list[_InstallWorker] = []

        close_button = QtWidgets.QToolButton()
        close_button.setText("←")
        close_button.setToolTip("Назад")
        close_button.clicked.connect(self.closed.emit)

        header = QtWidgets.QHBoxLayout()
        header.addWidget(close_button)
        header.addWidget(QtWidgets.QLabel("Агенты"))
        header.addStretch(1)

        self._rows_layout = QtWidgets.QVBoxLayout()
        self._custom_rows_layout = QtWidgets.QVBoxLayout()

        self._custom_name = QtWidgets.QLineEdit()
        self._custom_name.setPlaceholderText("Имя")
        self._custom_command = QtWidgets.QLineEdit()
        self._custom_command.setPlaceholderText("Команда")
        self._custom_args = QtWidgets.QLineEdit()
        self._custom_args.setPlaceholderText("Аргументы через пробел")
        add_custom_btn = QtWidgets.QPushButton("Добавить своего агента")
        add_custom_btn.clicked.connect(self._on_add_custom)

        custom_form = QtWidgets.QHBoxLayout()
        custom_form.addWidget(self._custom_name)
        custom_form.addWidget(self._custom_command)
        custom_form.addWidget(self._custom_args)
        custom_form.addWidget(add_custom_btn)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addLayout(header)
        layout.addLayout(self._rows_layout)
        layout.addWidget(QtWidgets.QLabel("Свой агент"))
        layout.addLayout(self._custom_rows_layout)
        layout.addLayout(custom_form)
        layout.addStretch(1)

        self._load_custom_agents()

    # --- наполнение данными -------------------------------------------------

    def set_agents(self, entries: list["AgentEntry"], *, updates: list["Update"] | None = None) -> None:
        """Перерисовать список реестровых агентов.

        Состояние («поставлен»/версия/обновление) читается из `settings.load()`
        при каждой перерисовке — источник правды один, дублировать его в
        памяти виджета незачем и рискованно рассинхронизировать.
        """
        self._entries = list(entries)
        self._updates_by_target = {u.target: u for u in (updates or []) if u.kind == "agent"}
        self._rebuild_registry_rows()

    def refresh_from_registry(self, *, force: bool = False) -> None:
        """Шорткат для панели: сходить в реестр самой, с тем же `fetch`, что
        передан в конструктор (тесты подсовывают `FakeFetcher`, продакшен —
        ничего, тогда `registry.fetch_registry` берёт настоящую сеть)."""
        try:
            entries = registry.fetch_registry(force=force, fetch=self._fetch)
        except registry.RegistryError:
            entries = []
        self.set_agents(entries)

    def _rebuild_registry_rows(self) -> None:
        _clear_layout(self._rows_layout)
        current_settings = settings_module.load()
        for entry in self._entries:
            reason = entry.unavailable_reason()
            installed = current_settings.installed_agents.get(entry.id)
            update = self._updates_by_target.get(entry.id)
            row = _AgentRow(
                title=f"{entry.name} {entry.version}",
                state_text=_state_text(installed, update),
                unavailable_reason=reason,
                is_installed=installed is not None,
                has_update=update is not None,
                parent=self,
            )
            row.install_requested.connect(lambda checked=False, e=entry, r=row: self._install(e, r))
            row.update_requested.connect(lambda checked=False, e=entry, r=row: self._install(e, r))
            row.uninstall_requested.connect(lambda checked=False, e=entry: self._uninstall(e.id))
            row.use_requested.connect(lambda checked=False, e=entry: self.agent_chosen.emit(e.id))
            self._rows_layout.addWidget(row)

    def _install(self, entry: "AgentEntry", row: "_AgentRow") -> None:
        worker = _InstallWorker(entry, fetch=self._fetch, parent=self)
        self._threads.append(worker)
        worker.progressed.connect(row.set_progress)
        worker.succeeded.connect(lambda _spec, e=entry, r=row: self._on_installed(e, r))
        worker.failed.connect(lambda message, r=row: self._on_install_failed(r, message))
        worker.finished.connect(lambda w=worker: self._forget_thread(w))
        worker.start()

    def _forget_thread(self, worker: "_InstallWorker") -> None:
        # `finished` уходит непосредственно ПЕРЕД тем, как поток реально
        # остановится — уронить последнюю Python-ссылку прямо здесь, не
        # дождавшись `wait()`, значит рискнуть удалить QThread, пока ОС-поток
        # ещё физически не присоединился (падение, не всегда воспроизводимое).
        worker.wait()
        if worker in self._threads:
            self._threads.remove(worker)

    def _on_install_failed(self, row: "_AgentRow", message: str) -> None:
        row.clear_progress()
        row.set_state_text(f"ошибка: {message}")

    def _on_installed(self, entry: "AgentEntry", row: "_AgentRow") -> None:
        row.clear_progress()
        current = settings_module.load()
        kind = "npx" if entry.needs_node else "binary"
        current.installed_agents[entry.id] = settings_module.InstalledAgent(
            agent_id=entry.id,
            version=entry.version,
            kind=kind,
            installed_at=settings_module.InstalledAgent.now(),
        )
        settings_module.save(current)
        self._rebuild_registry_rows()

    def _uninstall(self, agent_id: str) -> None:
        runtime.uninstall_agent(agent_id)
        current = settings_module.load()
        current.installed_agents.pop(agent_id, None)
        settings_module.save(current)
        self._rebuild_registry_rows()

    # --- «Свой агент» --------------------------------------------------------

    def _load_custom_agents(self) -> None:
        _clear_layout(self._custom_rows_layout)
        current = settings_module.load()
        for agent in current.custom_agents:
            row = _AgentRow(title=f"{agent.name} ({agent.command})", state_text="свой агент", is_custom=True, parent=self)
            row.use_requested.connect(lambda checked=False, a=agent: self.agent_chosen.emit(a.id))
            row.remove_custom_requested.connect(lambda checked=False, a=agent: self._remove_custom(a.id))
            self._custom_rows_layout.addWidget(row)

    def _on_add_custom(self) -> None:
        name = self._custom_name.text().strip()
        command = self._custom_command.text().strip()
        if not name or not command:
            return
        args = self._custom_args.text().split()
        current = settings_module.load()
        agent_id = f"custom:{name}"
        current.custom_agents = [a for a in current.custom_agents if a.id != agent_id]
        current.custom_agents.append(settings_module.CustomAgent(id=agent_id, name=name, command=command, args=args))
        settings_module.save(current)
        self._custom_name.clear()
        self._custom_command.clear()
        self._custom_args.clear()
        self._load_custom_agents()

    def _remove_custom(self, agent_id: str) -> None:
        current = settings_module.load()
        current.custom_agents = [a for a in current.custom_agents if a.id != agent_id]
        settings_module.save(current)
        self._load_custom_agents()


__all__ = ["AgentsView"]

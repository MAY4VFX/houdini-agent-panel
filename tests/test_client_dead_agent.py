"""Агент, который не отвечает на initialize.

Регрессия из живой Houdini: панель писала «Запускаю claude-acp…» и висела
бесконечно. Процесс агента умирал сразу после старта — путь к `npx-cli.js`
резолвился в несуществующий файл на машине с Homebrew, — каналы закрывались,
ответа на `initialize` не могло быть в принципе, а клиент всё ждал.

Причина в тот раз была одна, но ждать вечно нельзя ни по какой причине.
Поэтому тесты проверяют не конкретный сломанный путь, а поведение клиента:
процесс умер или молчит — панель обязана сказать об этом.
"""

from __future__ import annotations

import sys

import pytest

from houdini_agent_panel.client import AcpClient
from houdini_agent_panel.runtime import LaunchSpec


def _wait_for(qapp, predicate, timeout_ms: int = 15000) -> bool:
    from houdini_agent_panel.ui.qt import QtCore

    timer = QtCore.QElapsedTimer()
    timer.start()
    while timer.elapsed() < timeout_ms:
        qapp.processEvents()
        if predicate():
            return True
        QtCore.QThread.msleep(20)
    return False


def test_agent_that_dies_immediately_reports_instead_of_hanging(qapp):
    client = AcpClient()
    failures: list[str] = []
    client.failed.connect(failures.append)

    # Процесс, который мгновенно умирает с ненулевым кодом и пишет в stderr —
    # ровно то, что делает `node <несуществующий-файл>.js`.
    spec = LaunchSpec(
        command=sys.executable,
        args=["-c", "import sys; sys.stderr.write('cannot find module npx-cli.js\\n'); sys.exit(1)"],
        env={},
    )
    client.start(spec, cwd=".")

    assert _wait_for(qapp, lambda: bool(failures)), "клиент завис вместо того, чтобы сообщить об ошибке"
    assert not client.is_running()

    message = failures[0]
    # Что именно выиграет гонку — обрыв соединения от SDK или наш собственный
    # надзор за процессом — зависит от того, кто успел первым, и это не важно.
    # Важно, что сообщение объясняет причину, а суть почти всегда в stderr:
    # не найден файл, нет прав, не хватает переменной окружения.
    assert "npx-cli.js" in message, f"хвост stderr должен попадать в сообщение: {message!r}"
    assert len(message.splitlines()) > 1, f"одна строка без деталей бесполезна: {message!r}"

    client.stop()


def test_agent_that_starts_but_never_answers_hits_the_ceiling(qapp, monkeypatch):
    """Процесс жив и молчит — тоже не повод ждать вечно."""
    from houdini_agent_panel import client as client_module

    monkeypatch.setattr(client_module, "_CONNECT_TIMEOUT", 1.0)

    client = AcpClient()
    failures: list[str] = []
    client.failed.connect(failures.append)

    spec = LaunchSpec(
        command=sys.executable,
        args=["-c", "import time; time.sleep(60)"],
        env={},
    )
    client.start(spec, cwd=".")

    assert _wait_for(qapp, lambda: bool(failures), timeout_ms=15000), "клиент завис на молчащем агенте"
    assert "initialize" in failures[0]

    client.stop()

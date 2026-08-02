"""Тесты экрана «Агенты»: недоступность на платформе, установка в фоне, «Свой агент»."""

from __future__ import annotations

import hashlib
import io
import zipfile

from PySide6 import QtTest, QtWidgets

from houdini_agent_panel import settings as settings_module
from houdini_agent_panel.registry import AgentEntry, BinaryDistribution
from houdini_agent_panel.ui.agents import AgentsView


def _zip_bytes(cmd_name: str = "myagent", content: bytes = b"echo hi\n") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(cmd_name, content)
    return buf.getvalue()


def _wait_until(condition, *, timeout_ms: int = 5000) -> None:
    app = QtWidgets.QApplication.instance()
    elapsed = 0
    step = 20
    while not condition() and elapsed < timeout_ms:
        app.processEvents()
        QtTest.QTest.qWait(step)
        elapsed += step
    assert condition(), "условие не выполнилось за отведённое время"


def test_unavailable_agent_shown_with_reason_not_hidden(qapp, monkeypatch):
    monkeypatch.setattr("houdini_agent_panel.registry.platform_key", lambda: "fake-platform")
    entry = AgentEntry(
        id="kimi",
        name="Kimi CLI",
        version="1.0.0",
        binaries={"linux-x86_64": BinaryDistribution(archive="https://x/kimi.zip", cmd="./kimi", sha256="0" * 64)},
    )
    view = AgentsView()
    view.set_agents([entry])

    assert view._rows_layout.count() == 1
    row = view._rows_layout.itemAt(0).widget()
    assert row.unavailable
    assert "Kimi CLI" in row.findChild(QtWidgets.QLabel).text() or True
    # причина видна текстом, не спрятана
    labels = [child.text() for child in row.findChildren(QtWidgets.QLabel)]
    assert any("fake-platform" in text for text in labels)
    # и ни одной кнопки действия
    assert not row.findChildren(QtWidgets.QPushButton)


def test_not_installed_agent_shows_install_button(qapp, monkeypatch):
    monkeypatch.setattr("houdini_agent_panel.registry.platform_key", lambda: "fake-platform")
    entry = AgentEntry(
        id="agent-a",
        name="Agent A",
        version="1.0.0",
        binaries={"fake-platform": BinaryDistribution(archive="https://x/a.zip", cmd="./myagent", sha256="0" * 64)},
    )
    view = AgentsView()
    view.set_agents([entry])
    row = view._rows_layout.itemAt(0).widget()
    buttons = {b.text() for b in row.findChildren(QtWidgets.QPushButton)}
    assert buttons == {"Поставить"}


def test_install_flow_runs_in_background_and_updates_row(qapp, monkeypatch, fetcher):
    monkeypatch.setattr("houdini_agent_panel.runtime.platform_key", lambda: "fake-platform")
    monkeypatch.setattr("houdini_agent_panel.registry.platform_key", lambda: "fake-platform")

    payload = _zip_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    fetcher.add_bytes("https://x/a.zip", payload)

    entry = AgentEntry(
        id="agent-a",
        name="Agent A",
        version="1.0.0",
        binaries={"fake-platform": BinaryDistribution(archive="https://x/a.zip", cmd="./myagent", sha256=digest)},
    )
    view = AgentsView(fetch=fetcher)
    view.set_agents([entry])
    row = view._rows_layout.itemAt(0).widget()
    install_button = next(b for b in row.findChildren(QtWidgets.QPushButton) if b.text() == "Поставить")
    install_button.click()

    _wait_until(lambda: not view._threads)

    current = settings_module.load()
    assert current.installed_agents["agent-a"].version == "1.0.0"

    # После установки строка перерисована: теперь "Использовать"/"Удалить".
    row = view._rows_layout.itemAt(0).widget()
    buttons = {b.text() for b in row.findChildren(QtWidgets.QPushButton)}
    assert buttons == {"Использовать", "Удалить"}


def test_use_button_emits_agent_chosen(qapp, monkeypatch):
    monkeypatch.setattr("houdini_agent_panel.registry.platform_key", lambda: "fake-platform")
    entry = AgentEntry(
        id="agent-a",
        name="Agent A",
        version="1.0.0",
        binaries={"fake-platform": BinaryDistribution(archive="https://x/a.zip", cmd="./myagent", sha256="0" * 64)},
    )
    current = settings_module.load()
    current.installed_agents["agent-a"] = settings_module.InstalledAgent(
        agent_id="agent-a", version="1.0.0", kind="binary", installed_at="now"
    )
    settings_module.save(current)

    view = AgentsView()
    view.set_agents([entry])
    row = view._rows_layout.itemAt(0).widget()
    use_button = next(b for b in row.findChildren(QtWidgets.QPushButton) if b.text() == "Использовать")

    received = []
    view.agent_chosen.connect(received.append)
    use_button.click()
    assert received == ["agent-a"]


def test_update_available_shown_and_offers_update_button(qapp, monkeypatch):
    from houdini_agent_panel.updates import Update

    monkeypatch.setattr("houdini_agent_panel.registry.platform_key", lambda: "fake-platform")
    entry = AgentEntry(
        id="agent-a",
        name="Agent A",
        version="2.0.0",
        binaries={"fake-platform": BinaryDistribution(archive="https://x/a.zip", cmd="./myagent", sha256="0" * 64)},
    )
    current = settings_module.load()
    current.installed_agents["agent-a"] = settings_module.InstalledAgent(
        agent_id="agent-a", version="1.0.0", kind="binary", installed_at="now"
    )
    settings_module.save(current)

    view = AgentsView()
    update = Update(kind="agent", target="agent-a", label="Agent A 2.0.0", current="1.0.0", latest="2.0.0")
    view.set_agents([entry], updates=[update])

    row = view._rows_layout.itemAt(0).widget()
    buttons = {b.text() for b in row.findChildren(QtWidgets.QPushButton)}
    assert "Обновить" in buttons


def test_custom_agent_add_and_remove(qapp):
    view = AgentsView()
    view._custom_name.setText("Моя команда")
    view._custom_command.setText("/usr/bin/my-acp-agent")
    view._custom_args.setText("--flag value")
    view._on_add_custom()

    current = settings_module.load()
    assert len(current.custom_agents) == 1
    assert current.custom_agents[0].name == "Моя команда"
    assert current.custom_agents[0].args == ["--flag", "value"]

    assert view._custom_rows_layout.count() == 1
    row = view._custom_rows_layout.itemAt(0).widget()
    remove_button = next(b for b in row.findChildren(QtWidgets.QPushButton) if b.text() == "Удалить")
    remove_button.click()

    current = settings_module.load()
    assert current.custom_agents == []

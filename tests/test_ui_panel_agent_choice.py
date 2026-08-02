"""«Добавил своего агента → сразу нажал Использовать».

Регрессия, найденная только живой проверкой в Houdini: панель держит снимок
настроек с момента открытия, а экран «Агенты» пишет добавленного агента в файл
напрямую. Сохранение снимка поверх стирало свежую запись, и запуск падал с
«агента нет ни в реестре, ни среди своих» — при том что после перезапуска
Houdini всё работало, что и делало баг таким коварным.

Отдельным файлом от test_ui_panel.py: тот параллельно правит другая сессия.
"""

from __future__ import annotations

import pytest

from houdini_agent_panel import settings as settings_mod
from houdini_agent_panel.ui import panel as panel_mod


@pytest.fixture(autouse=True)
def isolated_panel_state(qapp, monkeypatch):
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


def test_choosing_a_just_added_custom_agent_does_not_erase_it(qapp, monkeypatch):
    widget = panel_mod.AgentPanel()
    qapp.processEvents()

    # Экран «Агенты» пишет в файл напрямую, мимо снимка панели — ровно как
    # AgentsView._on_add_custom.
    fresh = settings_mod.load()
    fresh.custom_agents.append(
        settings_mod.CustomAgent(
            id="custom:my", name="Мой", command="/usr/bin/env", args=["python3"]
        )
    )
    settings_mod.save(fresh)

    # Панель не должна доходить до реального запуска процесса.
    started: list[str] = []
    monkeypatch.setattr(widget, "_start_agent", lambda agent_id: started.append(agent_id))

    widget._on_agent_chosen("custom:my")

    on_disk = settings_mod.load()
    assert [a.id for a in on_disk.custom_agents] == ["custom:my"], (
        "выбор агента затёр только что добавленную запись"
    )
    assert on_disk.default_agent == "custom:my"
    assert started == ["custom:my"]

    # И собственный снимок панели теперь свежий — _launch_spec найдёт агента.
    assert [a.id for a in widget._settings.custom_agents] == ["custom:my"]

    widget.shutdown()


def test_remember_seen_does_not_clobber_concurrent_settings_writes(qapp):
    widget = panel_mod.AgentPanel()
    qapp.processEvents()

    fresh = settings_mod.load()
    fresh.custom_agents.append(
        settings_mod.CustomAgent(id="custom:other", name="Другой", command="/bin/true")
    )
    settings_mod.save(fresh)

    widget._remember_seen("ann-1")

    on_disk = settings_mod.load()
    assert "ann-1" in on_disk.seen_announcements
    assert [a.id for a in on_disk.custom_agents] == ["custom:other"]

    widget.shutdown()

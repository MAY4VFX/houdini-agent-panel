"""Общие фикстуры.

Два правила, которые здесь и держатся: ни один тест не пишет в настоящую папку
данных пользователя и ни один тест не ходит в сеть.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "python"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from houdini_agent_panel import paths  # noqa: E402


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch) -> Path:
    """Уводит всю запись панели в tmp_path — автоматически, для каждого теста.

    Автоматически, а не по запросу, потому что забытая фикстура здесь означает
    тест, который молча гадит в ``~/Library/Application Support`` разработчика.
    """
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(root))
    return root


class FakeFetcher:
    """Подстановка вместо сети.

    Считает вызовы: тест «с выключенными тумблерами панель молчит» проверяется
    именно счётчиком, а не отсутствием исключения.
    """

    def __init__(self, responses: dict[str, bytes] | None = None) -> None:
        self.responses: dict[str, bytes] = dict(responses or {})
        self.calls: list[str] = []

    def add_json(self, url: str, payload) -> None:
        self.responses[url] = json.dumps(payload).encode("utf-8")

    def add_bytes(self, url: str, payload: bytes) -> None:
        self.responses[url] = payload

    def __call__(self, url: str, *, timeout: float = 30.0) -> bytes:
        self.calls.append(url)
        try:
            return self.responses[url]
        except KeyError:
            from houdini_agent_panel.network import NetworkError

            raise NetworkError(f"{url}: в FakeFetcher нет ответа на этот адрес") from None


@pytest.fixture
def fetcher() -> FakeFetcher:
    return FakeFetcher()


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Страховка: настоящий сетевой вызов из теста — падение с внятным текстом."""

    def explode(*args, **kwargs):
        raise AssertionError(
            "тест попытался выйти в сеть; передай fetch=FakeFetcher() в проверяемую функцию"
        )

    monkeypatch.setattr("houdini_agent_panel.network.urlopen_fetch", explode)
    monkeypatch.setattr("houdini_agent_panel.network.stream_fetch", explode)


@pytest.fixture(scope="session")
def qapp():
    """Один QApplication на прогон: второй экземпляр Qt не разрешает."""
    from houdini_agent_panel.ui.qt import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app

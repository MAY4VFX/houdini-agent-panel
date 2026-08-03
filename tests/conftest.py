"""Shared fixtures.

Two rules are enforced here: no test writes to the real user data folder,
and no test reaches the network.
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
    """Redirects all of the panel's writes into tmp_path — automatically, for every test.

    Automatic rather than opt-in, because a forgotten fixture here means a
    test that silently litters the developer's
    ``~/Library/Application Support``.
    """
    root = tmp_path / "data"
    root.mkdir()
    monkeypatch.setenv(paths.DATA_DIR_ENV, str(root))
    return root


class FakeFetcher:
    """A stand-in for the network.

    Counts calls: the "panel stays silent with both toggles off" test is
    verified by exactly this counter, not by the absence of an exception.
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

            raise NetworkError(f"{url}: FakeFetcher has no response registered for this address") from None


@pytest.fixture
def fetcher() -> FakeFetcher:
    return FakeFetcher()


@pytest.fixture(autouse=True)
def fresh_fx_port_cache():
    """`scene` remembers the HTTP port scan for the life of the process.

    That's deliberate in production (the scan costs 16 seconds on the main
    thread), but process-wide state leaks between tests, so it's cleared
    around every one of them.
    """
    from houdini_agent_panel import scene

    scene.reset_port_cache_for_tests()
    yield
    scene.reset_port_cache_for_tests()


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Safety net: a real network call from a test fails with a clear message."""

    def explode(*args, **kwargs):
        raise AssertionError(
            "a test tried to reach the network; pass fetch=FakeFetcher() into the function under test"
        )

    monkeypatch.setattr("houdini_agent_panel.network.urlopen_fetch", explode)
    monkeypatch.setattr("houdini_agent_panel.network.stream_fetch", explode)


@pytest.fixture(scope="session")
def qapp():
    """One QApplication per run: Qt doesn't allow a second instance."""
    from houdini_agent_panel.ui.qt import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app

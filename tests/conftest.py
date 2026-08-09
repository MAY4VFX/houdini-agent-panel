"""Shared fixtures.

Three rules are enforced here: no test writes to the real user data folder,
no test reaches the network, and no test opens a window on the developer's
screen.

That last one was learned the hard way. Fifty-eight `show()` calls across the
suite, a suite run dozens of times a day, and nothing anywhere set Qt's
platform — so every run put real windows on a real desktop, and any run that
did not exit cleanly left them there. The person whose machine it was
eventually reported hundreds of stray panels and a computer crawling, and it
took an embarrassingly long detour through the macOS window server to work
out that the windows were ours and the tests had put them there.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# BEFORE anything imports Qt: once a QGuiApplication exists the platform
# plugin is fixed, and setting this afterwards does nothing at all. Assigned,
# not `setdefault`ed — an inherited value is exactly the accident being
# prevented, so the test suite decides this and nothing else does.
os.environ["QT_QPA_PLATFORM"] = "offscreen"

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_ROOT = REPO_ROOT / "python"
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from houdini_agent_panel import paths, proxy  # noqa: E402


@pytest.fixture(autouse=True)
def no_ambient_proxy(monkeypatch) -> None:
    """The developer's own proxy is not part of the test fixture.

    `proxy.py` is built to INHERIT a machine-wide `HTTPS_PROXY`/CA bundle —
    a studio that exports them is correctly configured and the panel passes
    them through. That is also why eight tests went red the moment the
    suite ran on such a machine: they assert on the launch environment a
    panel builds, and the machine's own proxy was legitimately in it. The
    behaviour is right; reading it from outside the test is what is wrong.
    A test that cares about inheritance sets the variables itself, and its
    own `monkeypatch.setenv` still wins over this.
    """
    for name in (*proxy.PROXY_VARS, *proxy.CA_VARS, "NO_PROXY", "no_proxy"):
        monkeypatch.delenv(name, raising=False)


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

    That's deliberate in production (the scan costs up to a second on the
    main thread, even parallelized), but process-wide state leaks between
    tests, so it's cleared around every one of them.
    """
    from houdini_agent_panel import scene

    scene.reset_port_cache_for_tests()
    yield
    scene.reset_port_cache_for_tests()


@pytest.fixture(autouse=True)
def fresh_registry_memory_cache():
    """`registry.fetch_registry` keeps a parsed copy in memory for the life
    of the process, on top of the on-disk cache, to avoid re-reading/parsing
    the registry file on every call within one panel session. Cleared around
    every test, same reasoning as `fresh_fx_port_cache`: production wants the
    memory to persist, tests must not leak it across `HAP_DATA_DIR`s."""
    from houdini_agent_panel import registry

    registry.reset_memory_cache_for_tests()
    yield
    registry.reset_memory_cache_for_tests()


@pytest.fixture(autouse=True)
def fresh_manifest_cache():
    """`runtime` caches each agent's installed version in memory — it's
    read once per agent on every repaint of the "Agents" screen otherwise.
    Cleared around every test for the same reason as the fixtures above."""
    from houdini_agent_panel import runtime

    runtime.reset_manifest_cache_for_tests()
    yield
    runtime.reset_manifest_cache_for_tests()


@pytest.fixture(autouse=True)
def fresh_system_node_cache():
    """`node.find_system_node` caches its subprocess lookup in memory for
    the life of the process. Cleared around every test for the same reason
    as the fixtures above."""
    from houdini_agent_panel import node

    node.reset_system_node_cache_for_tests()
    yield
    node.reset_system_node_cache_for_tests()


@pytest.fixture(autouse=True)
def no_real_network(monkeypatch):
    """Safety net: a real network call from a test fails with a clear message."""

    def explode(*args, **kwargs):
        raise AssertionError(
            "a test tried to reach the network; pass fetch=FakeFetcher() into the function under test"
        )

    monkeypatch.setattr("houdini_agent_panel.network.urlopen_fetch", explode)
    monkeypatch.setattr("houdini_agent_panel.network.stream_fetch", explode)
    # `token_check.verify` builds its own request (it needs headers that
    # `urlopen_fetch` has no way to carry), so the two guards above never
    # see it. Left as `VALID` rather than exploding: every existing
    # sign-in test asserts on `token_captured`, and the check sits
    # directly in front of that signal — a test suite that had to opt in
    # to it one file at a time would be a suite where the next new test
    # quietly reaches api.anthropic.com. Tests that care about the check
    # itself drive `token_check.verify` directly (`test_token_check.py`)
    # or override this.
    monkeypatch.setattr(
        "houdini_agent_panel.token_check.verify",
        lambda token, **kwargs: __import__(
            "houdini_agent_panel.token_check", fromlist=["VALID"]
        ).VALID,
    )


@pytest.fixture(scope="session")
def qapp():
    """One QApplication per run: Qt doesn't allow a second instance."""
    from houdini_agent_panel.ui.qt import QtWidgets

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app

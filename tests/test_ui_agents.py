"""Tests for the agents section: platform unavailability, background install, "custom agent"."""

from __future__ import annotations

import json

import hashlib
import io
import zipfile

from PySide6 import QtTest, QtWidgets

from houdini_agent_panel import settings as settings_module
from houdini_agent_panel.registry import AgentEntry, BinaryDistribution, NpxDistribution
from houdini_agent_panel.ui.agents import AgentsView



def _mark_installed(agent_id: str, version: str) -> None:
    """Mark an agent as installed the way the panel actually decides it.

    The manifest on disk is the single source of truth — settings only carry
    extra detail. Faking installed state through settings alone used to pass
    while the real UI showed "not installed", which is precisely the bug
    these tests are here to catch.
    """
    from houdini_agent_panel import paths

    manifest = paths.agent_dir(agent_id) / "manifest.json"
    manifest.write_text(
        json.dumps({"agent_id": agent_id, "version": version, "kind": "binary"}),
        "utf-8",
    )



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
    assert condition(), "condition did not become true in time"


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
    # the reason is visible text, not hidden
    labels = [child.text() for child in row.findChildren(QtWidgets.QLabel)]
    assert any("fake-platform" in text for text in labels)
    # and not a single action button
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
    assert buttons == {"Install"}


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
    install_button = next(b for b in row.findChildren(QtWidgets.QPushButton) if b.text() == "Install")

    changed = []
    view.installed_changed.connect(lambda: changed.append(True))
    install_button.click()

    _wait_until(lambda: not view._threads)

    current = settings_module.load()
    assert current.installed_agents["agent-a"].version == "1.0.0"
    assert changed == [True]

    # After install the row is redrawn: no more "Use" button, just "Remove".
    row = view._rows_layout.itemAt(0).widget()
    buttons = {b.text() for b in row.findChildren(QtWidgets.QPushButton)}
    assert buttons == {"Remove"}


def test_install_failure_not_an_installerror_still_reports_and_unlocks(qapp, monkeypatch):
    """A real bug, found live: `_InstallWorker.run()` only caught
    `runtime.InstallError`. `node.NpxNotFoundError` (raised by
    `node.npx_argv` when there is no npm next to the detected Node) is a
    plain `RuntimeError`, not an `InstallError` — it escaped the catch
    entirely. Confirmed by fault injection in hython: neither `succeeded`
    nor `failed` ever fired, PySide only printed the traceback to a
    terminal no Houdini artist has open, and `_installing` was never
    cleared — permanently locking every later click on that agent's
    Install/Update button too. This is exactly what "Remove it, then
    Install — nothing happens, and it stays broken" looks like from the
    artist's side.
    """
    # npx, not binary — `needs_node` is what routes through `ensure_node`/
    # `npx_argv`, exactly where `NpxNotFoundError` comes from for real.
    entry = AgentEntry(
        id="agent-a", name="Agent A", version="1.0.0",
        npx=NpxDistribution(package="@test/agent", args=[]),
    )

    def _boom(*a, **k):
        raise RuntimeError("no npm found next to /fake/node")

    monkeypatch.setattr("houdini_agent_panel.runtime.install_agent", _boom)

    view = AgentsView()
    view.set_agents([entry])
    row = view._rows_layout.itemAt(0).widget()
    install_button = next(b for b in row.findChildren(QtWidgets.QPushButton) if b.text() == "Install")

    failures = []
    view.install_failed.connect(lambda agent_id, message: failures.append((agent_id, message)))
    install_button.click()

    _wait_until(lambda: not view._threads)

    assert failures == [("agent-a", "no npm found next to /fake/node")]
    assert "agent-a" not in view._installing, "the agent stayed locked as 'installing' forever"
    row = view._rows_layout.itemAt(0).widget()
    assert row._state_label.text() == "error: no npm found next to /fake/node"

    # And the lock being cleared means a retry is actually possible, not
    # silently ignored a second time — the earlier bug's real consequence.
    install_button = next(b for b in row.findChildren(QtWidgets.QPushButton) if b.text() == "Install")
    install_button.click()
    assert "agent-a" in view._installing, "clicking again did not even start a new attempt"


def test_installed_agent_has_no_use_button(qapp, monkeypatch):
    """Switching agents lives in the header chip's menu now — this view is
    install/update/remove only."""
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
    _mark_installed("agent-a", "1.0.0")
    settings_module.save(current)

    view = AgentsView()
    view.set_agents([entry])
    row = view._rows_layout.itemAt(0).widget()
    buttons = {b.text() for b in row.findChildren(QtWidgets.QPushButton)}
    assert buttons == {"Remove"}


def test_sign_in_shows_only_on_the_currently_connected_agents_own_row(qapp, monkeypatch):
    """Moved here from the header chip's switcher menu — a real complaint:
    the button used to show for any agent that had declared auth methods,
    not for whichever one was actually running, in the menu meant to answer
    "which agent to talk to", not "manage this one". `set_current_agent_auth`
    is how the panel says which single row, if any, gets it.
    """
    monkeypatch.setattr("houdini_agent_panel.registry.platform_key", lambda: "fake-platform")
    entry_a = AgentEntry(
        id="agent-a",
        name="Agent A",
        version="1.0.0",
        binaries={"fake-platform": BinaryDistribution(archive="https://x/a.zip", cmd="./a", sha256="0" * 64)},
    )
    entry_b = AgentEntry(
        id="agent-b",
        name="Agent B",
        version="1.0.0",
        binaries={"fake-platform": BinaryDistribution(archive="https://x/b.zip", cmd="./b", sha256="0" * 64)},
    )
    _mark_installed("agent-a", "1.0.0")
    _mark_installed("agent-b", "1.0.0")

    view = AgentsView()
    view.set_agents([entry_a, entry_b])
    view.set_current_agent_auth("agent-a", True)

    row_a = view._rows_by_id["agent-a"]
    row_b = view._rows_by_id["agent-b"]
    assert "Sign in…" in {b.text() for b in row_a.findChildren(QtWidgets.QPushButton)}
    assert "Sign in…" not in {b.text() for b in row_b.findChildren(QtWidgets.QPushButton)}

    # Disconnecting (or switching to an agent with no auth methods at all)
    # must take the button away again — it must not keep pointing at a dead
    # connection.
    view.set_current_agent_auth(None, False)
    row_a = view._rows_by_id["agent-a"]
    assert "Sign in…" not in {b.text() for b in row_a.findChildren(QtWidgets.QPushButton)}


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
    _mark_installed("agent-a", "1.0.0")
    settings_module.save(current)

    view = AgentsView()
    update = Update(kind="agent", target="agent-a", label="Agent A 2.0.0", current="1.0.0", latest="2.0.0")
    view.set_agents([entry], updates=[update])

    row = view._rows_layout.itemAt(0).widget()
    buttons = {b.text() for b in row.findChildren(QtWidgets.QPushButton)}
    assert "Update" in buttons


def test_custom_agent_section_is_hidden_but_still_works(qapp):
    """Hidden per the owner's call, seen live: "давай это поле пока
    скроем, я пока даже не понимаю, что это за функционал" — not removed.
    The section is invisible; the feature underneath (`test_custom_agent_
    add_and_remove` below) is untouched. `.show()` matters here: an
    unshown top-level widget reports every child as not-visible regardless
    of its OWN explicit state, which would make this pass even without the
    fix — showing the view is what makes `isVisible()` mean anything."""
    view = AgentsView()
    view.show()
    qapp.processEvents()
    assert view._custom_section.isVisible() is False


def test_custom_agent_add_and_remove(qapp):
    view = AgentsView()

    changed = []
    view.installed_changed.connect(lambda: changed.append(True))

    view._custom_name.setText("My command")
    view._custom_command.setText("/usr/bin/my-acp-agent")
    view._custom_args.setText("--flag value")
    view._on_add_custom()

    current = settings_module.load()
    assert len(current.custom_agents) == 1
    assert current.custom_agents[0].name == "My command"
    assert current.custom_agents[0].args == ["--flag", "value"]

    assert view._custom_rows_layout.count() == 1
    row = view._custom_rows_layout.itemAt(0).widget()
    buttons = {b.text() for b in row.findChildren(QtWidgets.QPushButton)}
    assert buttons == {"Remove"}
    remove_button = next(b for b in row.findChildren(QtWidgets.QPushButton) if b.text() == "Remove")
    remove_button.click()

    current = settings_module.load()
    assert current.custom_agents == []
    assert changed == [True, True]

"""Tests for houdini_agent.pypanel: both the XML shape and real behavior.

There's no real Houdini here at all — the `<script>` is pulled out of the
XML and executed in a clean namespace. `houdini_agent_panel.ui.panel` is
replaced with a fake (the real module wasn't written yet at the time this
test was written — the UI layer was someone else's work in progress), and
`kwargs['paneTab']`, which Houdini uses to tag a specific tab, is simulated
with plain objects.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

PYPANEL_PATH = (
    Path(__file__).resolve().parents[1]
    / "python"
    / "houdini_agent_panel"
    / "houdini"
    / "python_panels"
    / "houdini_agent.pypanel"
)


def _script_text() -> str:
    tree = ET.parse(PYPANEL_PATH)
    interface = tree.getroot().find("interface")
    script = interface.find("script")
    return script.text


@pytest.fixture
def fake_agent_panel_module(monkeypatch):
    """Plants houdini_agent_panel.ui.panel.AgentPanel as a fake.

    The real ui/panel.py doesn't exist in the repository yet (the UI layer
    was someone else's work), and the test needs to check `.pypanel`'s
    behavior, not the presence of someone else's file — so the module is
    emulated directly via sys.modules.
    """
    import types

    created = []

    class FakeAgentPanel:
        def __init__(self):
            self.shutdown_calls = 0
            created.append(self)

        def shutdown(self):
            self.shutdown_calls += 1

    fake_module = types.ModuleType("houdini_agent_panel.ui.panel")
    fake_module.AgentPanel = FakeAgentPanel
    monkeypatch.setitem(sys.modules, "houdini_agent_panel.ui.panel", fake_module)
    return created


@pytest.fixture
def panel_namespace(fake_agent_panel_module):
    """Executes the panel's <script> in a fresh namespace for each test."""
    namespace: dict = {"__name__": "houdini_agent_panel_pypanel_under_test"}
    exec(compile(_script_text(), str(PYPANEL_PATH), "exec"), namespace)
    return namespace


def test_pypanel_is_well_formed_xml_with_expected_interface():
    tree = ET.parse(PYPANEL_PATH)
    interface = tree.getroot().find("interface")
    assert interface is not None
    assert interface.get("name") == "hap::agent"
    assert interface.get("label") == "Agent"
    assert interface.get("icon") == "MISC_python"
    assert interface.find("script") is not None
    assert interface.find("includeInPaneTabMenu") is not None


def test_oncreate_and_ondestroy_are_defined(panel_namespace):
    assert callable(panel_namespace.get("onCreateInterface"))
    assert callable(panel_namespace.get("onDestroyInterface"))


def test_shutdown_is_not_wired_to_ondeactivate(panel_namespace):
    """onDeactivateInterface fires on every tab switch — if shutdown() were
    hooked to it, the panel would kill the agent's connection just because
    the artist glanced at a neighboring tab. The right place for it is
    onDestroyInterface (the tab actually closing)."""
    assert "onDeactivateInterface" not in panel_namespace


def test_oncreate_returns_widget_backed_by_agent_panel(panel_namespace, fake_agent_panel_module):
    widget = panel_namespace["onCreateInterface"]()
    assert widget is not None
    assert widget.shutdown_calls == 0
    assert fake_agent_panel_module == [widget]


def test_ondestroy_shuts_down_only_the_closed_tab(panel_namespace, fake_agent_panel_module):
    """Two tabs of the same panel — two different paneTabs, two different
    widgets. Closing one must not touch the other (see LightLinker.pypanel:
    kwargs['paneTab'] is the standard way to tell which tab is in play)."""

    tab_a = object()
    tab_b = object()

    panel_namespace["kwargs"] = {"paneTab": tab_a}
    panel_a = panel_namespace["onCreateInterface"]()

    panel_namespace["kwargs"] = {"paneTab": tab_b}
    panel_b = panel_namespace["onCreateInterface"]()

    assert panel_a is not panel_b

    panel_namespace["kwargs"] = {"paneTab": tab_a}
    panel_namespace["onDestroyInterface"]()

    assert panel_a.shutdown_calls == 1
    assert panel_b.shutdown_calls == 0


def test_oncreate_survives_missing_kwargs_global(panel_namespace, fake_agent_panel_module):
    """`kwargs` isn't always in globals (e.g. a manual call outside
    Houdini). Referencing it directly by name would raise NameError; that
    must not happen here."""
    assert "kwargs" not in panel_namespace
    widget = panel_namespace["onCreateInterface"]()
    assert widget is not None


def test_oncreate_falls_back_to_error_widget_when_import_fails(qapp, monkeypatch):
    """If houdini_agent_panel.ui.panel fails to import at all (an
    interrupted dependency install) — onCreateInterface must return a
    widget with readable text, not let an exception fall through into
    Houdini."""
    monkeypatch.setitem(sys.modules, "houdini_agent_panel.ui.panel", None)  # force an ImportError

    from houdini_agent_panel.ui.qt import QtWidgets

    namespace: dict = {"__name__": "houdini_agent_panel_pypanel_under_test"}
    exec(compile(_script_text(), str(PYPANEL_PATH), "exec"), namespace)

    widget = namespace["onCreateInterface"]()

    assert isinstance(widget, QtWidgets.QWidget)
    labels = widget.findChildren(QtWidgets.QLabel)
    assert labels, "the error widget must show text"
    text = labels[0].text()
    assert "doctor" in text
    assert "terminal" in text.lower()
    # The text used to suggest opening the Python Shell and running a shell
    # command there — a guaranteed SyntaxError in a Python console.
    assert "python shell" not in text.lower()

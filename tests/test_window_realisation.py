"""Regression test for the stray-window burst reported during agent connect.

The owner watched a background window-count monitor show waves of 50-100+
native windows appearing and then disappearing while an agent was in use —
"+101 -> +110 -> +85 -> +69 -> +57 windows, all 0x0, then they resolve into
250x31 / 250x32 / 256x56 / 256x120 / 280x96 panes, then -288 and they are
gone", observed twice over the course of an afternoon.

A `QWidget` (or any `QWidget` subclass) constructed with no parent IS a
top-level window in Qt — `isWindow()` is true — and on macOS the OS hands it
a real native window the instant it is realised (`QEvent.Show` /
`QEvent.WinIdChange` / `QEvent.Create`), even if the very next line
reparents it into a layout. The already-created native window is never
reclaimed once that happens; only destroying the widget gives it back.

This test installs the same kind of `QApplication`-wide event filter used to
find the bug in the first place (watching every widget where `isWindow()` is
true on those three events) and drives a real `AgentPanel` through a
simulated connect: `set_capabilities`, a large `set_commands` list, a
`set_config_options` update with many choices, `set_modes`, a refresh of the
agent chip's menu, and — the one that actually mattered — a long run of
consecutive tool calls, since `_ToolGroupRow.add_tool` (`ui/transcript.py`)
used to construct each `_ToolCallRow` parentless and reparent it a line
later in `_adopt`.

Measured before the fix, on this exact sequence: 56 window-flagged
realisations during bare `AgentPanel()` construction (mostly `settings_view.
py`'s `SettingsView.__init__`, where nearly every field — checkboxes, line
edits, labels, buttons — was built with no parent and only picked one up
when added to a layout moments later) plus 58 more from 60 simulated
consecutive tool calls (60 - 2: every call past the second, which is where a
run of tool calls graduates into a `_ToolGroupRow`). After the fix: 1 at
construction — `AgentPanel` itself, which genuinely is a top-level window,
being a Houdini pane widget constructed with `parent=None` — and 0 from
everything else. That is the number this test protects: at most the panel
itself, and nothing else, ever.
"""

from __future__ import annotations

import collections
from types import SimpleNamespace

import pytest

from houdini_agent_panel.client import AgentInfo
from houdini_agent_panel.transcript_model import TranscriptModel
from houdini_agent_panel.ui import panel as panel_mod
from houdini_agent_panel.ui.qt import QtCore, QtWidgets

_EVENT_NAMES = {
    QtCore.QEvent.Show: "Show",
    QtCore.QEvent.WinIdChange: "WinIdChange",
    QtCore.QEvent.Create: "Create",
}


class _WindowSpy(QtCore.QObject):
    """Counts every widget realised as a top-level window while installed.

    `isWindow()` must be checked AT THE MOMENT of the event, not assumed
    from where a widget ends up later — a widget built with no parent and
    reparented a line afterward is a window for that one moment, which is
    exactly the defect this test exists to catch.
    """

    def __init__(self) -> None:
        super().__init__()
        self.by_class: collections.Counter = collections.Counter()
        self.total = 0

    def eventFilter(self, obj, event) -> bool:  # noqa: N802 - Qt override
        if (
            event.type() in _EVENT_NAMES
            and isinstance(obj, QtWidgets.QWidget)
            and obj.isWindow()
        ):
            self.by_class[type(obj).__name__] += 1
            self.total += 1
        return False  # never consume — just watch


def _drain_deferred_deletes(app) -> None:
    # `processEvents()` alone does not run `QEvent.DeferredDelete`
    # (docs/facts/houdini.md §13) — a count taken right after it, without
    # this, undercounts what's actually pending and can hide a real leak.
    app.sendPostedEvents(None, QtCore.QEvent.DeferredDelete)
    app.processEvents()


def _fake_command(i: int) -> SimpleNamespace:
    return SimpleNamespace(
        name=f"cmd-{i}", description=f"Command number {i}",
        input=SimpleNamespace(hint=f"<arg-{i}>") if i % 3 == 0 else None,
    )


def _fake_choice(i: int) -> SimpleNamespace:
    return SimpleNamespace(value=f"model-{i}", name=f"Model {i}", description=f"Description {i}")


@pytest.fixture
def _no_background_work(monkeypatch):
    """Same isolation as `test_ui_panel.py`'s `isolated_panel_state` — a
    fresh scene stub and no real background threads, so `AgentPanel()`
    builds without touching Houdini or the network."""
    monkeypatch.setattr(panel_mod.scene, "hip_dir", lambda: "/tmp")
    monkeypatch.setattr(panel_mod.scene, "mcp_servers", lambda: [])
    monkeypatch.setattr(panel_mod._RefreshWorker, "start", lambda self: None)
    monkeypatch.setattr(panel_mod._OrphanSweepWorker, "start", lambda self: None)
    panel_mod.reset_shared_state_for_tests()
    yield
    panel_mod.reset_shared_state_for_tests()


def test_connect_does_not_flash_stray_windows(qapp, _no_background_work):
    # Flush whatever earlier, unrelated tests left pending BEFORE the spy
    # goes on and BEFORE anything of ours is built: `sendPostedEvents(None,
    # ...)` below drains deferred deletes for the WHOLE process, not just
    # this test's own widgets — running it once here first keeps a leftover
    # `deleteLater()` from some other test's own (unrelated, often
    # deliberately parentless-for-that-test) widget from being attributed
    # to this test the moment it's finally destroyed.
    _drain_deferred_deletes(qapp)
    spy = _WindowSpy()
    qapp.installEventFilter(spy)
    try:
        widget = panel_mod.AgentPanel()
        qapp.processEvents()
        _drain_deferred_deletes(qapp)

        # `AgentPanel` itself is a real top-level window — it's a Houdini
        # pane widget constructed with `parent=None` — so exactly one
        # window-flagged realisation at construction is correct, not a bug.
        # Measured before the fix: 56 (see module docstring).
        assert spy.total <= 1, (
            f"AgentPanel() construction realised {spy.total} window-flagged "
            f"widgets (expected at most 1, the panel itself): {spy.by_class}"
        )
        spy.by_class.clear()
        spy.total = 0

        info = AgentInfo(
            name="claude", version="0.64.2", protocol_version=1,
            supports_image=True, supports_audio=True, supports_embedded_context=True,
            supports_load_session=False, supports_logout=True, auth_methods=(),
        )
        widget._composer.set_capabilities(info, "")
        # Claude ships hundreds of slash commands via its personal
        # marketplace (docs/facts/acp-sdk.md §8) — this is the scale a real
        # connect actually sends, not a token handful.
        widget._composer.set_commands([_fake_command(i) for i in range(300)])
        config_option = SimpleNamespace(
            id="model", name="Model", current_value="model-0",
            choices=[_fake_choice(i) for i in range(80)],
        )
        widget._composer.set_config_options([config_option])
        modes = [SimpleNamespace(id=f"mode-{i}", name=f"Mode {i}", description="") for i in range(20)]
        widget._composer.set_modes(modes, "mode-0")
        widget._header.set_agent_menu([(f"agent-{i}", f"Agent {i}") for i in range(10)], "agent-0")
        _drain_deferred_deletes(qapp)

        assert spy.total == 0, (
            f"capabilities/commands/config/modes/agent-menu realised "
            f"{spy.total} window-flagged widgets (expected 0): {spy.by_class}"
        )

        # The actual multiplier: a long run of consecutive tool calls, one
        # `session/update` at a time — exactly how a real agentic turn
        # streams in, not a single batch. `_refresh_one` collapses a run of
        # 2+ into one `_ToolGroupRow`, and every call from the third one on
        # used to go through `_ToolGroupRow.add_tool`, which built its
        # `_ToolCallRow` with no parent. Measured before the fix: 58 (60 - 2)
        # window-flagged realisations for this exact loop.
        model = TranscriptModel()
        widget._transcript.set_model(model)
        _drain_deferred_deletes(qapp)
        spy.by_class.clear()
        spy.total = 0

        n_tool_calls = 60
        for i in range(n_tool_calls):
            call = SimpleNamespace(
                tool_call_id=f"tool-{i}", title=f"Read file {i}", kind="read",
                status="completed", content=[], locations=[],
            )
            entry = model.apply_tool_call(call)
            widget._transcript.refresh(entry.id)
        _drain_deferred_deletes(qapp)

        assert spy.total == 0, (
            f"{n_tool_calls} consecutive tool calls realised {spy.total} "
            f"window-flagged widgets (expected 0): {spy.by_class}"
        )

        widget.shutdown()
    finally:
        qapp.removeEventFilter(spy)

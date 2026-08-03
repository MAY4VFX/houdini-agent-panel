"""Standalone live preview for the production Qt widgets.

Run from an editable checkout with::

    .venv/bin/python -m houdini_agent_panel.dev_preview --watch

The watcher restarts the preview process after Python source changes.  Houdini
is not imported; ``ui.qt`` uses ordinary PySide6 from the dev environment.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from .sessions import AvailableCommand, SessionMode, SessionState, Usage
from .transcript_model import PermissionView, TranscriptModel
from .ui.chips import HeaderBar
from .ui.composer import Composer
from .ui.conversations import ConversationDrawer
from .ui.permissions import PermissionRow
from .ui.qt import QtCore, QtGui, QtWidgets
from .ui.transcript import TranscriptView


def _apply_preview_palette(app: QtWidgets.QApplication) -> None:
    palette = QtGui.QPalette()
    colors = (
        (QtGui.QPalette.Window, "#181818"),
        (QtGui.QPalette.Base, "#303030"),
        (QtGui.QPalette.AlternateBase, "#292929"),
        (QtGui.QPalette.Text, "#e5e3df"),
        (QtGui.QPalette.WindowText, "#e5e3df"),
        (QtGui.QPalette.Button, "#2b2b2b"),
        (QtGui.QPalette.ButtonText, "#d1cec8"),
        (QtGui.QPalette.Mid, "#393939"),
        (QtGui.QPalette.Highlight, "#dfa047"),
    )
    for role, color in colors:
        palette.setColor(role, QtGui.QColor(color))
    palette.setColor(QtGui.QPalette.Disabled, QtGui.QPalette.Text, QtGui.QColor("#85827d"))
    app.setPalette(palette)


class PreviewPanel(QtWidgets.QWidget):
    """Interactive fixture composed entirely from production UI classes."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._model = TranscriptModel()

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.header = HeaderBar(self)
        self.header.set_agent("Codex", None)
        self.header.set_cwd("$HIP / hero_shot")
        layout.addWidget(self.header)

        self.transcript = TranscriptView(self)
        self._seed_transcript()
        self.transcript.set_model(self._model)
        layout.addWidget(self.transcript, 1)

        self.composer = Composer(self)
        self.composer.set_modes(
            [
                SessionMode("approval", "♨  Ask for approval"),
                SessionMode("auto", "Auto"),
            ],
            "approval",
        )
        self.composer.set_usage(Usage(total_tokens=8_200))
        # Shaped exactly like `client.ConfigOption` — the preview feeds the
        # composer the same duck-typed data a real agent's `configOptions`
        # would arrive as.
        self.composer.set_config_options(
            [
                SimpleNamespace(
                    id="model",
                    name="Model",
                    description="Model",
                    current_value="sonnet",
                    choices=(
                        SimpleNamespace(value="sonnet", name="Claude Sonnet 4.5"),
                        SimpleNamespace(value="opus", name="Claude Opus 4.1"),
                    ),
                ),
            ]
        )
        self.composer.enable_preview_microphone()
        self.composer.set_buddy("crag")
        self.composer.set_commands(
            [
                AvailableCommand("compact", "Compact this chat context"),
                AvailableCommand("new", "Continue in a new conversation"),
                AvailableCommand("model", "Choose the agent model"),
                AvailableCommand("reasoning", "Set reasoning effort"),
                AvailableCommand("clear", "Clear the current conversation"),
            ]
        )
        self.composer.submitted.connect(self._submit)
        self.composer.cancelled.connect(self._finish_preview_turn)
        layout.addWidget(self.composer)
        self.permission = PermissionRow(self._preview_permission, self)
        self.permission.answered.connect(self._resolve_permission)
        self.permission.show()
        self.permission.raise_()
        self.conversations = ConversationDrawer(self)
        self.conversations.set_sessions(
            [
                SessionState("lighting", "Build a soft rim light", "/tmp/hero_shot", 1.0),
                SessionState("materials", "Fix the wet rock material", "/tmp/hero_shot", 2.0),
                SessionState("preview", "Current Houdini scene", "/tmp/hero_shot", 3.0),
            ],
            "preview",
        )
        self.header.conversations_clicked.connect(self.conversations.toggle)
        QtCore.QTimer.singleShot(0, self._position_permission)
        QtCore.QTimer.singleShot(0, self._start_visible_activity)

    def _seed_transcript(self) -> None:
        self._model.append_user(
            "Set up a soft rim light and expose its intensity through a single controller."
        )
        activity = self._model.start_activity()
        activity.activity.started_at -= 42
        self._model.finish_activity()
        self._model.apply_chunk(
            "intro",
            "Done. Added a soft Area Light and wired its intensity "
            "to a single controller.",
        )
        self._model.apply_tool_call(
            SimpleNamespace(
                tool_call_id="preview-tool",
                title="Create Area Light",
                kind="edit",
                status="pending",
                content=None,
                locations=None,
            )
        )
        self._preview_permission = PermissionView(
            "preview-permission",
            "Allow changing the scene?",
            [
                ("reject_once", "Reject", "reject_once"),
                ("allow_once", "Allow once", "allow_once"),
            ],
        )
        self._model.apply_permission(self._preview_permission)

    def _submit(self, blocks: list[dict]) -> None:
        self._finish_preview_turn()
        text = " ".join(
            block.get("text", "") for block in blocks if block.get("type") == "text"
        ).strip()
        if text:
            user = self._model.append_user(text)
            self.transcript.refresh(user.id)
        activity = self._model.start_activity()
        self.transcript.refresh(activity.id)
        self.composer.trigger_buddy()
        self.composer.set_busy(True)
        QtCore.QTimer.singleShot(1_400, self._preview_tool_burst)
        QtCore.QTimer.singleShot(3_200, self._finish_preview_turn)

    def _start_visible_activity(self) -> None:
        activity = self._model.start_activity()
        self.transcript.refresh(activity.id)
        self.composer.set_busy(True)

    def _preview_tool_burst(self) -> None:
        call_id = f"preview-{uuid.uuid4()}"
        entry = self._model.apply_tool_call(
            SimpleNamespace(
                tool_call_id=call_id,
                title="Update Houdini scene",
                kind="edit",
                status="in_progress",
                content=None,
                locations=None,
            )
        )
        self.transcript.refresh(entry.id)
        self.transcript.reset_thinking_after_tool()

    def _finish_preview_turn(self) -> None:
        activity = self._model.finish_activity()
        if activity is None:
            return
        self.transcript.refresh(activity.id)
        answer = self._model.apply_chunk(
            f"preview-answer-{uuid.uuid4()}",
            "Preview turn finished. This is where the agent's streamed reply would go.",
        )
        self.transcript.refresh(answer.id)
        self.composer.set_busy(False)

    def _resolve_permission(self, request_key: str, option_id: str) -> None:
        entry = self._model.resolve_permission(request_key, option_id or None)
        if entry is not None:
            self.transcript.refresh(entry.id)
        self.permission.hide()

    def _position_permission(self) -> None:
        anchor = self.composer.popover_anchor_rect(self)
        width = min(400, max(280, anchor.width() - 96))
        self.permission.setFixedWidth(width)
        self.permission.adjustSize()
        self.permission.move(
            anchor.center().x() - width // 2,
            anchor.top() - self.permission.height() - 10,
        )
        self.permission.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        if hasattr(self, "permission"):
            self._position_permission()
        if hasattr(self, "conversations"):
            self.conversations.sync_parent_geometry()
            if self.conversations.isVisible():
                self.conversations.raise_()


def _run_window() -> int:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    _apply_preview_palette(app)
    window = QtWidgets.QMainWindow()
    window.setWindowTitle("Houdini Agent Panel — UI preview")
    window.setCentralWidget(PreviewPanel(window))
    window.resize(900, 700)
    window.show()
    return app.exec()


def _source_stamp() -> tuple[tuple[str, int], ...]:
    package_root = Path(__file__).resolve().parent
    files = (*package_root.rglob("*.py"), *package_root.rglob("*.png"))
    return tuple(sorted((str(path), path.stat().st_mtime_ns) for path in files))


def _watch() -> int:
    child: subprocess.Popen | None = None
    stamp: tuple[tuple[str, int], ...] | None = None
    env = os.environ.copy()
    env.pop("QT_QPA_PLATFORM", None)
    try:
        while True:
            next_stamp = _source_stamp()
            if child is None or child.poll() is not None or next_stamp != stamp:
                if child is not None and child.poll() is None:
                    child.terminate()
                    try:
                        child.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait()
                stamp = next_stamp
                child = subprocess.Popen(
                    [sys.executable, "-m", "houdini_agent_panel.dev_preview", "--child"],
                    env=env,
                )
            time.sleep(0.4)
    except KeyboardInterrupt:
        return 0
    finally:
        if child is not None and child.poll() is None:
            child.terminate()
            child.wait(timeout=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true", help="restart preview after source saves")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    return _watch() if args.watch and not args.child else _run_window()


if __name__ == "__main__":  # pragma: no cover - manual dev entry point
    raise SystemExit(main())

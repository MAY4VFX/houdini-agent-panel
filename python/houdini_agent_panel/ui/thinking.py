"""Turn activity and a small Houdini test-geometry companion.

The timing/state model follows OpenClaude's public UI as a behavioural
reference (dot pulse, stable verb per burst, shimmer and an Enter action), but
this renderer is native Qt so it works in Houdini's PySide2/PySide6 shim.
"""

from __future__ import annotations

import random
import os
import time
from pathlib import Path

from .qt import QtCore, QtGui, QtWidgets, Signal

_DATA_DIR = Path(__file__).resolve().parents[1] / "data"
_BUDDIES = ("crag", "pig", "rubber-toy", "squid")

# OpenClaude's pulse grows and then walks back down the same sequence.
_DOTS = ("·", "∘", "○", "◎", "◉", "●")
_DOT_FRAMES = _DOTS + tuple(reversed(_DOTS))
_VERBS = (
    "Pondering",
    "Architecting",
    "Composing",
    "Concocting",
    "Crafting",
    "Deciphering",
    "Ideating",
    "Noodling",
    "Orchestrating",
    "Ruminating",
    "Shaping",
    "Tinkering",
    "Transmuting",
    "Wrangling",
)
_TICK_MS = 50
_DOT_FRAME_MS = 120
_SHIMMER_STEP_MS = 200
_TIMER_AFTER_MS = 5_000
_ACTION_MS = 1_800
_IDLE_FRAME_MS = 650
_AMBIENT_CYCLE_MS = 14_000
_THINK_START_MS = 11_200
_THINK_FRAME_MS = 700
_HOUDINI_AMBER = QtGui.QColor(222, 142, 74)


def _format_duration(milliseconds: int) -> str:
    seconds = max(0, int(milliseconds / 1000))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


class _ShimmerText(QtWidgets.QWidget):
    """Single status line with OpenClaude's slow reverse glimmer sweep."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._text = ""
        self._highlight = -1.0
        self.setSizePolicy(QtWidgets.QSizePolicy.Preferred, QtWidgets.QSizePolicy.Fixed)

    def set_text(self, text: str) -> None:
        if text == self._text:
            return
        self._text = text
        self.updateGeometry()
        self.update()

    def set_highlight(self, position: float) -> None:
        self._highlight = position
        self.update()

    def sizeHint(self) -> QtCore.QSize:  # noqa: N802 - Qt override
        metrics = QtGui.QFontMetrics(self.font())
        return QtCore.QSize(metrics.horizontalAdvance(self._text) + 6, metrics.height() + 4)

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802 - Qt override
        del event
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.TextAntialiasing)
        muted = self.palette().color(QtGui.QPalette.Disabled, QtGui.QPalette.Text)
        muted.setAlpha(190)
        bright = self.palette().color(QtGui.QPalette.Text)

        gradient = QtGui.QLinearGradient(0, 0, max(self.width(), 1), 0)
        if 0.0 <= self._highlight <= 1.0:
            shoulder = 0.12
            gradient.setColorAt(0.0, muted)
            gradient.setColorAt(max(0.0, self._highlight - shoulder), muted)
            gradient.setColorAt(self._highlight, bright)
            gradient.setColorAt(min(1.0, self._highlight + shoulder), muted)
            gradient.setColorAt(1.0, muted)
        else:
            gradient.setColorAt(0.0, muted)
            gradient.setColorAt(1.0, muted)
        painter.setPen(QtGui.QPen(QtGui.QBrush(gradient), 1))
        painter.drawText(self.rect(), QtCore.Qt.AlignLeft | QtCore.Qt.AlignVCenter, self._text)


class _BuddySprite(QtWidgets.QWidget):
    """Pixel-art companion with the idle/action state cadence from OpenClaude."""

    clicked = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setFixedSize(54, 54)
        self._frames: dict[str, tuple[QtGui.QPixmap, ...]] = {}
        self._elapsed_ms = 0
        self._action_elapsed: int | None = None
        self._key = "crag"
        self._started_at = time.monotonic()
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._advance_clock)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setAccessibleName("Houdini test geometry companion")
        self._animated = os.environ.get("HOUDINI_AGENT_REDUCED_MOTION") != "1"
        self.set_buddy(self._key)

    # Анимация живёт ровно столько, сколько её видно.
    #
    # Раньше таймер запускался в __init__ и не останавливался никогда: 20 тиков
    # в секунду крутились всё время жизни панели, в том числе когда её вкладка
    # неактивна или скрыта. Это чужой процесс, в нём человек работает со
    # сценой, и тратить его такт на перерисовку невидимого маскота панель
    # права не имеет.

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        if self._animated and not self._timer.isActive():
            self._timer.start()

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        super().hideEvent(event)
        self._timer.stop()

    def set_buddy(self, key: str) -> None:
        if key not in _BUDDIES:
            key = _BUDDIES[sum(ord(ch) for ch in key) % len(_BUDDIES)]
        self._key = key
        self._frames = {
            "idle": tuple(
                QtGui.QPixmap(str(_DATA_DIR / f"buddy-{key}-{index}.png"))
                for index in range(4)
            ),
            "think": tuple(
                QtGui.QPixmap(str(_DATA_DIR / f"buddy-{key}-think-{index}.png"))
                for index in range(4)
            ),
            "action": tuple(
                QtGui.QPixmap(str(_DATA_DIR / f"buddy-{key}-action-{index}.png"))
                for index in range(4)
            ),
        }
        self.setToolTip(
            f"Houdini Test Geometry: {key.replace('-', ' ').title()} — нажмите, чтобы сменить"
        )
        self.update()

    def mousePressEvent(self, event: QtGui.QMouseEvent) -> None:  # noqa: N802
        if event.button() != QtCore.Qt.LeftButton:
            super().mousePressEvent(event)
            return
        next_index = (_BUDDIES.index(self._key) + 1) % len(_BUDDIES)
        self.set_buddy(_BUDDIES[next_index])
        self.start_action()
        self.clicked.emit(self._key)
        event.accept()

    def start_action(self) -> None:
        self._action_elapsed = 0
        self.update()

    def advance(self, elapsed_ms: int) -> None:
        self._elapsed_ms = elapsed_ms
        if self._action_elapsed is not None:
            self._action_elapsed += _TICK_MS
            if self._action_elapsed >= _ACTION_MS:
                self._action_elapsed = None
        self.update()

    def _advance_clock(self) -> None:
        self.advance(max(0, int((time.monotonic() - self._started_at) * 1000)))

    def _current_pose(self) -> tuple[str, int]:
        action = self._action_elapsed
        if action is not None:
            return "action", min(3, int(action * 4 / _ACTION_MS))
        ambient = self._elapsed_ms % _AMBIENT_CYCLE_MS
        if ambient >= _THINK_START_MS:
            return "think", min(3, (ambient - _THINK_START_MS) // _THINK_FRAME_MS)
        return "idle", (self._elapsed_ms // _IDLE_FRAME_MS) % 4

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802 - Qt override
        del event
        all_frames = tuple(frame for animation in self._frames.values() for frame in animation)
        if not all_frames or any(frame.isNull() for frame in all_frames):
            return
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, False)

        animation, frame_index = self._current_pose()
        painter.drawPixmap(self.rect(), self._frames[animation][frame_index])


class ThinkingIndicator(QtWidgets.QWidget):
    """One activity row in the chronological transcript."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._started_at = 0.0
        self._active = False
        self._verb = _VERBS[0]
        self._reduced_motion = os.environ.get("HOUDINI_AGENT_REDUCED_MOTION") == "1"

        layout = QtWidgets.QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(7)

        self._dot = QtWidgets.QLabel(self)
        self._dot.setFixedWidth(14)
        self._dot.setAlignment(QtCore.Qt.AlignCenter)
        self._dot.hide()
        layout.addWidget(self._dot)

        self._status = _ShimmerText(self)
        layout.addWidget(self._status)
        layout.addStretch(1)

        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._advance)

        self.setAccessibleName("Agent activity")
        self.clear_activity()

    def start(self, started_at: float | None = None) -> None:
        self._active = True
        self._verb = random.choice(_VERBS)
        self._started_at = started_at if started_at is not None else time.monotonic()
        self._apply_frame(0)
        if not self._reduced_motion:
            self._timer.start()

    def finish(self, elapsed_ms: int | None = None) -> None:
        if elapsed_ms is None:
            elapsed_ms = max(0, int((time.monotonic() - self._started_at) * 1000))
        self._active = False
        self._timer.stop()
        self._dot.clear()
        self._dot.hide()
        self._status.set_highlight(-1.0)
        self._status.set_text(f"Worked for {_format_duration(elapsed_ms)}  ›")

    def clear_activity(self) -> None:
        self._active = False
        self._timer.stop()
        self._dot.clear()
        self._dot.hide()
        self._status.set_text("")
        self._status.set_highlight(-1.0)

    def reset_after_tool(self) -> None:
        """Start a fresh reasoning burst while preserving the turn timer."""
        if not self._active:
            return
        previous = self._verb
        candidates = tuple(verb for verb in _VERBS if verb != previous)
        self._verb = random.choice(candidates)
        self._apply_frame(max(0, int((time.monotonic() - self._started_at) * 1000)))

    def is_active(self) -> bool:
        return self._active

    def _advance(self) -> None:
        elapsed = max(0, int((time.monotonic() - self._started_at) * 1000))
        self._apply_frame(elapsed)

    def _apply_frame(self, elapsed: int) -> None:
        frame = 0 if self._reduced_motion else (elapsed // _DOT_FRAME_MS) % len(_DOT_FRAMES)
        self._dot.setText("●" if self._reduced_motion else _DOT_FRAMES[frame])
        self._dot.show()
        palette = self._dot.palette()
        palette.setColor(QtGui.QPalette.WindowText, _HOUDINI_AMBER)
        self._dot.setPalette(palette)

        timer = f"  ·  {_format_duration(elapsed)}" if elapsed >= _TIMER_AFTER_MS else ""
        self._status.set_text(f"{self._verb}…{timer}")
        if self._reduced_motion:
            self._status.set_highlight(-1.0)
            return
        width = max(self._status.sizeHint().width(), 1)
        travel = width + 20
        position = width + 10 - (elapsed // _SHIMMER_STEP_MS) % travel
        self._status.set_highlight(position / width)


__all__ = ["ThinkingIndicator", "_BuddySprite"]

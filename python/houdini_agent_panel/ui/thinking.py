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

from . import theme
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


class BuddyEntrance(QtWidgets.QWidget):
    """The companion climbing out of a hole, once an agent has finished
    starting.

    A nod to Houdini's own blackhole: a dark ellipse opens where the buddy
    stands, the buddy rises through it — clipped by the rim until it is
    clear — swells a little past its resting size as it pops free, and the
    hole closes behind it.

    The join with the real sprite has to be invisible, and getting it wrong
    was immediately obvious on screen: the first version drew the raw 64px
    source pixmap while `_BuddySprite` renders it into 54px, invented its
    own resting position, and drew a fixed idle frame while the sprite was
    somewhere else in its cadence. The result jumped in size, in place and
    in pose, all at once.

    So this draws the sprite's OWN current frame (`current_frame`), with the
    sprite's own render hints, into a rect that ends exactly equal to the
    sprite's geometry. At `t = 1` the two are pixel-identical, which is what
    makes the handover a cut rather than a jump.
    """

    finished = Signal()

    #: The whole thing, start to settled. Long enough to read as an event,
    #: short enough that nobody waits for it — the input is already live
    #: underneath by the time it plays.
    DURATION_MS = 1400

    _HOLE_H = 22
    #: The hole is wider than the buddy so it can be seen around it.
    _HOLE_OVERHANG = 16
    #: How far past its resting size the buddy swells on the way out.
    _PEAK_SCALE = 1.14
    #: Room the animation needs around the sprite's own rect: sideways for
    #: the hole, above for the swell, below for the hole's lower half.
    MARGIN_X = 24
    MARGIN_TOP = 14
    MARGIN_BOTTOM = 16

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._source = None  # the live _BuddySprite, asked for its frame each paint
        self._target = QtCore.QRect()
        self._t = 0.0
        self.setAttribute(QtCore.Qt.WA_TransparentForMouseEvents, True)
        self._animation = QtCore.QVariantAnimation(self)
        self._animation.setDuration(self.DURATION_MS)
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.valueChanged.connect(self._on_value)
        self._animation.finished.connect(self._on_finished)
        self.hide()

    def geometry_for(self, target: QtCore.QRect) -> QtCore.QRect:
        """Where this widget has to sit to animate a sprite occupying `target`
        (both in the parent's coordinates)."""
        return QtCore.QRect(
            target.x() - self.MARGIN_X,
            target.y() - self.MARGIN_TOP,
            target.width() + 2 * self.MARGIN_X,
            target.height() + self.MARGIN_TOP + self.MARGIN_BOTTOM,
        )

    def play(self, source, target: QtCore.QRect) -> None:
        """Run it for `source` (a `_BuddySprite`), landing exactly on `target`.

        With motion reduced, or nothing to draw, it finishes at once rather
        than not at all — the caller reveals the real buddy on that signal
        and must never be left waiting for an animation that was skipped.
        """
        self._source = source
        self._target = QtCore.QRect(
            target.x() - self.x(), target.y() - self.y(), target.width(), target.height()
        )
        reduced = os.environ.get("HOUDINI_AGENT_REDUCED_MOTION") == "1"
        if reduced or source is None or source.current_frame().isNull():
            self.finished.emit()
            return
        self._t = 0.0
        source.hold_clock(True)
        self.show()
        self.raise_()
        self._animation.stop()
        self._animation.start()

    def skip(self) -> None:
        """Stop without emitting: the boot was cancelled out from under it."""
        self._animation.stop()
        self._release_clock()
        self.hide()

    def _release_clock(self) -> None:
        if self._source is not None:
            self._source.hold_clock(False)

    def _on_value(self, value) -> None:
        self._t = float(value)
        self.update()

    def _on_finished(self) -> None:
        self._release_clock()
        self.hide()
        self.finished.emit()

    # --- the curves ------------------------------------------------------

    @staticmethod
    def _ease_out(t: float) -> float:
        return 1 - (1 - t) ** 3

    @staticmethod
    def _ease_in(t: float) -> float:
        return t**3

    @staticmethod
    def _back_out(t: float) -> float:
        c1 = 1.70158
        return 1 + (c1 + 1) * (t - 1) ** 3 + c1 * (t - 1) ** 2

    def _state(self) -> tuple[float, float, float]:
        """`(hole, rise, scale)` for the current moment.

        `scale` is relative to the sprite's real size, and is exactly 1.0 at
        the end — the whole point of the join.
        """
        t = self._t
        open_end, rise_end, close_end = 0.24, 0.70, 0.88
        if t < open_end:
            return self._ease_out(t / open_end), 0.0, 0.0
        rise = 1.0 if t >= rise_end else self._ease_out((t - open_end) / (rise_end - open_end))
        if t < rise_end:
            hole = 1.0
        elif t < close_end:
            hole = 1.0 - self._ease_in((t - rise_end) / (close_end - rise_end))
        else:
            hole = 0.0
        growth = 0.34 + (self._PEAK_SCALE - 0.34) * self._back_out(min(1.0, rise / 0.85))
        if t > close_end:
            settle = self._ease_out((t - close_end) / (1.0 - close_end))
            growth = self._PEAK_SCALE + (1.0 - self._PEAK_SCALE) * settle
        return hole, rise, growth

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802
        del event
        if self._source is None or self._target.isNull():
            return
        frame = self._source.current_frame()
        if frame.isNull():
            return
        hole, rise, scale = self._state()

        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        # Nearest-neighbour, exactly as `_BuddySprite` paints it: smoothing
        # pixel art here would make the handover visible even with the
        # geometry perfect.
        painter.setRenderHint(QtGui.QPainter.SmoothPixmapTransform, False)

        # From the edges, not from `center()`: QRect.center() floors, so on a
        # 54px sprite it lands half a pixel off and the final frame resamples
        # to something subtly different from what the sprite itself draws.
        # Measured — that half pixel was a visible difference across the
        # buddy's lower half in an image diff of the join.
        centre_x = self._target.x() + self._target.width() / 2.0
        # The feet, which is where the hole belongs and what the scaling is
        # anchored to — a buddy that swells from its centre lifts off the
        # ground.
        ground = float(self._target.bottom() + 1)
        hole_w = self._target.width() + self._HOLE_OVERHANG

        if hole > 0.001:
            width, height = hole_w * hole, self._HOLE_H * hole
            rect = QtCore.QRectF(centre_x - width / 2, ground - height / 2, width, height)
            gradient = QtGui.QRadialGradient(rect.center(), max(1.0, width / 2))
            # theme-exception: a hole is an absence, not a surface. Depth
            # reads as near-black in light and dark themes alike — the same
            # reasoning the permission popover's shadow is exempt under —
            # and a hole tinted to follow a pink theme stops being a hole.
            # Only the void is fixed; the rim below comes from the theme.
            gradient.setColorAt(0.0, QtGui.QColor(5, 7, 13))  # theme-exception: see above
            gradient.setColorAt(0.72, QtGui.QColor(11, 16, 32))  # theme-exception: as above
            gradient.setColorAt(1.0, QtGui.QColor(27, 42, 68))  # theme-exception: as above
            painter.setPen(QtCore.Qt.NoPen)
            painter.setBrush(gradient)
            painter.drawEllipse(rect)
            # The far rim catches light, which is what makes it read as a
            # hole in a surface rather than a dark blob painted on one. This
            # one IS themed: it is light falling on the artist's own UI.
            rim = QtGui.QColor(theme.accent_color())
            rim.setAlpha(int(120 * hole))
            painter.setBrush(QtCore.Qt.NoBrush)
            painter.setPen(QtGui.QPen(rim, 1.4))
            painter.drawArc(rect, 200 * 16, 140 * 16)

        if rise > 0:
            width = self._target.width() * scale
            height = self._target.height() * scale
            # Anchored at the feet: at rise=1, scale=1 this is exactly
            # `self._target`, so the real sprite takes over without moving.
            top = ground - height + (height + self._HOLE_H * 0.3) * (1.0 - rise)
            target = QtCore.QRectF(centre_x - width / 2, top, width, height)
            painter.save()
            if hole > 0.02:
                # Clipped by the rim while still coming through: everything
                # above the hole, plus the mouth of the hole itself.
                clip = QtGui.QPainterPath()
                clip.addRect(QtCore.QRectF(0, 0, self.width(), ground))
                mouth = QtGui.QPainterPath()
                mouth.addEllipse(
                    QtCore.QRectF(
                        centre_x - hole_w * hole / 2,
                        ground - self._HOLE_H * hole / 2,
                        hole_w * hole,
                        self._HOLE_H * hole,
                    )
                )
                painter.setClipPath(clip.united(mouth))
            painter.drawPixmap(target, frame, QtCore.QRectF(frame.rect()))
            painter.restore()
        painter.end()


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
        self._hold_clock = False
        self._started_at = time.monotonic()
        self._timer = QtCore.QTimer(self)
        self._timer.setInterval(_TICK_MS)
        self._timer.timeout.connect(self._advance_clock)
        self.setCursor(QtCore.Qt.PointingHandCursor)
        self.setAccessibleName("Houdini test geometry companion")
        self._animated = os.environ.get("HOUDINI_AGENT_REDUCED_MOTION") != "1"
        self.set_buddy(self._key)

    # The animation lives exactly as long as it is visible.
    #
    # The timer used to start in __init__ and never stop: twenty ticks a
    # second for the whole life of the panel, including while its tab was
    # inactive or hidden. This is someone else's process, a human is working
    # on a scene in it, and the panel has no right to spend their frame time
    # redrawing a mascot nobody can see.

    def showEvent(self, event: QtGui.QShowEvent) -> None:
        super().showEvent(event)
        if self._animated and not self._timer.isActive():
            self._timer.start()

    def hideEvent(self, event: QtGui.QHideEvent) -> None:
        super().hideEvent(event)
        if not self._hold_clock:
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
            f"Houdini Test Geometry: {key.replace('-', ' ').title()} — click to change"
        )
        self.update()

    def current_frame(self) -> QtGui.QPixmap:
        """The frame this buddy is showing RIGHT NOW.

        `BuddyEntrance` draws this rather than a fixed idle frame, so the
        creature does not visibly jump to another pose the instant the
        animation hands over. Used together with `hold_clock`, which keeps
        the cadence running while the sprite itself is hidden.
        """
        animation, index = self._current_pose()
        frames = self._frames.get(animation) or ()
        if not frames:
            return QtGui.QPixmap()
        return frames[min(index, len(frames) - 1)]

    def hold_clock(self, holding: bool) -> None:
        """Keep ticking while hidden (or stop doing so).

        The rule elsewhere is that the animation lives exactly as long as it
        is visible — a mascot nobody can see has no right to the artist's
        frame time. The one exception is the moment the buddy is being drawn
        by somebody else: stopping the clock there freezes the pose, and the
        handover then jumps a frame.
        """
        self._hold_clock = holding
        if holding and self._animated and not self._timer.isActive():
            self._timer.start()
        elif not holding and not self.isVisible():
            self._timer.stop()

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
        # The pulsing dot's own colour — the theme's live accent, read fresh
        # on every frame (this used to be a fixed "Houdini amber" that
        # stayed amber under a Houdini 22 "Edit Theme" preset like Plumtree).
        palette = self._dot.palette()
        palette.setColor(QtGui.QPalette.WindowText, theme.accent_color())
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

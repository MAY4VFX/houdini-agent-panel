"""A visible answer to "is it doing anything?" while an agent starts.

Reported by the artist: starting an agent showed a couple of lines of text
in the feed ("Preparing Codex…", "Launching Codex…"), which then vanished,
and some time later the model and mode chips simply appeared. Nothing said
a process was still coming up, nothing said how far along it was, and the
gap is not short — an agent fetched by `npx` can take a minute, and the fx
MCP server alone costs 12-16s under a Houdini interpreter (measured).

So the phases the panel already knows about are shown as a strip that
stays put until the agent is ready, instead of scrolling away in the feed:
a step counter, the name of the step, and a bar that fills as the steps
complete. Ending is as explicit as starting — the strip reports "Ready"
and then removes itself.

The phases are the real ones the panel goes through, not decoration:

    Preparing → Launching → Connecting → Opening a conversation → Ready

Each is entered from the code path that actually does that work, so the
strip cannot claim progress that has not happened. There is deliberately no
timer advancing it on its own: a bar that moves while nothing happens is a
lie that costs the artist their trust in every other indicator we draw.
"""

from __future__ import annotations

from . import theme
from .qt import QtCore, QtGui, QtWidgets

#: The steps, in order. `PHASE_READY` is the terminal one and is not
#: counted as work — it exists so the strip can say it finished before it
#: goes away.
PHASE_PREPARING = "preparing"
PHASE_LAUNCHING = "launching"
PHASE_CONNECTING = "connecting"
PHASE_SESSION = "session"
PHASE_READY = "ready"

_ORDER = (PHASE_PREPARING, PHASE_LAUNCHING, PHASE_CONNECTING, PHASE_SESSION)

_LABELS = {
    PHASE_PREPARING: "Preparing {name}",
    PHASE_LAUNCHING: "Starting {name}",
    PHASE_CONNECTING: "Connecting to {name}",
    PHASE_SESSION: "Opening a conversation",
    PHASE_READY: "{name} is ready",
}

#: How long "Ready" stays on screen before the strip hides itself. Long
#: enough to be read as an ending rather than a flicker; short enough that
#: it is gone by the time anybody types.
_READY_LINGER_MS = 1200

_BAR_HEIGHT = 3


class _ProgressBar(QtWidgets.QWidget):
    """A bar filled to a fraction, in the theme accent.

    Not a `QProgressBar`: that one comes with a groove, a border, a text
    format and a chunk, all of which have to be styled away before it looks
    like anything but a Windows installer. Two `fillRect` calls are the
    whole widget.
    """

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._fraction = 0.0
        self.setFixedHeight(_BAR_HEIGHT)
        self.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Fixed)

    def set_fraction(self, fraction: float) -> None:
        clamped = max(0.0, min(1.0, fraction))
        if clamped == self._fraction:
            return
        self._fraction = clamped
        self.update()

    def fraction(self) -> float:
        return self._fraction

    def paintEvent(self, event) -> None:  # noqa: N802 - Qt
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing, True)
        track = theme.color(QtGui.QPalette.Mid)
        accent = theme.accent_color()
        radius = _BAR_HEIGHT / 2
        painter.setPen(QtCore.Qt.NoPen)
        painter.setBrush(track)
        painter.drawRoundedRect(QtCore.QRectF(self.rect()), radius, radius)
        if self._fraction > 0:
            filled = QtCore.QRectF(self.rect())
            filled.setWidth(filled.width() * self._fraction)
            painter.setBrush(accent)
            painter.drawRoundedRect(filled, radius, radius)
        painter.end()


class BootStatus(QtWidgets.QWidget):
    """The strip. Hidden unless an agent is coming up."""

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self._name = ""
        self._phase = ""
        self._detail = ""
        #: Is a boot running? Kept as state rather than read off
        #: `isVisible()`, which answers False for every widget whose panel
        #: sits in a background tab — there, `finish()` bailed out and the
        #: strip stayed frozen on "Opening a conversation" until the artist
        #: looked at it. Caught in a rendering of the phases, not by a test.
        self._active = False

        self._label = QtWidgets.QLabel(self)
        self._step = QtWidgets.QLabel(self)
        self._bar = _ProgressBar(self)

        font = self._label.font()
        font.setPointSizeF(max(1.0, font.pointSizeF() - 1))
        self._label.setFont(font)
        self._step.setFont(font)
        self._step.setAlignment(QtCore.Qt.AlignRight | QtCore.Qt.AlignVCenter)
        # The step counter must not fight the phase name for width: the name
        # is what the artist reads, the counter only reassures.
        self._step.setSizePolicy(QtWidgets.QSizePolicy.Fixed, QtWidgets.QSizePolicy.Preferred)
        self._label.setSizePolicy(QtWidgets.QSizePolicy.Ignored, QtWidgets.QSizePolicy.Preferred)

        row = QtWidgets.QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(theme.SPACING)
        row.addWidget(self._label, 1)
        row.addWidget(self._step, 0)

        layout = QtWidgets.QVBoxLayout(self)
        layout.setContentsMargins(theme.SPACING, theme.SPACING_TIGHT, theme.SPACING, theme.SPACING_TIGHT)
        layout.setSpacing(theme.SPACING_TIGHT)
        layout.addLayout(row)
        layout.addWidget(self._bar)

        self._hide_timer = QtCore.QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

        self._apply_theme()
        self.hide()

    # --- driving it --------------------------------------------------------

    def begin(self, agent_name: str) -> None:
        """An agent is starting. Shows the strip at the first phase."""
        self._name = agent_name or "the agent"
        self._active = True
        self._hide_timer.stop()
        self.set_phase(PHASE_PREPARING)
        self.show()

    def set_phase(self, phase: str, detail: str = "") -> None:
        """Move to `phase`. Unknown phases are ignored rather than guessed at.

        `detail` is for what only the running step knows — the package being
        downloaded, say. It replaces the step name when present, because a
        specific truth beats a generic one.
        """
        if phase not in _LABELS:
            return
        self._phase = phase
        self._detail = detail
        if phase == PHASE_READY:
            self._active = False
            self._bar.set_fraction(1.0)
            self._hide_timer.start(_READY_LINGER_MS)
        else:
            # Filled by steps COMPLETED, so entering the first phase shows an
            # empty bar: nothing has finished yet, and pretending otherwise
            # would make the last step look shorter than it is.
            self._bar.set_fraction(_ORDER.index(phase) / len(_ORDER))
            self._hide_timer.stop()
            self.show()
        self._refresh_text()

    def finish(self) -> None:
        """The agent is up. Says so, then hides.

        Does nothing unless a boot was actually running: `session/new` also
        fires when the artist presses "+" on an agent that has been up for
        an hour, and flashing "ready" at them there would mean nothing.
        """
        if not self._active:
            return
        self.set_phase(PHASE_READY)

    def cancel(self) -> None:
        """Boot ended without becoming ready — a failure, or the artist
        switched away. The strip goes immediately: the reason is reported in
        the feed, and leaving a half-filled bar behind would suggest
        something is still coming."""
        self._active = False
        self._hide_timer.stop()
        self.hide()

    # --- state, for the tests and for whoever asks -------------------------

    def phase(self) -> str:
        return self._phase

    def is_booting(self) -> bool:
        return self._active

    def fraction(self) -> float:
        return self._bar.fraction()

    def text(self) -> str:
        return self._label.text()

    # --- painting ----------------------------------------------------------

    def _refresh_text(self) -> None:
        template = _LABELS.get(self._phase, "")
        self._label.setText(self._detail or template.format(name=self._name))
        if self._phase == PHASE_READY:
            self._step.setText("")
        else:
            self._step.setText(f"{_ORDER.index(self._phase) + 1}/{len(_ORDER)}")

    def _apply_theme(self) -> None:
        muted = theme.color(QtGui.QPalette.Text, QtGui.QPalette.Disabled)
        self._label.setStyleSheet(f"color: {theme.to_hex(muted)};")
        self._step.setStyleSheet(f"color: {theme.to_hex(muted)};")

    def changeEvent(self, event) -> None:  # noqa: N802 - Qt
        if event.type() == QtCore.QEvent.PaletteChange:
            self._apply_theme()
            self._bar.update()
        super().changeEvent(event)


__all__ = [
    "BootStatus",
    "PHASE_CONNECTING",
    "PHASE_LAUNCHING",
    "PHASE_PREPARING",
    "PHASE_READY",
    "PHASE_SESSION",
]

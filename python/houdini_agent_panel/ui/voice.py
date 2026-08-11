"""The composer's microphone button: recording and (optionally) whisper.

design.md's rule for this module: an agent with the ``audio`` capability gets
the audio block as-is; an agent without it but with a local whisper endpoint
configured gets recognised text; neither, and there is no button at all. The
``QtMultimedia`` API itself diverged between PySide2 (``QAudioRecorder``, Qt5)
and PySide6 (``QMediaCaptureSession``/``QAudioInput``/``QMediaRecorder``,
Qt6), so there is no single path — the module tries both and honestly hides
the button if neither came together, rather than pretending recording works.

Voice input is currently OFF EVERYWHERE, unconditionally, regardless of
platform — see ``_VOICE_INPUT_AVAILABLE``. Not a platform check: on macOS,
Houdini's own bundle (checked across 20.5/21.0/22.0, every edition) never
declares ``NSMicrophoneUsageDescription``, so Qt6 refuses to even ask for
microphone access and a recording silently produces a WAV header with zero
samples — but that's the ONE platform anyone actually measured. Linux and
Windows have not been tried even once, and an earlier version of this file
showed the button there on the unverified assumption that they'd be fine —
exactly the guess-instead-of-measurement this project has already paid for
once, on the OAuth token capture. So the flag is a single, unconditional
kill switch until each platform is actually verified, not a per-platform
guess.

Neither recording nor the network runs on the main thread: recording itself
goes through `QMediaRecorder` (asynchronous and event-driven, so it doesn't
block the UI), and the whisper upload runs on its own `QThread`
(`_UploadWorker`) so a network timeout can't hang Houdini.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Protocol

from .. import network
from ..logbook import logger as _logbook_logger
from .worker import Worker
from .qt import QtCore, QtGui, QtWidgets, Signal

#: This module logged NOTHING until a real failure had to be diagnosed
#: over ssh by running `xxd` on the owner's own temporary files. Every
#: recording had produced 16 bytes — `RIFF....WAVEfmt ` and no samples —
#: and the panel's own log had not a single line to say so.
#:
#: The same blind spot cost three consecutive wrong releases on the OAuth
#: token (docs/facts/acp-sdk.md §25-§28): the code recorded its result and
#: never the path it took to get there, so each cause had to be guessed.
#: What goes in here is chosen to make that impossible a second time —
#: above all the size of the file that was produced, which names this
#: entire class of failure at a glance.
#:
#: Never logged: the API key, and the transcribed text. That text is the
#: artist speaking, in their own room; its LENGTH is diagnostic, its
#: content is theirs. The endpoint is not a secret and is logged.
_log = _logbook_logger("houdini_agent_panel.ui.voice")

#: THE single switch for whether voice input (composer mic button, Settings
#: → Voice) is offered at all. `False` unconditionally, on every platform,
#: until each one has actually been VERIFIED — a real Houdini install
#: recording real audio end to end, not "should work" reasoning.
#:
#: What's measured so far (macOS only, one evening's work by the owner):
#: none of Houdini's ten installed bundles (20.5/21.0/22.0, every edition)
#: declare `NSMicrophoneUsageDescription` in their `Info.plist`, so macOS's
#: TCC never lists Houdini under Privacy & Security at all, and Qt6
#: (`QMicrophonePermission`) refuses to even request access — a recording
#: still "succeeds", with no error, and produces a WAV header with zero
#: audio samples.
#:
#: Linux and Windows have NOT been tried even once — no install, no real
#: microphone, nothing. An earlier version of this fix showed the button
#: there anyway, on the assumption that "no known TCC-equivalent" meant "it
#: works" — that's a guess standing in for a measurement, the same mistake
#: that already cost several wrong releases this week on the OAuth token
#: capture (docs/facts/acp-sdk.md §25-§28). So: off everywhere, regardless
#: of `sys.platform`, until proven otherwise.
#:
#: To bring it back: on each target platform, install the panel the way an
#: artist would and confirm — not assume — that a real recording produces a
#: playable audio file. Then flip this flag (per platform if they turn out
#: to differ, or drop it entirely if all of them check out) in this one
#: place; `recording_available()`, right below, is the only thing that
#: reads it.
_VOICE_INPUT_AVAILABLE = False

#: The button's tooltip outside of an error — restored once a recording
#: succeeds, so a stale "key rejected" message from three uploads ago never
#: lingers over an otherwise-working button.
_DEFAULT_TOOLTIP = "Voice input"

#: How long `_stop_recording` waits for the backend to actually report the
#: file is finalized before giving up. `QMediaRecorder.stop()`/
#: `QAudioRecorder.stop()` are asynchronous — a wedged driver or a Qt bug
#: withholding the state-changed signal must not leave the button looking
#: stuck forever.
_STOP_TIMEOUT_MS = 5000

#: A PCM WAV's fixed header is 44 bytes; a file at or below that has no
#: audio samples in it at all — just the RIFF/WAVE preamble, or nothing.
_MIN_VALID_WAV_BYTES = 44

#: The OpenAI-compatible path the documented whisper service (project's
#: `whisper` skill / `whi.ai-vfx.com`) actually serves transcriptions at.
_WHISPER_TRANSCRIPTION_PATH = "/v1/audio/transcriptions"

_EMPTY_RECORDING_MACOS = (
    "The recording captured no audio. Houdini's own app bundle doesn't "
    "declare microphone use to macOS, so it may not even be listed under "
    "System Settings → Privacy & Security → Microphone — add it "
    "there (or remove and re-add the entry) and try again."
)
_EMPTY_RECORDING_GENERIC = (
    "The recording captured no audio — check that a microphone is "
    "connected and try again."
)

#: What `recording_available()` reports while `_VOICE_INPUT_AVAILABLE` is
#: `False` — deliberately generic (no platform names, no Qt/TCC jargon):
#: this is the one string an artist might actually see, in the Settings
#: caption that replaces the whole Voice section. The technical account
#: lives in `_VOICE_INPUT_AVAILABLE`'s own comment, for whoever returns to
#: flip it back.
_VOICE_INPUT_DISABLED_REASON = (
    "Voice input is temporarily turned off while it's verified on every "
    "platform."
)


def _import_qtmultimedia():
    """The same three-step path as `ui/qt.py`, but local.

    `QtMultimedia` isn't in the set `ui/qt.py` re-exports, and outside
    Houdini it may not be in the environment at all (CI with no system audio
    stack, for instance) — hence a soft `None` rather than an ImportError
    escaping.
    """
    for modpath in ("hutil.PySide", "PySide6", "PySide2"):
        try:
            module = __import__(modpath, fromlist=["QtMultimedia"])
            return getattr(module, "QtMultimedia")
        except (ImportError, AttributeError):
            continue
    return None


class Uploader(Protocol):
    def __call__(self, endpoint: str, audio_path: Path, mime_type: str, api_key: str = "") -> str: ...


def _normalize_whisper_endpoint(url: str) -> str:
    """Fill in the transcription path when the artist typed a bare host.

    Settings → Voice only asks for a base address — `GET /health` on the
    documented service answers at the bare host, but transcription itself
    lives at `/v1/audio/transcriptions`, so a POST straight to a bare host
    is a 404. A URL with no path (or just `/`) gets that path appended
    here; a URL that already names one is trusted as-is and left
    untouched, so a self-hosted whisper mounted somewhere else keeps
    working exactly as configured.
    """
    parts = urllib.parse.urlsplit(url)
    if not parts.scheme or not parts.netloc:
        # Not a parseable absolute URL (or empty) — nothing sensible to
        # append; let it fail downstream exactly as it does today.
        return url
    if parts.path in ("", "/"):
        return urllib.parse.urlunsplit((
            parts.scheme,
            parts.netloc,
            _WHISPER_TRANSCRIPTION_PATH,
            parts.query,
            parts.fragment,
        ))
    return url


def _looks_like_real_audio(path: Path) -> bool:
    """`False` for a missing file or one that is only the RIFF/WAVE
    preamble with no audio samples — the exact shape of the bug this
    module guards against (see `VoiceButton._await_stop`)."""
    try:
        return path.stat().st_size > _MIN_VALID_WAV_BYTES
    except OSError:
        return False


def _empty_recording_message(*, platform: str = sys.platform) -> str:
    """The message for a recording that produced no usable audio.

    On macOS this is very likely microphone access Houdini never got:
    Houdini's own `Info.plist` (checked across 20.5, 21.0 and 22.0) has no
    `NSMicrophoneUsageDescription` key, so the OS never shows the usual
    permission prompt and QtMultimedia just receives no audio — no error,
    just silence, which is indistinguishable from "no mic present" through
    the Qt5/Qt6 recorder APIs. `platform` is a parameter (not a `sys`
    import at the call site) purely so tests can exercise both branches
    without needing to monkeypatch `sys.platform`.
    """
    return _EMPTY_RECORDING_MACOS if platform == "darwin" else _EMPTY_RECORDING_GENERIC


def default_uploader(
    endpoint: str, audio_path: Path, mime_type: str, api_key: str = "", *, opener=None
) -> str:
    """POST the recording to a whisper endpoint, with no third-party deps.

    A simple hand-rolled multipart form over `urllib`: dragging `requests`
    into the `--target` tree inside Houdini for the sake of one POST isn't
    worth it. The reply is expected to look like `{"text": "..."}` — the
    self-hosted service documented in the project's `whisper` skill returns
    exactly that shape for `response_format=json`, and so does a bare local
    whisper with no `response_format` field at all, which is why that field
    is added explicitly here rather than relied on as a default.

    Goes through `network`'s own opener (`opener=None`, the production
    case) so the studio proxy and CA bundle the artist configured in
    Settings apply here exactly as they do to every other request the panel
    makes — see `token_check.verify` for the same pattern. `opener` is for
    tests.

    `api_key`, when non-empty, is sent as `X-API-Key` — one of the two
    equivalent headers the documented service accepts (the other,
    `Authorization: Bearer`, is not used here to avoid colliding with a
    future OAuth-style credential on this same request). Empty means no
    header at all: a local, unauthenticated whisper must keep working with
    nothing configured.
    """
    boundary = uuid.uuid4().hex
    data = audio_path.read_bytes()
    file_part = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{audio_path.name}"\r\n'
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode("utf-8") + data + b"\r\n"
    format_part = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="response_format"\r\n\r\n'
        f"json\r\n"
    ).encode("utf-8")
    body = file_part + format_part + f"--{boundary}--\r\n".encode("utf-8")

    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    if api_key:
        headers["X-API-Key"] = api_key

    request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")
    director = opener if opener is not None else network._opener_director()
    try:
        with director.open(request, timeout=60.0) as response:
            # `.status` is the real `http.client.HTTPResponse` attribute;
            # tests hand in bare doubles that only implement `read()`, so
            # this stays best-effort rather than a hard requirement.
            status = getattr(response, "status", None)
            payload = response.read()
    except urllib.error.HTTPError as exc:
        _log.warning("voice: whisper POST %s -> HTTP %s", endpoint, exc.code)
        if exc.code == 401:
            # The one failure mode a silent empty result would hide
            # completely — see the module docstring on `VoiceButton._on_
            # upload_failed` for where this message ends up.
            raise RuntimeError(
                "Whisper rejected the API key (401) — check Settings → Voice."
            ) from exc
        if exc.code == 404:
            # The common misconfiguration this message exists for: a bare
            # `https://host` typed into Settings → Voice with no path.
            # `_normalize_whisper_endpoint` already fixes that case before
            # it gets here, but an explicit, wrong path still lands here.
            raise RuntimeError(
                f"No transcription endpoint at {endpoint} (404) — check Settings → Voice."
            ) from exc
        raise
    _log.info("voice: whisper POST %s -> HTTP %s", endpoint, status)
    parsed = json.loads(payload.decode("utf-8"))
    text = parsed.get("text") if isinstance(parsed, dict) else None
    return text if isinstance(text, str) else ""


class RecordBackend(Protocol):
    """Platform recording adapter — the only place PySide2 and PySide6 part
    ways. `start` writes WAV/PCM into `destination`.

    `stop` is asynchronous: both `QMediaRecorder.stop()` (Qt6) and
    `QAudioRecorder.stop()` (Qt5) return immediately while the container is
    still being flushed and closed in the background, so the backend
    reports completion through `on_finished` instead of `stop` itself
    returning. `on_finished` is called with `""` on a clean stop, or a
    human-readable error if the backend's own error signal fired during
    the recording — either way exactly once, and always on the Qt event
    loop the backend already lives on (never a worker thread), so a
    fake/synchronous backend in tests may call it directly from inside
    `stop`.
    """

    def start(self, destination: Path) -> None: ...
    def stop(self, on_finished: Callable[[str], None]) -> None: ...


class _OneShotCallback:
    """Fires a stored callback exactly once.

    Both Qt backends below race the same two triggers: a state-changed
    signal arriving after `stop()` returns, and the recorder already being
    in `StoppedState` by the time `stop()` is called (it can end a
    recording on its own, e.g. on a resource error, before the artist
    ever clicks the button). Either can plausibly fire first; this just
    makes sure only the first one counts.
    """

    def __init__(self) -> None:
        self._callback: Callable[[str], None] | None = None

    def arm(self, callback: Callable[[str], None]) -> None:
        self._callback = callback

    def fire(self, error: str) -> None:
        if self._callback is None:
            return
        callback, self._callback = self._callback, None
        callback(error)


class _Qt6RecordBackend:
    """PySide6/Qt6: `QMediaCaptureSession` + `QAudioInput` + `QMediaRecorder`."""

    def __init__(self, qtmultimedia) -> None:
        self._qtmultimedia = qtmultimedia
        self._session = qtmultimedia.QMediaCaptureSession()
        self._input = qtmultimedia.QAudioInput()
        self._session.setAudioInput(self._input)
        self._recorder = qtmultimedia.QMediaRecorder()
        self._session.setRecorder(self._recorder)
        self._done = _OneShotCallback()
        self._error_text = ""
        self._recorder.recorderStateChanged.connect(self._on_state_changed)
        self._recorder.errorOccurred.connect(self._on_error)

    def start(self, destination: Path) -> None:
        self._error_text = ""
        self._recorder.setOutputLocation(QtCore.QUrl.fromLocalFile(str(destination)))
        self._recorder.record()

    def stop(self, on_finished: Callable[[str], None]) -> None:
        self._done.arm(on_finished)
        self._recorder.stop()
        if self._recorder.recorderState() == self._qtmultimedia.QMediaRecorder.StoppedState:
            # Already stopped (e.g. an error ended it earlier) — the state
            # isn't changing, so recorderStateChanged won't fire again.
            self._done.fire(self._error_text)

    def _on_state_changed(self, state) -> None:
        if state == self._qtmultimedia.QMediaRecorder.StoppedState:
            self._done.fire(self._error_text)

    def _on_error(self, error, error_string) -> None:
        self._error_text = error_string or "Recording failed."
        _log.warning("voice: Qt6 recorder error: %s", self._error_text)


class _Qt5RecordBackend:
    """PySide2/Qt5: `QAudioRecorder` — a simple single-class API."""

    def __init__(self, qtmultimedia) -> None:
        self._qtmultimedia = qtmultimedia
        self._recorder = qtmultimedia.QAudioRecorder()
        self._done = _OneShotCallback()
        self._error_text = ""
        self._recorder.stateChanged.connect(self._on_state_changed)
        self._recorder.error.connect(self._on_error)

    def start(self, destination: Path) -> None:
        self._error_text = ""
        self._recorder.setOutputLocation(QtCore.QUrl.fromLocalFile(str(destination)))
        self._recorder.record()

    def stop(self, on_finished: Callable[[str], None]) -> None:
        self._done.arm(on_finished)
        self._recorder.stop()
        if self._recorder.state() == self._qtmultimedia.QMediaRecorder.StoppedState:
            self._done.fire(self._error_text)

    def _on_state_changed(self, state) -> None:
        if state == self._qtmultimedia.QMediaRecorder.StoppedState:
            self._done.fire(self._error_text)

    def _on_error(self, error) -> None:
        self._error_text = self._recorder.errorString() or "Recording failed."
        _log.warning("voice: Qt5 recorder error: %s", self._error_text)


def recording_available() -> tuple[bool, str]:
    """`(True, "")` or `(False, reason)` — whether voice input should be
    offered on this machine at all, right now.

    Currently just `_VOICE_INPUT_AVAILABLE` (`False`, unconditionally) —
    see that flag's own comment for the full account of why and what would
    flip it. No backend construction, no platform check, no network, no
    `hou` — this is meant to be the one place both `SettingsView` (the
    Voice section) and `VoiceButton.configure` (the composer's mic button)
    ask, so the two can never disagree about whether voice input is on.
    """
    if _VOICE_INPUT_AVAILABLE:
        return True, ""
    return False, _VOICE_INPUT_DISABLED_REASON


def build_default_backend() -> tuple[RecordBackend | None, str]:
    """`(backend, "")` or `(None, reason)` — the second element is diagnostics.

    Tries the Qt6 path, then the Qt5 path; any exception while constructing
    (no audio device, the platform backend unavailable and so on) also counts
    as "unavailable" rather than a reason to take the panel down.
    """
    qtmultimedia = _import_qtmultimedia()
    if qtmultimedia is None:
        _log.info("voice: no recording backend — QtMultimedia is unavailable")
        return None, "QtMultimedia is unavailable in this environment"
    if hasattr(qtmultimedia, "QMediaCaptureSession") and hasattr(qtmultimedia, "QAudioInput"):
        try:
            backend = _Qt6RecordBackend(qtmultimedia)
            _log.info("voice: recording backend is Qt6 (QMediaCaptureSession)")
            return backend, ""
        except Exception as exc:  # noqa: BLE001 - degrade, don't crash
            _log.info("voice: Qt6 recording backend unavailable: %r", exc)
            return None, f"QtMultimedia (Qt6): {exc!r}"
    if hasattr(qtmultimedia, "QAudioRecorder"):
        try:
            backend = _Qt5RecordBackend(qtmultimedia)
            _log.info("voice: recording backend is Qt5 (QAudioRecorder)")
            return backend, ""
        except Exception as exc:  # noqa: BLE001
            _log.info("voice: Qt5 recording backend unavailable: %r", exc)
            return None, f"QtMultimedia (Qt5): {exc!r}"
    return None, "QtMultimedia offers no recording API we know"


class _UploadWorker(Worker):
    """One POST to whisper, on its own thread so the network can't freeze the UI."""

    done = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        endpoint: str,
        audio_path: Path,
        mime_type: str,
        api_key: str,
        uploader: Uploader,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._endpoint = endpoint
        self._audio_path = audio_path
        self._mime_type = mime_type
        self._api_key = api_key
        self._uploader = uploader

    def work(self) -> None:  # noqa: D102 - QThread.run override
        try:
            text = self._uploader(self._endpoint, self._audio_path, self._mime_type, self._api_key)
        except Exception as exc:  # noqa: BLE001 - a network error must not take the panel down
            self.failed.emit(str(exc))
            return
        self.done.emit(text)


class VoiceButton(QtWidgets.QToolButton):
    """The microphone button. Lives inside `Composer` but knows nothing about
    it beyond what it reports through its own signals.

    `backend_factory`/`uploader` are parameters purely for testability: unit
    tests hand in a fake recorder and a fake network, touching neither a real
    microphone nor a real whisper.
    """

    recorded_audio = Signal(dict)  # a ready ACP audio block: {"type": "audio", ...}
    transcribed_text = Signal(str)  # text recognised by a local whisper
    failed = Signal(str)  # the reason, for diagnostics — never a modal at the user

    def __init__(
        self,
        parent=None,
        *,
        backend_factory: Callable[[], tuple[RecordBackend | None, str]] = build_default_backend,
        uploader: Uploader = default_uploader,
    ) -> None:
        super().__init__(parent)
        self._backend_factory = backend_factory
        self._uploader = uploader
        self._mode: str | None = None  # "audio" | "whisper" | None (hidden)
        self._whisper_endpoint = ""
        self._whisper_api_key = ""
        self._recording = False
        self._backend: RecordBackend | None = None
        self._backend_checked = False
        self._unavailable_reason = ""
        self._tmp_path: Path | None = None
        self._upload_thread: _UploadWorker | None = None

        self.setText("")
        self.setFixedSize(28, 28)
        self.setToolTip(_DEFAULT_TOOLTIP)
        self.setCheckable(True)
        self.setVisible(False)
        self.clicked.connect(self._on_clicked)

    def paintEvent(self, event: QtGui.QPaintEvent) -> None:  # noqa: N802
        """Draw a crisp platform-independent mic instead of an emoji glyph."""
        super().paintEvent(event)
        painter = QtGui.QPainter(self)
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        color = self.palette().color(QtGui.QPalette.ButtonText)
        if self._recording:
            color = self.palette().color(QtGui.QPalette.Highlight)
        pen = QtGui.QPen(color, 1.4)
        pen.setCapStyle(QtCore.Qt.RoundCap)
        painter.setPen(pen)
        painter.setBrush(QtCore.Qt.NoBrush)
        painter.drawRoundedRect(QtCore.QRectF(10.5, 5.0, 7.0, 11.0), 3.5, 3.5)
        painter.drawArc(QtCore.QRectF(7.5, 8.5, 13.0, 11.0), 180 * 16, 180 * 16)
        painter.drawLine(QtCore.QPointF(14.0, 19.5), QtCore.QPointF(14.0, 22.0))
        painter.drawLine(QtCore.QPointF(10.5, 22.0), QtCore.QPointF(17.5, 22.0))

    # --- public --------------------------------------------------------

    def configure(
        self, *, supports_audio: bool, whisper_endpoint: str, whisper_api_key: str = ""
    ) -> None:
        """Recompute the button's visibility for fresh agent capabilities.

        The order of preference comes straight from design.md: the agent's
        `audio` beats a local whisper, because it saves a transcription step.

        `whisper_api_key` is optional and defaults to empty — a caller that
        hasn't been updated to pass it (there is none in this codebase, but
        `configure` is a public contract) still gets the old, unauthenticated
        behaviour rather than a `TypeError`.

        The backend still gets constructed and checked below even while
        `recording_available()` says no (see its own docstring /
        `_VOICE_INPUT_AVAILABLE`) — only the final visibility decision is
        overridden. That keeps `_start_recording`/`_stop_recording` (and
        everything downstream of them: upload, logging, the empty-recording
        guard) fully wired and testable independent of whether the button
        is ever shown to an artist.
        """
        self._whisper_endpoint = _normalize_whisper_endpoint(whisper_endpoint or "")
        self._whisper_api_key = whisper_api_key or ""
        if supports_audio:
            self._mode = "audio"
        elif self._whisper_endpoint:
            self._mode = "whisper"
        else:
            self._mode = None

        if self._mode is None:
            self.setVisible(False)
            return

        if not self._backend_checked:
            self._backend, self._unavailable_reason = self._backend_factory()
            self._backend_checked = True

        if self._backend is None:
            self.setVisible(False)
            self.failed.emit(self._unavailable_reason)
            return

        available, reason = recording_available()
        if not available:
            self.setVisible(False)
            _log.info("voice: hidden — %s", reason)
            self.failed.emit(reason)
            return
        self.setVisible(True)

    def is_available(self) -> bool:
        """Whether the backend actually constructed — independent of
        `_apply_availability`'s own visibility decision above, which stays
        off while `_VOICE_INPUT_AVAILABLE` is unconditionally False. Kept
        (unlike its sibling `unavailable_reason`, dropped as unused
        elsewhere): `test_voice.py` still asserts on this directly as the
        sanity check that a backend was wired even though the button
        itself never becomes visible."""
        return self._backend is not None

    # --- recording -------------------------------------------------------

    def _on_clicked(self) -> None:
        if self._recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self) -> None:
        if self._backend is None:
            return
        # A fresh attempt — an error tooltip left over from a previous
        # failed upload must not keep reading as the current state.
        self.setToolTip(_DEFAULT_TOOLTIP)
        self._tmp_path = Path(tempfile.gettempdir()) / f"hap-voice-{uuid.uuid4().hex}.wav"
        _log.info("voice: recording to %s", self._tmp_path)
        self._backend.start(self._tmp_path)
        self._recording = True
        self.setChecked(True)

    def _stop_recording(self) -> None:
        if self._backend is None:
            return
        self._recording = False
        self.setChecked(False)
        path, self._tmp_path = self._tmp_path, None
        if path is None:
            self._backend.stop(lambda error: None)
            return
        self._await_stop(path)

    def _await_stop(self, path: Path) -> None:
        """Wait for the backend to actually report the file is finalized
        before reading it — see `RecordBackend`'s own docstring for why
        `stop()` alone isn't enough. Bounded by `_STOP_TIMEOUT_MS` so a
        backend that never reports completion leaves a clear error rather
        than a button that looks stuck forever. No busy loop: both the
        timer and the backend's own signal resolve on the Qt event loop
        Houdini is already running, same as every other async thing in
        this widget.
        """
        resolved = _OneShotCallback()
        timer = QtCore.QTimer(self)
        timer.setSingleShot(True)
        started = time.monotonic()
        timed_out = False

        def finish(error: str) -> None:
            timer.stop()
            timer.deleteLater()
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if timed_out:
                _log.warning("voice: stop timed out after %dms", elapsed_ms)
            else:
                _log.info("voice: stop signal received after %dms", elapsed_ms)
            self._handle_stopped(path, error)

        def on_timeout() -> None:
            nonlocal timed_out
            timed_out = True
            resolved.fire("Recording did not finish saving in time — try again.")

        resolved.arm(finish)
        timer.timeout.connect(on_timeout)
        timer.start(_STOP_TIMEOUT_MS)
        self._backend.stop(resolved.fire)

    def _handle_stopped(self, path: Path, error: str) -> None:
        # The size is the whole diagnosis for the failure this module was
        # rebuilt around: 16 bytes is a header and no audio, and that was
        # invisible everywhere until someone read the file by hand.
        try:
            size = path.stat().st_size
        except OSError:
            size = -1
        _log.info(
            "voice: recording finished, %s bytes%s",
            size if size >= 0 else "no file, 0",
            f", backend error: {error}" if error else "",
        )
        if error:
            self._on_upload_failed(error)
            return
        if not _looks_like_real_audio(path):
            self._on_upload_failed(_empty_recording_message())
            return
        if self._mode == "audio":
            self._emit_audio_block(path)
        elif self._mode == "whisper":
            self._start_upload(path)

    def _emit_audio_block(self, path: Path) -> None:
        try:
            data = path.read_bytes()
        except OSError as exc:
            self.failed.emit(str(exc))
            return
        mime_type = mimetypes.guess_type(str(path))[0] or "audio/wav"
        block = {"type": "audio", "data": base64.b64encode(data).decode("ascii"), "mimeType": mime_type}
        self.recorded_audio.emit(block)

    def _start_upload(self, path: Path) -> None:
        mime_type = mimetypes.guess_type(str(path))[0] or "audio/wav"
        # The endpoint is not a secret, and getting it wrong — a base URL
        # with no path — was one of the two faults here. The key is never
        # logged; only whether one is set at all.
        _log.info(
            "voice: uploading to %s (api key: %s)",
            self._whisper_endpoint,
            "set" if self._whisper_api_key else "none",
        )
        self._upload_thread = _UploadWorker(
            self._whisper_endpoint, path, mime_type, self._whisper_api_key, self._uploader, self
        )
        self._upload_thread.done.connect(self._on_upload_done)
        self._upload_thread.failed.connect(self._on_upload_failed)
        self._upload_thread.start()

    def _on_upload_done(self, text: str) -> None:
        # Length only. The content is the artist speaking in their own
        # room — diagnostic value lives entirely in "did anything come
        # back", and none of it in what was said.
        _log.info("voice: transcribed %d characters", len(text))
        if text:
            self.transcribed_text.emit(text)

    def _on_upload_failed(self, reason: str) -> None:
        """A failed transcription must not read to the artist as "I said
        nothing and nothing happened" — `failed`'s own docstring rules out
        a modal, so the tooltip is the one visible surface this button
        already has (the same one `configure()` uses for "no audio backend
        here"). `default_uploader` raises a specifically worded message for
        a rejected API key; anything else (timeout, DNS, a malformed
        response) still lands here as whatever `str(exc)` gave.
        """
        _log.info("voice: failed — %s", reason)
        self.setToolTip(reason or _DEFAULT_TOOLTIP)
        self.failed.emit(reason)

    def shutdown(self) -> None:
        """Release the upload thread if one is still running — called
        from `Composer.shutdown()`.

        `_upload_thread` is parented to THIS widget, so a slow whisper
        upload still in flight when the panel closes is the same hazard
        as any other worker here: a `QThread` still running when its
        parent is destroyed is `qFatal()`/`SIGABRT`, not a warning
        (docs/facts/houdini.md §14). See `ui/worker.py::release`'s own
        docstring for why a bare `wait()` isn't enough.
        """
        from .worker import release

        release(self._upload_thread)
        self._upload_thread = None


__all__ = [
    "VoiceButton",
    "RecordBackend",
    "Uploader",
    "build_default_backend",
    "default_uploader",
    "recording_available",
]

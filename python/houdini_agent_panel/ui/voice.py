"""The composer's microphone button: recording and (optionally) whisper.

design.md's rule for this module: an agent with the ``audio`` capability gets
the audio block as-is; an agent without it but with a local whisper endpoint
configured gets recognised text; neither, and there is no button at all. The
``QtMultimedia`` API itself diverged between PySide2 (``QAudioRecorder``, Qt5)
and PySide6 (``QMediaCaptureSession``/``QAudioInput``/``QMediaRecorder``,
Qt6), so there is no single path — the module tries both and honestly hides
the button if neither came together, rather than pretending recording works.

Neither recording nor the network runs on the main thread: recording itself
goes through `QMediaRecorder` (asynchronous and event-driven, so it doesn't
block the UI), and the whisper upload runs on its own `QThread`
(`_UploadWorker`) so a network timeout can't hang Houdini.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import tempfile
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Callable, Protocol

from .. import network
from .worker import Worker
from .qt import QtCore, QtGui, QtWidgets, Signal

#: The button's tooltip outside of an error — restored once a recording
#: succeeds, so a stale "key rejected" message from three uploads ago never
#: lingers over an otherwise-working button.
_DEFAULT_TOOLTIP = "Voice input"


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
            payload = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            # The one failure mode a silent empty result would hide
            # completely — see the module docstring on `VoiceButton._on_
            # upload_failed` for where this message ends up.
            raise RuntimeError(
                "Whisper rejected the API key (401) — check Settings → Voice."
            ) from exc
        raise
    parsed = json.loads(payload.decode("utf-8"))
    text = parsed.get("text") if isinstance(parsed, dict) else None
    return text if isinstance(text, str) else ""


class RecordBackend(Protocol):
    """Platform recording adapter — the only place PySide2 and PySide6 part
    ways. `start`/`stop` write WAV/PCM into `destination`."""

    def start(self, destination: Path) -> None: ...
    def stop(self) -> None: ...


class _Qt6RecordBackend:
    """PySide6/Qt6: `QMediaCaptureSession` + `QAudioInput` + `QMediaRecorder`."""

    def __init__(self, qtmultimedia) -> None:
        self._session = qtmultimedia.QMediaCaptureSession()
        self._input = qtmultimedia.QAudioInput()
        self._session.setAudioInput(self._input)
        self._recorder = qtmultimedia.QMediaRecorder()
        self._session.setRecorder(self._recorder)

    def start(self, destination: Path) -> None:
        self._recorder.setOutputLocation(QtCore.QUrl.fromLocalFile(str(destination)))
        self._recorder.record()

    def stop(self) -> None:
        self._recorder.stop()


class _Qt5RecordBackend:
    """PySide2/Qt5: `QAudioRecorder` — a simple single-class API."""

    def __init__(self, qtmultimedia) -> None:
        self._recorder = qtmultimedia.QAudioRecorder()

    def start(self, destination: Path) -> None:
        self._recorder.setOutputLocation(QtCore.QUrl.fromLocalFile(str(destination)))
        self._recorder.record()

    def stop(self) -> None:
        self._recorder.stop()


def build_default_backend() -> tuple[RecordBackend | None, str]:
    """`(backend, "")` or `(None, reason)` — the second element is diagnostics.

    Tries the Qt6 path, then the Qt5 path; any exception while constructing
    (no audio device, the platform backend unavailable and so on) also counts
    as "unavailable" rather than a reason to take the panel down.
    """
    qtmultimedia = _import_qtmultimedia()
    if qtmultimedia is None:
        return None, "QtMultimedia is unavailable in this environment"
    if hasattr(qtmultimedia, "QMediaCaptureSession") and hasattr(qtmultimedia, "QAudioInput"):
        try:
            return _Qt6RecordBackend(qtmultimedia), ""
        except Exception as exc:  # noqa: BLE001 - degrade, don't crash
            return None, f"QtMultimedia (Qt6): {exc!r}"
    if hasattr(qtmultimedia, "QAudioRecorder"):
        try:
            return _Qt5RecordBackend(qtmultimedia), ""
        except Exception as exc:  # noqa: BLE001
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
        """
        self._whisper_endpoint = whisper_endpoint or ""
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
        self.setVisible(True)

    def is_available(self) -> bool:
        return self._backend is not None

    def unavailable_reason(self) -> str:
        return self._unavailable_reason

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
        self._backend.start(self._tmp_path)
        self._recording = True
        self.setChecked(True)

    def _stop_recording(self) -> None:
        if self._backend is None:
            return
        self._backend.stop()
        self._recording = False
        self.setChecked(False)
        if self._tmp_path is None:
            return
        path, self._tmp_path = self._tmp_path, None
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
        self._upload_thread = _UploadWorker(
            self._whisper_endpoint, path, mime_type, self._whisper_api_key, self._uploader, self
        )
        self._upload_thread.done.connect(self._on_upload_done)
        self._upload_thread.failed.connect(self._on_upload_failed)
        self._upload_thread.start()

    def _on_upload_done(self, text: str) -> None:
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


__all__ = ["VoiceButton", "RecordBackend", "Uploader", "build_default_backend", "default_uploader"]

"""The composer's microphone button: recording and (optionally) whisper.

design.md's rule for this module: an agent with the ``audio`` capability gets
the audio block as-is; an agent without it but with a local whisper endpoint
configured gets recognised text; neither, and there is no button at all. The
``QtMultimedia`` API itself diverged between PySide2 (``QAudioRecorder``, Qt5)
and PySide6 (``QMediaCaptureSession``/``QAudioInput``/``QMediaRecorder``,
Qt6), so there is no single path — the module tries both and honestly hides
the button if neither came together, rather than pretending recording works.

On macOS, "came together" also means the OS will actually let it record.
Houdini's own bundle (checked across 20.5/21.0/22.0, every edition) never
declares ``NSMicrophoneUsageDescription``, so Qt6 flatly refuses to even ask
for microphone access — not a crash, just a permission stuck forever at
``Undetermined`` and a recording that starts, runs, and produces silence.
``build_default_backend``/``recording_available`` check for exactly this
(via ``QMicrophonePermission``, Qt6 only) and report "unavailable" rather
than let the button or the Settings → Voice section promise something that
can only fail; see ``_qt6_permission_block_reason``.

Neither recording nor the network runs on the main thread: recording itself
goes through `QMediaRecorder` (asynchronous and event-driven, so it doesn't
block the UI), and the whisper upload runs on its own `QThread`
(`_UploadWorker`) so a network timeout can't hang Houdini.
"""

from __future__ import annotations

import base64
import json
import mimetypes
import plistlib
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

#: Why recording is refused outright (button/section hidden) rather than
#: offered and left to fail the way `_EMPTY_RECORDING_MACOS` describes —
#: see `_qt6_permission_block_reason`. `Denied` is the ordinary case: the
#: artist (or a studio image) turned it off, and System Settings can turn
#: it back on. `NO_ENTITLEMENT` is the one this whole module was rebuilt
#: around: Houdini's own bundle (checked across 20.5/21.0/22.0, every
#: edition) has never declared `NSMicrophoneUsageDescription`, so macOS
#: never shows Houdini in Privacy & Security at all, and Qt itself refuses
#: to even ask — no amount of clicking in System Settings fixes this one.
_MIC_BLOCKED_DENIED = (
    "Microphone access for Houdini is turned off in macOS Privacy & "
    "Security settings."
)
_MIC_BLOCKED_NO_ENTITLEMENT = (
    "Houdini's own app bundle doesn't declare microphone use to macOS (no "
    "NSMicrophoneUsageDescription in its Info.plist), so macOS will never "
    "prompt for access and the panel can't request it either — this isn't "
    "something the artist can fix from System Settings."
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


def _main_bundle_info_plist_path(*, executable: str = "") -> Path | None:
    """The `Contents/Info.plist` of whatever `.app` bundle launched the
    current process — the exact bundle Qt itself reads
    `NSMicrophoneUsageDescription` from (its own refusal names this file
    explicitly: `qt.permissions: Requesting QMicrophonePermission requires
    "NSMicrophoneUsageDescription" in Info.plist`).

    Found by walking up from the interpreter binary rather than any
    hardcoded Houdini path — Houdini alone ships ten different bundles
    across 20.5/21.0/22.0 (every edition), and whichever one is actually
    running has to resolve here the same way, with no special-casing.
    `executable` is a parameter purely so a test can point this at a fake
    bundle tree instead of `sys.executable`.
    """
    for parent in Path(executable or sys.executable).resolve().parents:
        if parent.suffix == ".app":
            plist = parent / "Contents" / "Info.plist"
            return plist if plist.is_file() else None
    return None


def _bundle_declares_microphone_usage(*, executable: str = "") -> bool:
    """Whether the running process's own bundle names
    `NSMicrophoneUsageDescription` — read from the real Info.plist, not
    assumed: Houdini's bundle has never had this key in any of the ten
    installs checked (20.5/21.0/22.0, every edition), but a future SideFX
    build — or a studio's own re-signed copy — might, and this must notice
    the day that changes rather than keep hiding voice input forever.
    """
    plist_path = _main_bundle_info_plist_path(executable=executable)
    if plist_path is None:
        return False
    try:
        with plist_path.open("rb") as fh:
            info = plistlib.load(fh)
    except (OSError, plistlib.InvalidFileException):
        return False
    return "NSMicrophoneUsageDescription" in info


def _qt6_permission_status(*, app=None):
    """`QCoreApplication.checkPermission` for the microphone, or `None` if
    there's no `QMicrophonePermission` API at all (PySide2/Qt5, Houdini
    20.5) or no live `QApplication` yet to ask. Split out from
    `_qt6_permission_block_reason` purely so a test can hand in a stand-in
    `app` instead of monkeypatching the real, process-wide `QApplication`
    singleton's own method.
    """
    permission_cls = getattr(QtCore, "QMicrophonePermission", None)
    if permission_cls is None:
        return None
    app = app if app is not None else QtWidgets.QApplication.instance()
    if app is None:
        return None
    return app.checkPermission(permission_cls())


def _qt6_permission_block_reason(
    *, platform: str = "", app=None, bundle_declares_microphone: bool | None = None
) -> str:
    """"" if Qt6 recording may proceed on this machine, else the reason it
    can never work right now.

    Only macOS has TCC (and therefore anything to check here) — Qt5
    (PySide2, Houdini 20.5) has no `QMicrophonePermission` at all, which
    `_qt6_permission_status` already turns into "nothing to report" on its
    own, so callers on that binding get "" here regardless of platform.
    `Denied` is unconditional. `Undetermined` — Qt has never been asked —
    only blocks when the bundle's own Info.plist can't back up that ask
    (see `_bundle_declares_microphone_usage`); a `Granted` app, or an
    `Undetermined` one with the key present, is left to actually try.

    `platform`/`app`/`bundle_declares_microphone` are injectable purely
    for tests, the same idiom `_empty_recording_message`'s own `platform`
    parameter already uses — production callers (`_blocked_reason`) pass
    none of them and get the real `sys.platform`/`QApplication`/bundle
    read.
    """
    if (platform or sys.platform) != "darwin":
        return ""
    status = _qt6_permission_status(app=app)
    if status is None:
        return ""
    permission_status = QtCore.Qt.PermissionStatus
    if status == permission_status.Denied:
        _log.info("voice: microphone permission is Denied")
        return _MIC_BLOCKED_DENIED
    if status == permission_status.Undetermined:
        declares = (
            bundle_declares_microphone
            if bundle_declares_microphone is not None
            else _bundle_declares_microphone_usage()
        )
        if not declares:
            _log.info(
                "voice: microphone permission is Undetermined and the app "
                "bundle has no NSMicrophoneUsageDescription — access can "
                "never be requested"
            )
            return _MIC_BLOCKED_NO_ENTITLEMENT
    return ""


def _blocked_reason(qtmultimedia) -> str:
    """"" if recording may work on this machine, else why it can't.

    Never constructs a session/recorder/input object — only inspects what
    QtMultimedia offers and, on macOS + Qt6, what TCC would even let the
    panel do with it. This is the one function `build_default_backend`
    (below) and `recording_available` both defer to, so a button and a
    settings section can never disagree about whether recording works
    here.
    """
    if qtmultimedia is None:
        return "QtMultimedia is unavailable in this environment"
    if hasattr(qtmultimedia, "QMediaCaptureSession") and hasattr(qtmultimedia, "QAudioInput"):
        return _qt6_permission_block_reason()
    if hasattr(qtmultimedia, "QAudioRecorder"):
        return ""
    return "QtMultimedia offers no recording API we know"


def recording_available() -> tuple[bool, str]:
    """`(True, "")` or `(False, reason)` — cheap, side-effect-free read of
    "can this machine record audio at all", with no backend/session/
    recorder construction and no network or `hou` access.

    Shared by `SettingsView` (whether to draw the Voice section at all —
    design.md's "the agent doesn't support it — the control doesn't get
    drawn" applies just as much to a hardware/OS reason as an agent one)
    and `build_default_backend` (whether to even try). The two must never
    disagree, on pain of a Voice section with fields for a button that can
    never appear.
    """
    reason = _blocked_reason(_import_qtmultimedia())
    return (not reason, reason)


def build_default_backend() -> tuple[RecordBackend | None, str]:
    """`(backend, "")` or `(None, reason)` — the second element is diagnostics.

    Tries the Qt6 path, then the Qt5 path; any exception while constructing
    (no audio device, the platform backend unavailable and so on) also counts
    as "unavailable" rather than a reason to take the panel down. Layered on
    `_blocked_reason`: a machine that can never grant microphone access
    (macOS Qt6 with the permission Denied, or Undetermined with no
    `NSMicrophoneUsageDescription` in Houdini's own bundle — see
    `_qt6_permission_block_reason`) is unavailable here too, without ever
    constructing a `QMediaRecorder` that would only fail silently the
    moment `start()` is actually called.
    """
    qtmultimedia = _import_qtmultimedia()
    reason = _blocked_reason(qtmultimedia)
    if reason:
        _log.info("voice: no recording backend — %s", reason)
        return None, reason
    if hasattr(qtmultimedia, "QMediaCaptureSession") and hasattr(qtmultimedia, "QAudioInput"):
        try:
            backend = _Qt6RecordBackend(qtmultimedia)
        except Exception as exc:  # noqa: BLE001 - degrade, don't crash
            _log.info("voice: Qt6 recording backend unavailable: %r", exc)
            return None, f"QtMultimedia (Qt6): {exc!r}"
        _log.info("voice: recording backend is Qt6 (QMediaCaptureSession)")
        return backend, ""
    try:
        backend = _Qt5RecordBackend(qtmultimedia)
    except Exception as exc:  # noqa: BLE001
        _log.info("voice: Qt5 recording backend unavailable: %r", exc)
        return None, f"QtMultimedia (Qt5): {exc!r}"
    _log.info("voice: recording backend is Qt5 (QAudioRecorder)")
    return backend, ""


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

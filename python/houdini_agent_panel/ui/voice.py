"""Кнопка микрофона композера: запись с микрофона и (опционально) whisper.

Правило design.md для этого модуля: агент с capability ``audio`` получает
аудио-блок как есть; агент без неё, но с настроенным локальным whisper —
получает распознанный текст; ни того ни другого — кнопки нет вообще. Само
API ``QtMultimedia`` разошлось между PySide2 (``QAudioRecorder``, Qt5) и
PySide6 (``QMediaCaptureSession``/``QAudioInput``/``QMediaRecorder``, Qt6),
поэтому единого пути нет — модуль пробует оба и честно прячет кнопку, если ни
один не собрался, вместо того чтобы притворяться, что запись работает.

Запись и сеть — не на главном потоке: сама запись идёт через `QMediaRecorder`
(асинхронный, событийный — не блокирует UI), а загрузка на whisper — в
отдельном `QThread` (`_UploadWorker`), чтобы сетевой таймаут не подвесил
Houdini.
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

from .qt import QtCore, QtGui, QtWidgets, Signal


def _import_qtmultimedia():
    """Тот же трёхступенчатый путь, что и `ui/qt.py`, но локально.

    `QtMultimedia` — не в наборе, который реэкспортирует `ui/qt.py`, и вне
    Houdini его вообще может не быть в окружении (нет системного аудио-стека
    у CI, например) — отсюда мягкий возврат `None`, а не ImportError наружу.
    """
    for modpath in ("hutil.PySide", "PySide6", "PySide2"):
        try:
            module = __import__(modpath, fromlist=["QtMultimedia"])
            return getattr(module, "QtMultimedia")
        except (ImportError, AttributeError):
            continue
    return None


class Uploader(Protocol):
    def __call__(self, endpoint: str, audio_path: Path, mime_type: str) -> str: ...


def default_uploader(endpoint: str, audio_path: Path, mime_type: str) -> str:
    """POST записи на локальный whisper-эндпоинт, без сторонних зависимостей.

    Простая multipart-форма руками через `urllib`: тащить `requests` в
    `--target`-дерево внутри Houdini ради одного POST-запроса того не стоит.
    Ответ ожидается вида `{"text": "..."}` — этого достаточно для локального
    whisper (см. `whisper`-скилл проекта, тот же контракт).
    """
    boundary = uuid.uuid4().hex
    data = audio_path.read_bytes()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{audio_path.name}"\r\n'
        f"Content-Type: {mime_type}\r\n\r\n"
    ).encode("utf-8") + data + f"\r\n--{boundary}--\r\n".encode("utf-8")
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60.0) as response:
        payload = response.read()
    parsed = json.loads(payload.decode("utf-8"))
    text = parsed.get("text") if isinstance(parsed, dict) else None
    return text if isinstance(text, str) else ""


class RecordBackend(Protocol):
    """Платформенный адаптер записи — единственное место, где расходятся
    PySide2/PySide6. `start`/`stop` пишут WAV/PCM в `destination`."""

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
    """PySide2/Qt5: `QAudioRecorder` — простой одноклассовый API."""

    def __init__(self, qtmultimedia) -> None:
        self._recorder = qtmultimedia.QAudioRecorder()

    def start(self, destination: Path) -> None:
        self._recorder.setOutputLocation(QtCore.QUrl.fromLocalFile(str(destination)))
        self._recorder.record()

    def stop(self) -> None:
        self._recorder.stop()


def build_default_backend() -> tuple[RecordBackend | None, str]:
    """`(backend, "")` либо `(None, причина)` — вторым элементом диагностика.

    Пробует Qt6-путь, затем Qt5-путь; любое исключение при конструировании
    (нет аудио-устройства, недоступен бэкенд платформы и т.п.) — это тоже
    "недоступно", а не повод уронить панель.
    """
    qtmultimedia = _import_qtmultimedia()
    if qtmultimedia is None:
        return None, "QtMultimedia недоступен в этом окружении"
    if hasattr(qtmultimedia, "QMediaCaptureSession") and hasattr(qtmultimedia, "QAudioInput"):
        try:
            return _Qt6RecordBackend(qtmultimedia), ""
        except Exception as exc:  # noqa: BLE001 - деградация, не падение
            return None, f"QtMultimedia (Qt6): {exc!r}"
    if hasattr(qtmultimedia, "QAudioRecorder"):
        try:
            return _Qt5RecordBackend(qtmultimedia), ""
        except Exception as exc:  # noqa: BLE001
            return None, f"QtMultimedia (Qt5): {exc!r}"
    return None, "QtMultimedia не даёт известного API записи"


class _UploadWorker(QtCore.QThread):
    """Один POST на whisper — на своём потоке, чтобы сеть не морозила UI."""

    done = Signal(str)
    failed = Signal(str)

    def __init__(self, endpoint: str, audio_path: Path, mime_type: str, uploader: Uploader, parent=None) -> None:
        super().__init__(parent)
        self._endpoint = endpoint
        self._audio_path = audio_path
        self._mime_type = mime_type
        self._uploader = uploader

    def run(self) -> None:  # noqa: D102 - переопределение QThread.run
        try:
            text = self._uploader(self._endpoint, self._audio_path, self._mime_type)
        except Exception as exc:  # noqa: BLE001 - сетевая ошибка не должна ронять панель
            self.failed.emit(str(exc))
            return
        self.done.emit(text)


class VoiceButton(QtWidgets.QToolButton):
    """Кнопка микрофона. Живёт в `Composer`, но не знает о нём ничего, кроме
    того, что сообщает через свои сигналы.

    `backend_factory`/`uploader` — параметры именно ради тестируемости: юнит-
    тесты подсовывают фейковую запись и фейковую сеть, не трогая ни настоящий
    микрофон, ни настоящий whisper.
    """

    recorded_audio = Signal(dict)  # готовый ACP audio-блок: {"type": "audio", ...}
    transcribed_text = Signal(str)  # текст, распознанный локальным whisper
    failed = Signal(str)  # причина — в диагностику, не модалкой пользователю

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
        self._mode: str | None = None  # "audio" | "whisper" | None (скрыта)
        self._whisper_endpoint = ""
        self._recording = False
        self._backend: RecordBackend | None = None
        self._backend_checked = False
        self._unavailable_reason = ""
        self._tmp_path: Path | None = None
        self._upload_thread: _UploadWorker | None = None

        self.setText("")
        self.setFixedSize(28, 28)
        self.setToolTip("Голосовой ввод")
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

    # --- публичное -----------------------------------------------------

    def configure(self, *, supports_audio: bool, whisper_endpoint: str) -> None:
        """Пересчитать видимость кнопки под свежие capability агента.

        Порядок предпочтения ровно из design.md: `audio` агента важнее
        локального whisper, потому что не требует лишнего шага транскрипции.
        """
        self._whisper_endpoint = whisper_endpoint or ""
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

    # --- запись ----------------------------------------------------------

    def _on_clicked(self) -> None:
        if self._recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _start_recording(self) -> None:
        if self._backend is None:
            return
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
        self._upload_thread = _UploadWorker(self._whisper_endpoint, path, mime_type, self._uploader, self)
        self._upload_thread.done.connect(self._on_upload_done)
        self._upload_thread.failed.connect(self.failed.emit)
        self._upload_thread.start()

    def _on_upload_done(self, text: str) -> None:
        if text:
            self.transcribed_text.emit(text)


__all__ = ["VoiceButton", "RecordBackend", "Uploader", "build_default_backend", "default_uploader"]

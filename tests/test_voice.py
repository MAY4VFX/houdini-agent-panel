"""`ui/voice.py`: the whisper upload contract (auth header, proxy/CA
respect, and a clear error instead of a silent empty result) plus the
`VoiceButton` wiring that carries the API key from `configure()` down to
the request and a failure back up to something the artist can see.

The documented service (project's `whisper` skill / the owner's own
`whi.ai-vfx.com`) accepts `X-API-Key` or `Authorization: Bearer`; this
panel uses `X-API-Key` exclusively — see `default_uploader`'s own
docstring for why. It answers a bad key with `HTTP 401
{"error":"Invalid API key"}`, and a local, unauthenticated whisper must
keep working with nothing configured — both are exercised below.
"""

from __future__ import annotations

import json
import plistlib
import types
import urllib.error
from pathlib import Path

import pytest

from houdini_agent_panel.ui import voice


# --- default_uploader: pure function, no Qt -------------------------------


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._payload


class _FakeDirector:
    """Stands in for `network._opener_director()`'s return value —
    same shape `test_token_check.py::_FakeOpener` uses."""

    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.requests: list = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


@pytest.fixture
def audio_path(tmp_path) -> Path:
    path = tmp_path / "clip.wav"
    path.write_bytes(b"RIFF....WAVEfmt ")
    return path


def _headers(request) -> dict:
    return {k.lower(): v for k, v in request.header_items()}


def test_sends_the_api_key_as_x_api_key_header(audio_path):
    director = _FakeDirector(_FakeResponse(json.dumps({"text": "hello"}).encode()))

    text = voice.default_uploader(
        "https://whi.example.com/v1/audio/transcriptions",
        audio_path,
        "audio/wav",
        "secret-key",
        opener=director,
    )

    assert text == "hello"
    assert _headers(director.requests[0])["x-api-key"] == "secret-key"


def test_no_key_means_no_auth_header_at_all(audio_path):
    """A local, unauthenticated whisper must keep working unchanged."""
    director = _FakeDirector(_FakeResponse(json.dumps({"text": "hi"}).encode()))

    voice.default_uploader(
        "http://127.0.0.1:9000", audio_path, "audio/wav", "", opener=director
    )

    assert "x-api-key" not in _headers(director.requests[0])


def test_no_key_argument_at_all_still_works(audio_path):
    """`api_key` defaults to "" — an old-style call with only the first
    three positional arguments must not break."""
    director = _FakeDirector(_FakeResponse(json.dumps({"text": "hi"}).encode()))

    text = voice.default_uploader(
        "http://127.0.0.1:9000", audio_path, "audio/wav", opener=director
    )

    assert text == "hi"
    assert "x-api-key" not in _headers(director.requests[0])


def test_the_multipart_body_declares_response_format_json(audio_path):
    """The service defaults to JSON already, but relying on a third
    party's default is exactly the kind of thing that breaks quietly —
    the field is sent explicitly."""
    director = _FakeDirector(_FakeResponse(json.dumps({"text": "hi"}).encode()))

    voice.default_uploader(
        "http://127.0.0.1:9000", audio_path, "audio/wav", "", opener=director
    )

    body = director.requests[0].data
    assert b'name="response_format"' in body
    assert b"\r\n\r\njson\r\n" in body
    # The audio field is still there, untouched by the new form field.
    assert b'name="file"; filename="clip.wav"' in body


def test_goes_through_networks_own_opener_by_default(monkeypatch, audio_path):
    """Not `urllib.request.urlopen` directly — the studio proxy and CA
    bundle `network.configure()` applies must cover this request too,
    exactly as `token_check.verify` already does it."""
    director = _FakeDirector(_FakeResponse(json.dumps({"text": "hi"}).encode()))
    monkeypatch.setattr(voice.network, "_opener_director", lambda: director)

    voice.default_uploader("http://127.0.0.1:9000", audio_path, "audio/wav", "")

    assert len(director.requests) == 1


def test_a_401_raises_a_clear_error_instead_of_an_empty_result(audio_path):
    error = urllib.error.HTTPError(
        "https://whi.example.com/v1/audio/transcriptions",
        401,
        "Unauthorized",
        {},
        None,
    )
    director = _FakeDirector(error)

    with pytest.raises(RuntimeError, match="401"):
        voice.default_uploader(
            "https://whi.example.com/v1/audio/transcriptions",
            audio_path,
            "audio/wav",
            "wrong-key",
            opener=director,
        )


def test_a_non_401_http_error_still_propagates(audio_path):
    """Only 401 gets the friendlier message — everything else (a 500, a
    502 from a proxy) surfaces as itself rather than being disguised."""
    error = urllib.error.HTTPError(
        "https://whi.example.com/v1/audio/transcriptions", 500, "Internal Server Error", {}, None
    )
    director = _FakeDirector(error)

    with pytest.raises(urllib.error.HTTPError):
        voice.default_uploader(
            "https://whi.example.com/v1/audio/transcriptions",
            audio_path,
            "audio/wav",
            "",
            opener=director,
        )


def test_a_404_raises_a_clear_error_naming_the_endpoint(audio_path):
    """The classic misconfiguration this exists for: a base address with
    no path, before `_normalize_whisper_endpoint` covered it."""
    error = urllib.error.HTTPError(
        "https://whi.example.com/wrong-path", 404, "Not Found", {}, None
    )
    director = _FakeDirector(error)

    with pytest.raises(RuntimeError, match="404"):
        voice.default_uploader(
            "https://whi.example.com/wrong-path",
            audio_path,
            "audio/wav",
            "",
            opener=director,
        )


# --- _normalize_whisper_endpoint --------------------------------------------


def test_bare_host_gets_the_transcription_path_appended():
    assert (
        voice._normalize_whisper_endpoint("https://whi.ai-vfx.com")
        == "https://whi.ai-vfx.com/v1/audio/transcriptions"
    )


def test_bare_host_with_trailing_slash_also_gets_the_path_appended():
    assert (
        voice._normalize_whisper_endpoint("https://whi.ai-vfx.com/")
        == "https://whi.ai-vfx.com/v1/audio/transcriptions"
    )


def test_an_endpoint_that_already_names_a_path_is_left_untouched():
    assert (
        voice._normalize_whisper_endpoint("http://127.0.0.1:9000/custom/route")
        == "http://127.0.0.1:9000/custom/route"
    )


def test_blank_endpoint_stays_blank():
    """`configure()` runs every endpoint through this unconditionally — a
    blank endpoint (no whisper configured at all) must not turn into a
    bogus URL, since `VoiceButton` also uses blank-vs-not to decide `_mode`."""
    assert voice._normalize_whisper_endpoint("") == ""


# --- _looks_like_real_audio / _empty_recording_message ---------------------


def test_a_16_byte_header_only_file_does_not_look_like_real_audio(tmp_path):
    path = tmp_path / "empty.wav"
    path.write_bytes(b"RIFF....WAVEfmt ")  # the exact bytes the real bug produced
    assert voice._looks_like_real_audio(path) is False


def test_a_missing_file_does_not_look_like_real_audio(tmp_path):
    assert voice._looks_like_real_audio(tmp_path / "missing.wav") is False


def test_a_file_past_the_wav_header_looks_like_real_audio(tmp_path):
    path = tmp_path / "real.wav"
    path.write_bytes(b"RIFF" + b"\x00" * 40 + b"some audio bytes")
    assert voice._looks_like_real_audio(path) is True


def test_empty_recording_message_on_macos_names_the_likely_cause():
    """The artist would not guess this on their own — Houdini's own app
    bundle doesn't declare microphone use, so macOS may never even list
    it under Privacy & Security for the artist to grant."""
    assert "System Settings" in voice._empty_recording_message(platform="darwin")


def test_empty_recording_message_elsewhere_is_the_generic_one():
    message = voice._empty_recording_message(platform="linux")
    assert message == voice._EMPTY_RECORDING_GENERIC
    assert "System Settings" not in message


# --- VoiceButton: the key travels from configure() to the uploader call ---


class _FakeBackend:
    """A synchronous stand-in for the real, asynchronous Qt backends —
    `stop()` calls `on_finished` immediately, and the file it writes is
    past the 44-byte WAV header so it reads as real audio. (16 bytes,
    header-only, is exactly the shape of the real bug — see
    `_EmptyBackend`-flavoured tests below for that case specifically.)
    """

    def start(self, destination: Path) -> None:
        destination.write_bytes(b"RIFF" + b"\x00" * 40 + b"fake audio payload")

    def stop(self, on_finished) -> None:
        on_finished("")


def _button(qapp, uploader) -> voice.VoiceButton:
    button = voice.VoiceButton(
        backend_factory=lambda: (_FakeBackend(), ""),
        uploader=uploader,
    )
    button.configure(
        supports_audio=False,
        whisper_endpoint="http://127.0.0.1:9000",
        whisper_api_key="the-key",
    )
    assert button.isVisible()
    return button


def _wait_until(app, condition, *, timeout_ms: int = 5000) -> None:
    from PySide6 import QtTest

    elapsed = 0
    step = 20
    while not condition() and elapsed < timeout_ms:
        app.processEvents()
        QtTest.QTest.qWait(step)
        elapsed += step
    assert condition(), "condition did not become true in time"
    # `done`/`failed` (what `condition` usually watches) and the worker
    # thread's own `finished` are separate queued cross-thread signals —
    # the first becoming true is no guarantee the second has been
    # delivered yet. Drain once more so a caller that immediately tears
    # the widget down right after this call (as every test here does)
    # doesn't destroy the still-un-discarded `_UploadWorker` out from
    # under `worker.py`'s own `_live` bookkeeping.
    app.processEvents()
    QtTest.QTest.qWait(step)


def test_configure_carries_the_api_key_to_the_uploader(qapp):
    seen: list[tuple] = []

    def fake_uploader(endpoint, audio_path, mime_type, api_key=""):
        seen.append((endpoint, api_key))
        return "transcribed"

    button = _button(qapp, fake_uploader)
    transcribed: list[str] = []
    button.transcribed_text.connect(transcribed.append)

    button._start_recording()
    button._stop_recording()

    _wait_until(qapp, lambda: transcribed)

    # The bare host `_button()` configures with has no path — `configure()`
    # normalizes it to the real transcription endpoint before it ever
    # reaches the uploader (see the `_normalize_whisper_endpoint` tests).
    assert seen == [("http://127.0.0.1:9000/v1/audio/transcriptions", "the-key")]
    assert transcribed == ["transcribed"]
    button.shutdown()


def test_configure_default_key_is_empty_string_not_none(qapp):
    """`whisper_api_key` is optional on `configure()` — an omitted key
    must reach the uploader as `""`, never `None`, so a plain uploader
    that only checks truthiness behaves the same as it always did."""
    seen: list = []

    def fake_uploader(endpoint, audio_path, mime_type, api_key=""):
        seen.append(api_key)
        return "x"

    button = voice.VoiceButton(
        backend_factory=lambda: (_FakeBackend(), ""), uploader=fake_uploader
    )
    button.configure(supports_audio=False, whisper_endpoint="http://127.0.0.1:9000")

    button._start_recording()
    button._stop_recording()
    _wait_until(qapp, lambda: seen)

    assert seen == [""]
    button.shutdown()


def test_a_rejected_key_shows_up_on_the_tooltip_not_silence(qapp):
    """`failed`'s docstring rules out a modal — the tooltip is the
    existing, non-modal surface this button already uses for "no backend
    here" (`unavailable_reason`)."""

    def rejecting_uploader(endpoint, audio_path, mime_type, api_key=""):
        raise RuntimeError("Whisper rejected the API key (401) — check Settings → Voice.")

    button = _button(qapp, rejecting_uploader)
    failures: list[str] = []
    button.failed.connect(failures.append)

    button._start_recording()
    button._stop_recording()

    _wait_until(qapp, lambda: failures)

    assert "401" in failures[0]
    assert "401" in button.toolTip()
    button.shutdown()


def test_a_successful_recording_clears_a_stale_error_tooltip(qapp):
    calls = {"n": 0}

    def flaky_uploader(endpoint, audio_path, mime_type, api_key=""):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("Whisper rejected the API key (401) — check Settings → Voice.")
        return "ok"

    button = _button(qapp, flaky_uploader)
    failures: list[str] = []
    transcribed: list[str] = []
    button.failed.connect(failures.append)
    button.transcribed_text.connect(transcribed.append)

    button._start_recording()
    button._stop_recording()
    _wait_until(qapp, lambda: failures)
    assert "401" in button.toolTip()

    button._start_recording()
    assert button.toolTip() == voice._DEFAULT_TOOLTIP
    button._stop_recording()
    _wait_until(qapp, lambda: transcribed)

    button.shutdown()


# --- VoiceButton: waiting for the backend to actually finish stopping ------
#
# `QMediaRecorder.stop()`/`QAudioRecorder.stop()` are asynchronous — reading
# the file the instant `stop()` returns is the real bug (docs the owner
# measured: 25 files, all exactly the 16-byte RIFF/WAVE preamble, no audio).
# `_DeferredBackend` below stands in for that asynchrony: `stop()` doesn't
# resolve until the test explicitly calls `finish_stop()`.


class _DeferredBackend:
    def __init__(self, wav_bytes: bytes = b"RIFF" + b"\x00" * 40 + b"real audio data") -> None:
        self._wav_bytes = wav_bytes
        self._destination: Path | None = None
        self._on_finished = None
        self.stop_called = False

    def start(self, destination: Path) -> None:
        self._destination = destination
        # Deliberately does NOT write anything yet — nothing valid exists
        # until `finish_stop()` "closes" the container, same as the real
        # backends between `record()` and the state actually reaching
        # `StoppedState`.

    def stop(self, on_finished) -> None:
        self.stop_called = True
        self._on_finished = on_finished

    def finish_stop(self, error: str = "") -> None:
        assert self._destination is not None
        self._destination.write_bytes(self._wav_bytes)
        callback, self._on_finished = self._on_finished, None
        callback(error)


def test_does_not_read_the_file_until_the_backend_reports_it_finished(qapp):
    backend = _DeferredBackend()
    seen: list[bytes] = []

    def fake_uploader(endpoint, audio_path, mime_type, api_key=""):
        seen.append(audio_path.read_bytes())
        return "transcribed"

    button = voice.VoiceButton(backend_factory=lambda: (backend, ""), uploader=fake_uploader)
    button.configure(supports_audio=False, whisper_endpoint="http://127.0.0.1:9000")
    transcribed: list[str] = []
    button.transcribed_text.connect(transcribed.append)

    button._start_recording()
    button._stop_recording()

    # stop() was called, but the backend hasn't said it actually finished —
    # nothing must have been read or uploaded yet.
    qapp.processEvents()
    assert backend.stop_called
    assert seen == []
    assert not transcribed

    backend.finish_stop()
    _wait_until(qapp, lambda: transcribed)

    assert transcribed == ["transcribed"]
    button.shutdown()


def test_a_backend_that_never_finishes_times_out_with_a_clear_error(qapp, monkeypatch):
    monkeypatch.setattr(voice, "_STOP_TIMEOUT_MS", 50)
    backend = _DeferredBackend()  # finish_stop() deliberately never called

    button = voice.VoiceButton(
        backend_factory=lambda: (backend, ""), uploader=lambda *a, **k: "unreachable"
    )
    button.configure(supports_audio=False, whisper_endpoint="http://127.0.0.1:9000")
    failures: list[str] = []
    button.failed.connect(failures.append)

    button._start_recording()
    button._stop_recording()

    _wait_until(qapp, lambda: failures)

    assert "time" in failures[0].lower()
    assert failures[0] in button.toolTip()
    button.shutdown()


class _EmptyBackend:
    """Backend that reports success but hands back the real bug's exact
    16-byte, header-only file."""

    def start(self, destination: Path) -> None:
        destination.write_bytes(b"RIFF....WAVEfmt ")

    def stop(self, on_finished) -> None:
        on_finished("")


class _NoFileBackend:
    """Backend that reports success but never wrote anything at all —
    the other half of the real bug (`Errno 2: No such file or directory`)."""

    def start(self, destination: Path) -> None:
        pass

    def stop(self, on_finished) -> None:
        on_finished("")


def test_a_16_byte_recording_is_not_uploaded_and_gives_a_clear_error(qapp):
    seen: list[Path] = []

    def fake_uploader(endpoint, audio_path, mime_type, api_key=""):
        seen.append(audio_path)
        return "unreachable"

    button = voice.VoiceButton(
        backend_factory=lambda: (_EmptyBackend(), ""), uploader=fake_uploader
    )
    button.configure(supports_audio=False, whisper_endpoint="http://127.0.0.1:9000")
    failures: list[str] = []
    button.failed.connect(failures.append)

    button._start_recording()
    button._stop_recording()

    _wait_until(qapp, lambda: failures)

    assert seen == []  # the uploader was never even called
    assert failures[0] == voice._empty_recording_message()
    button.shutdown()


def test_a_missing_output_file_is_treated_as_an_empty_recording(qapp):
    button = voice.VoiceButton(
        backend_factory=lambda: (_NoFileBackend(), ""), uploader=lambda *a, **k: "unreachable"
    )
    button.configure(supports_audio=False, whisper_endpoint="http://127.0.0.1:9000")
    failures: list[str] = []
    button.failed.connect(failures.append)

    button._start_recording()
    button._stop_recording()

    _wait_until(qapp, lambda: failures)

    assert failures[0] == voice._empty_recording_message()
    button.shutdown()


def test_an_empty_recording_is_also_rejected_in_native_audio_mode(qapp):
    """The empty-file guard isn't whisper-specific — an empty attachment
    handed straight to an agent with the `audio` capability is exactly as
    useless as an empty whisper upload."""
    recorded: list[dict] = []
    failures: list[str] = []

    button = voice.VoiceButton(
        backend_factory=lambda: (_EmptyBackend(), ""), uploader=lambda *a, **k: "unreachable"
    )
    button.configure(supports_audio=True, whisper_endpoint="")
    button.recorded_audio.connect(recorded.append)
    button.failed.connect(failures.append)

    button._start_recording()
    button._stop_recording()

    _wait_until(qapp, lambda: failures)

    assert recorded == []
    button.shutdown()


def test_a_backend_reported_error_surfaces_as_itself_not_the_empty_message(qapp):
    """When the backend's own error signal fired (a real `errorOccurred`/
    `error` from Qt), that message is more specific than the generic empty-
    recording guess and must win."""

    class _FailingBackend:
        def start(self, destination: Path) -> None:
            destination.write_bytes(b"RIFF....WAVEfmt ")

        def stop(self, on_finished) -> None:
            on_finished("Recording failed: ResourceError")

    button = voice.VoiceButton(
        backend_factory=lambda: (_FailingBackend(), ""), uploader=lambda *a, **k: "unreachable"
    )
    button.configure(supports_audio=False, whisper_endpoint="http://127.0.0.1:9000")
    failures: list[str] = []
    button.failed.connect(failures.append)

    button._start_recording()
    button._stop_recording()

    _wait_until(qapp, lambda: failures)

    assert failures[0] == "Recording failed: ResourceError"
    button.shutdown()


def test_the_recording_size_reaches_the_log(qapp, tmp_path, caplog):
    """The one line that would have named this bug in seconds.

    Every failed recording produced 16 bytes — a RIFF/WAVE preamble and no
    samples — and the panel's own log said nothing at all, so the size had
    to be read off the owner's machine by hand. §25-§28 record the same
    blind spot costing three wrong releases on the OAuth token.
    """
    import logging

    from houdini_agent_panel.ui import voice as voice_mod

    caplog.set_level(logging.INFO, logger="houdini_agent_panel.ui.voice")

    button = voice_mod.VoiceButton(backend_factory=lambda: (_FakeBackend(), ""))
    broken = tmp_path / "hap-voice-broken.wav"
    broken.write_bytes(b"RIFF....WAVEfmt ")  # the measured 16 bytes, exactly

    button._handle_stopped(broken, "")

    messages = [r.getMessage() for r in caplog.records]
    assert any("16 bytes" in m for m in messages), messages


def test_a_missing_recording_is_logged_rather_than_silently_dropped(qapp, tmp_path, caplog):
    import logging

    from houdini_agent_panel.ui import voice as voice_mod

    caplog.set_level(logging.INFO, logger="houdini_agent_panel.ui.voice")

    button = voice_mod.VoiceButton(backend_factory=lambda: (_FakeBackend(), ""))
    button._handle_stopped(tmp_path / "never-created.wav", "")

    messages = [r.getMessage() for r in caplog.records]
    assert any("recording finished" in m for m in messages), messages


def test_neither_the_api_key_nor_the_transcript_reaches_the_log(qapp, caplog, monkeypatch):
    """Two different reasons, both absolute. The key is a credential; the
    transcript is the artist speaking in their own room, and only its
    length is ever diagnostic."""
    import logging

    from houdini_agent_panel.ui import voice as voice_mod

    caplog.set_level(logging.INFO, logger="houdini_agent_panel.ui.voice")

    secret = "NOT-A-REAL-KEY-0123456789abcdef"
    spoken = "delete the pyro solver and start again"

    button = voice_mod.VoiceButton(
        backend_factory=lambda: (_FakeBackend(), ""), uploader=lambda *a, **k: ""
    )
    button.configure(
        supports_audio=False,
        whisper_endpoint="https://whisper.example/v1/audio/transcriptions",
        whisper_api_key=secret,
    )
    # A stub instead of the real `_UploadWorker`: this test is about what
    # reaches the log, and starting a genuine QThread here leaves Qt tearing
    # down a live C++ object at interpreter exit.
    class _NoThread:
        def __init__(self, *args, **kwargs) -> None:
            self.done = _Sig()
            self.failed = _Sig()

        def start(self) -> None:
            pass

    class _Sig:
        def connect(self, *_args, **_kwargs) -> None:
            pass

    monkeypatch.setattr(voice_mod, "_UploadWorker", _NoThread)
    button._start_upload(Path("/nonexistent/hap-voice.wav"))
    button._on_upload_done(spoken)

    messages = [r.getMessage() for r in caplog.records]
    blob = "\n".join(messages)
    assert secret not in blob, "the API key reached the log"
    assert spoken not in blob, "the artist's own words reached the log"
    assert any(str(len(spoken)) in m for m in messages), "the length is the diagnostic part"
    assert any("whisper.example" in m for m in messages), "the endpoint is not a secret"


# --- microphone availability: bundle Info.plist / TCC permission gating ----
#
# The owner's own measurement: none of Houdini's ten installed macOS
# bundles (20.5/21.0/22.0, every edition) declare
# `NSMicrophoneUsageDescription`, so Qt6 refuses to even ask for microphone
# access and a "successful" recording is silently empty (the 16-byte bug
# the rest of this file is built around). `recording_available()`/
# `build_default_backend()` check for that ahead of time so the mic button
# and Settings → Voice section are never offered for something guaranteed
# to fail — see `ui/voice.py`'s module docstring.


def _fake_bundle(tmp_path: Path, *, declares_microphone: bool) -> Path:
    """A minimal `.app/Contents/{Info.plist,MacOS/houdini}` tree standing
    in for one of Houdini's real bundles. Returns the fake executable path
    `_main_bundle_info_plist_path`/`_bundle_declares_microphone_usage` take
    as their `executable=` override — no hardcoded Houdini path, same as
    production."""
    bundle = tmp_path / "Houdini FX 20.5.test.app"
    macos = bundle / "Contents" / "MacOS"
    macos.mkdir(parents=True)
    info: dict = {"CFBundleName": "Houdini FX"}
    if declares_microphone:
        info["NSMicrophoneUsageDescription"] = "Houdini uses the microphone for voice input."
    with (bundle / "Contents" / "Info.plist").open("wb") as fh:
        plistlib.dump(info, fh)
    executable = macos / "houdini"
    executable.write_bytes(b"")
    return executable


def test_bundle_info_plist_path_found_by_walking_up_from_the_executable(tmp_path):
    executable = _fake_bundle(tmp_path, declares_microphone=False)
    plist_path = voice._main_bundle_info_plist_path(executable=str(executable))
    assert plist_path == executable.parent.parent / "Info.plist"


def test_bundle_info_plist_path_is_none_outside_any_app_bundle(tmp_path):
    executable = tmp_path / "bin" / "python3"
    executable.parent.mkdir()
    executable.write_bytes(b"")
    assert voice._main_bundle_info_plist_path(executable=str(executable)) is None


def test_bundle_declares_microphone_usage_true_when_key_present(tmp_path):
    executable = _fake_bundle(tmp_path, declares_microphone=True)
    assert voice._bundle_declares_microphone_usage(executable=str(executable)) is True


def test_bundle_declares_microphone_usage_false_when_key_absent(tmp_path):
    """The measured, real shape of every one of Houdini's ten installed
    bundles (20.5/21.0/22.0, every edition): no
    `NSMicrophoneUsageDescription` at all."""
    executable = _fake_bundle(tmp_path, declares_microphone=False)
    assert voice._bundle_declares_microphone_usage(executable=str(executable)) is False


def test_bundle_declares_microphone_usage_false_with_no_bundle_at_all(tmp_path):
    executable = tmp_path / "bin" / "python3"
    executable.parent.mkdir()
    executable.write_bytes(b"")
    assert voice._bundle_declares_microphone_usage(executable=str(executable)) is False


class _FakeApp:
    """Stands in for the real, process-wide `QApplication` singleton so a
    test controls `checkPermission`'s answer directly instead of depending
    on whatever TCC state the machine running pytest happens to be in."""

    def __init__(self, status) -> None:
        self.status = status
        self.calls = 0

    def checkPermission(self, permission) -> object:  # noqa: N802 - Qt's own name
        self.calls += 1
        return self.status


def test_qt6_permission_denied_blocks_with_the_denied_message(qapp):
    app = _FakeApp(voice.QtCore.Qt.PermissionStatus.Denied)
    reason = voice._qt6_permission_block_reason(platform="darwin", app=app)
    assert reason == voice._MIC_BLOCKED_DENIED


def test_qt6_permission_undetermined_without_entitlement_blocks(qapp):
    """The special case this whole feature exists for: Qt has never been
    asked, and never can be — the bundle's own Info.plist has no
    `NSMicrophoneUsageDescription` key for it to ask with."""
    app = _FakeApp(voice.QtCore.Qt.PermissionStatus.Undetermined)
    reason = voice._qt6_permission_block_reason(
        platform="darwin", app=app, bundle_declares_microphone=False
    )
    assert reason == voice._MIC_BLOCKED_NO_ENTITLEMENT


def test_qt6_permission_undetermined_with_entitlement_is_available(qapp):
    app = _FakeApp(voice.QtCore.Qt.PermissionStatus.Undetermined)
    reason = voice._qt6_permission_block_reason(
        platform="darwin", app=app, bundle_declares_microphone=True
    )
    assert reason == ""


def test_qt6_permission_granted_is_available(qapp):
    app = _FakeApp(voice.QtCore.Qt.PermissionStatus.Granted)
    reason = voice._qt6_permission_block_reason(platform="darwin", app=app)
    assert reason == ""


def test_qt6_permission_check_is_skipped_off_macos(qapp):
    """TCC (and this whole permission dance) doesn't exist off macOS — even
    a `Denied` app must not block, and the check must not even run."""
    app = _FakeApp(voice.QtCore.Qt.PermissionStatus.Denied)
    reason = voice._qt6_permission_block_reason(platform="linux", app=app)
    assert reason == ""
    assert app.calls == 0


def test_qt6_permission_check_reports_nothing_without_the_api(qapp, monkeypatch):
    """PySide2/Qt5 (Houdini 20.5) has no `QMicrophonePermission` at all —
    the one case where recording is treated as available regardless of
    platform, and any real failure surfaces later through the existing
    empty-recording path (`_empty_recording_message`)."""
    monkeypatch.delattr(voice.QtCore, "QMicrophonePermission", raising=False)
    app = _FakeApp(voice.QtCore.Qt.PermissionStatus.Denied)
    reason = voice._qt6_permission_block_reason(platform="darwin", app=app)
    assert reason == ""
    assert app.calls == 0


def test_blocked_reason_when_qtmultimedia_is_unavailable():
    assert voice._blocked_reason(None) == "QtMultimedia is unavailable in this environment"


def test_blocked_reason_qt6_defers_to_the_permission_check(monkeypatch):
    monkeypatch.setattr(voice, "_qt6_permission_block_reason", lambda: "permission blocked")
    fake_qtmultimedia = types.SimpleNamespace(QMediaCaptureSession=object(), QAudioInput=object())
    assert voice._blocked_reason(fake_qtmultimedia) == "permission blocked"


def test_blocked_reason_qt5_has_no_permission_api_to_check(monkeypatch):
    """Qt5 (PySide2, Houdini 20.5) offers no permission API — the
    permission check must not even run for it."""

    def _fail():
        raise AssertionError("the Qt6 permission check ran for a Qt5-shaped QtMultimedia")

    monkeypatch.setattr(voice, "_qt6_permission_block_reason", _fail)
    fake_qtmultimedia = types.SimpleNamespace(QAudioRecorder=object())
    assert voice._blocked_reason(fake_qtmultimedia) == ""


def test_blocked_reason_when_qtmultimedia_offers_neither_api():
    fake_qtmultimedia = types.SimpleNamespace()
    assert (
        voice._blocked_reason(fake_qtmultimedia) == "QtMultimedia offers no recording API we know"
    )


def test_recording_available_true_when_nothing_blocks(monkeypatch):
    monkeypatch.setattr(voice, "_blocked_reason", lambda qtm: "")
    assert voice.recording_available() == (True, "")


def test_recording_available_false_with_the_reason(monkeypatch):
    monkeypatch.setattr(voice, "_blocked_reason", lambda qtm: "no entitlement")
    assert voice.recording_available() == (False, "no entitlement")


def test_build_default_backend_is_none_when_blocked_reason_says_so(monkeypatch):
    """`build_default_backend` never even tries to construct a
    `_Qt6RecordBackend` once `_blocked_reason` has already said no —
    nothing here builds a real `QMediaRecorder` that would only fail
    silently the moment recording actually started."""
    monkeypatch.setattr(voice, "_blocked_reason", lambda qtm: "blocked: no entitlement")
    backend, reason = voice.build_default_backend()
    assert backend is None
    assert reason == "blocked: no entitlement"


def test_build_default_backend_logs_the_blocked_reason(monkeypatch, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="houdini_agent_panel.ui.voice")
    monkeypatch.setattr(voice, "_blocked_reason", lambda qtm: "blocked: no entitlement")

    voice.build_default_backend()

    messages = [r.getMessage() for r in caplog.records]
    assert any("blocked: no entitlement" in m for m in messages), messages


def test_voice_button_hides_when_recording_is_blocked(qapp, monkeypatch):
    """End to end through the button's own default `backend_factory`
    (`build_default_backend`) — not a stubbed-out fake this time — so this
    proves the wiring, not just the pure function above it."""
    monkeypatch.setattr(voice, "_blocked_reason", lambda qtm: "no entitlement")
    button = voice.VoiceButton(uploader=lambda *a, **k: "unreachable")
    failures: list[str] = []
    button.failed.connect(failures.append)

    button.configure(supports_audio=True, whisper_endpoint="")

    assert button.isVisible() is False
    assert failures == ["no entitlement"]
    button.shutdown()

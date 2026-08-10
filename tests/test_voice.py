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
    # Voice input is unconditionally hidden right now (see
    # `voice._VOICE_INPUT_AVAILABLE`) — the button stays invisible even
    # though its backend was wired up successfully. `is_available()`
    # reflects the backend, not the separate (and currently always-off)
    # visibility decision, so it's the right sanity check here.
    assert button.is_available()
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


# --- voice input is off everywhere, unconditionally --------------------
#
# The owner overruled the previous (platform-conditional) version of this
# fix: macOS is the only platform anyone actually measured (TCC/
# `NSMicrophoneUsageDescription`, see `ui/voice.py`'s module docstring),
# and showing voice input on Linux/Windows on the unverified ASSUMPTION
# that it'd work there was exactly the guess-instead-of-measurement this
# project already paid for once on the OAuth token capture. So
# `recording_available()` is now pinned to a single, unconditional flag
# (`_VOICE_INPUT_AVAILABLE`) with no platform branching at all — these
# tests check that pin, not any platform behaviour.


def test_recording_available_is_unconditionally_false():
    """No `sys.platform` branch, no backend probe — `recording_available()`
    just reports the single kill switch."""
    available, reason = voice.recording_available()
    assert available is False
    assert reason  # a real, non-empty explanation — not a bare `False`


def test_recording_available_reason_matches_the_disabled_reason_constant():
    """The reason is exactly `_VOICE_INPUT_DISABLED_REASON` — the one
    string an artist might see, in the Settings caption that replaces the
    Voice section (`test_ui_settings.py` checks it lands there)."""
    _, reason = voice.recording_available()
    assert reason == voice._VOICE_INPUT_DISABLED_REASON


def test_flipping_the_flag_makes_recording_available_true(monkeypatch):
    """Proves the flag is really the only thing gating this — flip it and
    `recording_available()` follows, with no reason string attached."""
    monkeypatch.setattr(voice, "_VOICE_INPUT_AVAILABLE", True)
    assert voice.recording_available() == (True, "")


def test_voice_button_never_shows_even_with_a_working_backend_and_full_capabilities(qapp):
    """The button stays hidden regardless of the agent's capabilities or
    whether a real recording backend is available — `recording_available()`
    overrides everything else, unconditionally."""
    button = voice.VoiceButton(
        backend_factory=lambda: (_FakeBackend(), ""), uploader=lambda *a, **k: "unreachable"
    )
    failures: list[str] = []
    button.failed.connect(failures.append)

    button.configure(supports_audio=True, whisper_endpoint="http://127.0.0.1:9000")

    assert button.isVisible() is False
    assert failures == [voice._VOICE_INPUT_DISABLED_REASON]
    button.shutdown()


def test_voice_button_backend_still_gets_wired_up_while_hidden(qapp):
    """The recording/upload machinery this file's other tests exercise
    (`_start_recording`/`_stop_recording`, manually driven) must keep
    working even though the button itself never becomes visible — only the
    final visibility decision is overridden, not backend construction."""
    button = voice.VoiceButton(
        backend_factory=lambda: (_FakeBackend(), ""), uploader=lambda *a, **k: "unreachable"
    )

    button.configure(supports_audio=True, whisper_endpoint="")

    assert button.isVisible() is False
    assert button.is_available() is True  # the backend was still constructed and wired


def test_voice_button_hidden_reason_reaches_the_log(qapp, caplog):
    import logging

    caplog.set_level(logging.INFO, logger="houdini_agent_panel.ui.voice")
    button = voice.VoiceButton(
        backend_factory=lambda: (_FakeBackend(), ""), uploader=lambda *a, **k: "unreachable"
    )

    button.configure(supports_audio=True, whisper_endpoint="")

    messages = [r.getMessage() for r in caplog.records]
    assert any(voice._VOICE_INPUT_DISABLED_REASON in m for m in messages), messages

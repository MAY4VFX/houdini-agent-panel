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


# --- VoiceButton: the key travels from configure() to the uploader call ---


class _FakeBackend:
    def start(self, destination: Path) -> None:
        destination.write_bytes(b"RIFF....WAVEfmt ")

    def stop(self) -> None:
        pass


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

    assert seen == [("http://127.0.0.1:9000", "the-key")]
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

from __future__ import annotations

import json

from houdini_agent_panel import telemetry
from houdini_agent_panel.settings import Settings

ENV = telemetry.TELEMETRY_URL_ENV


# --- is_enabled: both gates must be open ------------------------------------


def test_is_enabled_requires_both_toggle_and_endpoint(monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    assert telemetry.is_enabled(Settings(telemetry=True)) is False

    monkeypatch.setenv(ENV, "https://telemetry.example.com/e")
    assert telemetry.is_enabled(Settings(telemetry=False)) is False
    assert telemetry.is_enabled(Settings(telemetry=True)) is True


# --- build_payload: basic contents --------------------------------------


def test_build_payload_has_event_and_os(monkeypatch):
    monkeypatch.setattr(telemetry, "_os_name", lambda: "macOS-14.0-arm64")
    monkeypatch.setattr(telemetry, "_panel_version", lambda: "0.1.0")
    monkeypatch.setattr(telemetry, "_fx_version", lambda: "")
    monkeypatch.setattr(telemetry, "_houdini_version", lambda: "unknown")

    payload = telemetry.build_payload(Settings(), event="panel_opened")

    assert payload["event"] == "panel_opened"
    assert payload["os"] == "macOS-14.0-arm64"
    assert payload["panel_version"] == "0.1.0"
    # unavailable versions are simply absent, not "" or null
    assert "fx_version" not in payload
    assert "houdini_version" not in payload


def test_build_payload_includes_allowed_extra_only(monkeypatch):
    monkeypatch.setattr(telemetry, "_os_name", lambda: "macOS-14.0-arm64")
    monkeypatch.setattr(telemetry, "_panel_version", lambda: "0.1.0")
    monkeypatch.setattr(telemetry, "_fx_version", lambda: "2.10.0")
    monkeypatch.setattr(telemetry, "_houdini_version", lambda: "20.5.584")

    payload = telemetry.build_payload(
        Settings(),
        event="agent_crashed",
        exception_type="ConnectionError",
        agent_version="1.2.3",
        unexpected_field="should be dropped",
    )

    assert payload["exception_type"] == "ConnectionError"
    assert payload["agent_version"] == "1.2.3"
    assert payload["fx_version"] == "2.10.0"
    assert payload["houdini_version"] == "20.5.584"
    assert "unexpected_field" not in payload


# --- sentinel: NEVER a forbidden key or a hint of a path -------------


def test_build_payload_sentinel_never_leaks_forbidden_data(monkeypatch):
    """Checks the actual promise from docs/privacy.md, not how it's worded.

    Runs build_payload against several inputs, among them attempts to smuggle
    a scene path, a prompt's text, and an agent session id through **extra —
    and fails on any of them if the resulting payload ends up with a
    forbidden key or a substring that looks like a Houdini path/variable.
    """
    monkeypatch.setattr(telemetry, "_os_name", lambda: "macOS-14.0-arm64")
    monkeypatch.setattr(telemetry, "_panel_version", lambda: "0.1.0")
    monkeypatch.setattr(telemetry, "_fx_version", lambda: "2.10.0")
    monkeypatch.setattr(telemetry, "_houdini_version", lambda: "20.5.584")

    settings = Settings(telemetry=True)
    known_keys = {"event", "os", "panel_version", "fx_version", "houdini_version"} | set(
        telemetry._ALLOWED_EXTRA_KEYS
    )
    forbidden_markers = ("/", "\\", "$HIP")

    attempts: list[dict] = [
        {"event": "panel_opened"},
        {"event": "agent_connected", "agent_version": "1.4.0"},
        {"event": "agent_crashed", "exception_type": "TimeoutError"},
        # attempts to smuggle in extra data, not just an event name - must be dropped
        {
            "event": "prompt_sent",
            "path": "/Users/artist/shots/010/scene.hip",
            "scene_path": "$HIP/geo/cache.bgeo.sc",
            "prompt": "make me a VDB node from the selection",
            "session_id": "acp-session-9f31",
            "cwd": r"C:\Users\artist\Documents",
        },
    ]

    for kwargs in attempts:
        event = kwargs.pop("event")
        payload = telemetry.build_payload(settings, event=event, **kwargs)
        serialized = json.dumps(payload)

        unknown = set(payload) - known_keys
        assert not unknown, f"payload contains an unknown key: {unknown} ({serialized})"

        for marker in forbidden_markers:
            assert marker not in serialized, f"payload looks like a path ({marker!r}): {serialized}"


# --- send(): network rules -------------------------------------------------


def test_send_noop_when_telemetry_disabled(fetcher, monkeypatch):
    monkeypatch.setenv(ENV, "https://telemetry.example.com/e")
    telemetry.send("panel_opened", settings=Settings(telemetry=False), fetch=fetcher)
    assert fetcher.calls == []


def test_send_noop_when_endpoint_not_set(fetcher, monkeypatch):
    monkeypatch.delenv(ENV, raising=False)
    telemetry.send("panel_opened", settings=Settings(telemetry=True), fetch=fetcher)
    assert fetcher.calls == []


def test_send_makes_one_call_when_enabled(fetcher, monkeypatch):
    monkeypatch.setenv(ENV, "https://telemetry.example.com/e")
    fetcher.add_bytes("https://telemetry.example.com/e?event=panel_opened", b"")
    # No need to build the exact query string by hand - just allow any response.

    class _AnyUrlFetcher:
        def __init__(self):
            self.calls: list[str] = []

        def __call__(self, url: str, *, timeout: float = 30.0) -> bytes:
            self.calls.append(url)
            return b""

    any_fetcher = _AnyUrlFetcher()
    telemetry.send("panel_opened", settings=Settings(telemetry=True), fetch=any_fetcher)

    assert len(any_fetcher.calls) == 1
    assert any_fetcher.calls[0].startswith("https://telemetry.example.com/e?")
    assert "event=panel_opened" in any_fetcher.calls[0]


def test_send_swallows_network_error(monkeypatch):
    monkeypatch.setenv(ENV, "https://telemetry.example.com/e")

    def explode(url: str, *, timeout: float = 30.0) -> bytes:
        from houdini_agent_panel.network import NetworkError

        raise NetworkError("boom")

    # Must not let an exception escape - telemetry is not allowed to break
    # anything about how the panel works.
    telemetry.send("panel_opened", settings=Settings(telemetry=True), fetch=explode)

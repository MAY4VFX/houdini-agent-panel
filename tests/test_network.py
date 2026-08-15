"""The network layer: TLS and call tracking."""

from __future__ import annotations

import ssl

import pytest

from houdini_agent_panel import network


def test_ssl_context_prefers_certifi_bundle(monkeypatch):
    """Inside Houdini there's no system root CA bundle, and HTTPS without an
    explicit bundle fails with CERTIFICATE_VERIFY_FAILED. Check that the
    context is built from certifi, not from the system default."""
    monkeypatch.setattr(network, "_ssl_context", None)
    monkeypatch.delenv(network.CA_BUNDLE_ENV, raising=False)

    import certifi

    seen = {}

    def fake_create(*, cafile=None, **kwargs):
        seen["cafile"] = cafile
        return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    monkeypatch.setattr(ssl, "create_default_context", fake_create)

    network.ssl_context()

    assert seen["cafile"] == certifi.where()


def test_ca_bundle_env_wins(monkeypatch, tmp_path):
    """A studio with an intercepting proxy supplies its own bundle — it must
    win over certifi, otherwise the panel simply can't reach the network there."""
    monkeypatch.setattr(network, "_ssl_context", None)
    bundle = tmp_path / "corporate.pem"
    bundle.write_text("")
    monkeypatch.setenv(network.CA_BUNDLE_ENV, str(bundle))

    seen = {}

    def fake_create(*, cafile=None, **kwargs):
        seen["cafile"] = cafile
        return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    monkeypatch.setattr(ssl, "create_default_context", fake_create)

    network.ssl_context()

    assert seen["cafile"] == str(bundle)


def test_ssl_context_is_cached(monkeypatch):
    monkeypatch.setattr(network, "_ssl_context", None)
    first = network.ssl_context()
    assert network.ssl_context() is first


def _reset_configure_state(monkeypatch) -> None:
    """Record the pristine module state so `monkeypatch` restores it, no
    matter what `configure()` mutates during the test."""
    monkeypatch.setattr(network, "_proxy_url", None)
    monkeypatch.setattr(network, "_ca_bundle_override", None)
    monkeypatch.setattr(network, "_ssl_context", None)
    monkeypatch.setattr(network, "_opener", None)


def test_configure_stores_proxy_and_ca_bundle(monkeypatch):
    _reset_configure_state(monkeypatch)

    network.configure(proxy="http://proxy.studio.local:8080", ca_bundle="/etc/studio-ca.pem")

    assert network._proxy_url == "http://proxy.studio.local:8080"
    assert network._ca_bundle_override == "/etc/studio-ca.pem"


def test_configure_empty_string_means_unset(monkeypatch):
    """An empty setting must mean "don't set the variable", never "set it to
    empty" — the same rule the design doc states for the settings fields."""
    _reset_configure_state(monkeypatch)
    network.configure(proxy="http://proxy.studio.local:8080", ca_bundle="/etc/studio-ca.pem")

    network.configure(proxy="", ca_bundle="")

    assert network._proxy_url is None
    assert network._ca_bundle_override is None


def test_configure_drops_cached_ssl_context(monkeypatch, tmp_path):
    """A changed CA bundle must take effect without a Houdini restart, so
    `configure()` has to invalidate whatever `ssl_context()` already built."""
    _reset_configure_state(monkeypatch)
    monkeypatch.delenv(network.CA_BUNDLE_ENV, raising=False)

    stale = network.ssl_context()

    bundle = tmp_path / "corporate.pem"
    bundle.write_text("")
    network.configure(ca_bundle=str(bundle))

    seen = {}

    def fake_create(*, cafile=None, **kwargs):
        seen["cafile"] = cafile
        return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    monkeypatch.setattr(ssl, "create_default_context", fake_create)

    fresh = network.ssl_context()

    assert fresh is not stale
    assert seen["cafile"] == str(bundle)


def test_configured_ca_bundle_wins_over_env(monkeypatch, tmp_path):
    """`HAP_CA_BUNDLE` is documented as the fallback for when Network
    settings didn't set anything — an explicit setting must win."""
    _reset_configure_state(monkeypatch)

    env_bundle = tmp_path / "env.pem"
    env_bundle.write_text("")
    monkeypatch.setenv(network.CA_BUNDLE_ENV, str(env_bundle))

    settings_bundle = tmp_path / "settings.pem"
    settings_bundle.write_text("")
    network.configure(ca_bundle=str(settings_bundle))

    seen = {}

    def fake_create(*, cafile=None, **kwargs):
        seen["cafile"] = cafile
        return ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)

    monkeypatch.setattr(ssl, "create_default_context", fake_create)

    network.ssl_context()

    assert seen["cafile"] == str(settings_bundle)


def test_configure_drops_cached_opener(monkeypatch):
    _reset_configure_state(monkeypatch)

    stale = network._opener_director()
    network.configure(proxy="http://proxy.studio.local:8080")
    fresh = network._opener_director()

    assert fresh is not stale


def test_opener_uses_configured_proxy_for_https(monkeypatch):
    _reset_configure_state(monkeypatch)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)

    network.configure(proxy="http://proxy.studio.local:8080")
    opener = network._opener_director()

    ProxyHandler = network.urllib.request.ProxyHandler
    proxy_handlers = [h for h in opener.handlers if isinstance(h, ProxyHandler)]
    assert any(h.proxies.get("https") == "http://proxy.studio.local:8080" for h in proxy_handlers)


def test_opener_has_no_explicit_proxy_handler_by_default(monkeypatch):
    """With nothing configured, the panel must not force its own proxy
    choice — it has to leave discovery to `build_opener()`'s own default
    `ProxyHandler`, which is what already lets a machine-wide `HTTPS_PROXY`
    keep working today."""
    _reset_configure_state(monkeypatch)

    opener = network._opener_director()

    # We never add our own ProxyHandler when no proxy is configured; any
    # ProxyHandler present is the one build_opener() supplies by default.
    ours = [
        h
        for h in opener.handlers
        if isinstance(h, network.urllib.request.ProxyHandler) and h.proxies.get("https") is not None
    ]
    assert ours == []


def test_the_panel_actually_applies_its_network_settings(qapp, monkeypatch):
    """`network.configure` is a primitive nothing calls on its own.

    A proxy feature that is fully implemented and never invoked looks
    finished and does nothing — the exact failure mode this project has hit
    more than once. This asserts the panel calls it, at startup and again
    when settings are saved, so that gap cannot reopen quietly.
    """
    from houdini_agent_panel import network, settings as settings_mod
    from houdini_agent_panel.ui import panel as panel_mod

    calls: list[tuple] = []
    monkeypatch.setattr(
        network, "configure", lambda **kw: calls.append((kw.get("proxy"), kw.get("ca_bundle")))
    )

    monkeypatch.setattr(panel_mod.scene, "hip_dir", lambda: "/tmp")
    monkeypatch.setattr(
        panel_mod.scene, "mcp_servers",
        lambda: [{"name": "fxhoudini", "command": "python", "args": [], "env": []}],
    )
    monkeypatch.setattr(panel_mod._RefreshWorker, "start", lambda self: None)

    current = settings_mod.load()
    current.proxy_url = "http://proxy.studio.local:8080"
    current.ca_bundle = "/etc/ssl/studio.pem"
    settings_mod.save(current)

    widget = panel_mod.AgentPanel()
    assert calls, "the panel never applied its network settings at startup"
    assert calls[-1] == ("http://proxy.studio.local:8080", "/etc/ssl/studio.pem")

    calls.clear()
    widget._on_settings_changed()
    assert calls, "changing settings did not reach the network layer"
    widget.shutdown()


class _Clock:
    def __init__(self):
        self.now = 0.0

    def advance(self, seconds):
        self.now += seconds

    def __call__(self):
        return self.now


class _DribblingResponse:
    """A response that keeps arriving, slowly, and never ends.

    The shape measured on a Windows VM behind a filtering link: HTTP 200 at
    once, then a couple of hundred bytes a second, still going two minutes
    later. Every individual read succeeds, so a socket timeout never fires
    and `read()` never returns.
    """

    def __init__(self, clock):
        self._clock = clock

    def read(self, size=-1):
        self._clock.advance(5.0)
        return b"x" * 1024


def test_read_within_gives_up_on_a_response_that_never_ends(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(network.time, "monotonic", clock)

    with pytest.raises(network.NetworkError) as excinfo:
        network._read_within(
            _DribblingResponse(clock), "https://example.invalid/registry.json", deadline=30.0
        )

    message = str(excinfo.value)
    assert "still arriving after 30s" in message
    assert "bytes so far" in message


def test_read_within_returns_a_response_that_ends(monkeypatch):
    clock = _Clock()
    monkeypatch.setattr(network.time, "monotonic", clock)

    class _Finite:
        def __init__(self):
            self._left = [b"{", b"}"]

        def read(self, size=-1):
            return self._left.pop(0) if self._left else b""

    assert network._read_within(_Finite(), "https://example.invalid", deadline=30.0) == b"{}"


def test_read_within_allows_a_slow_but_finite_response(monkeypatch):
    """A deadline that fires on the last chunk of a download that did finish
    would turn a working slow link into a broken one."""
    clock = _Clock()
    monkeypatch.setattr(network.time, "monotonic", clock)

    class _Slow:
        def __init__(self):
            self._left = [b"a", b"b", b""]

        def read(self, size=-1):
            clock.advance(9.0)
            return self._left.pop(0)

    assert network._read_within(_Slow(), "https://example.invalid", deadline=30.0) == b"ab"

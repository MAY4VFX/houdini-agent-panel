"""The network layer: TLS and call tracking."""

from __future__ import annotations

import ssl

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

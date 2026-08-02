"""Сетевой слой: TLS и учёт вызовов."""

from __future__ import annotations

import ssl

from houdini_agent_panel import network


def test_ssl_context_prefers_certifi_bundle(monkeypatch):
    """Внутри Houdini системной связки корневых сертификатов нет, и HTTPS без
    явного bundle падает на CERTIFICATE_VERIFY_FAILED. Проверяем, что контекст
    строится именно из certifi, а не из системного умолчания."""
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
    """Студия с перехватывающим прокси подсовывает свою связку — она должна
    побеждать certifi, иначе панель там просто не выйдет в сеть."""
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

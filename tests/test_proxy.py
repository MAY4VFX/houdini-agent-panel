"""Tests for `proxy.py`: settings -> environment variables, pure and offline."""

from __future__ import annotations

from houdini_agent_panel import proxy
from houdini_agent_panel import settings as settings_module
from houdini_agent_panel.settings import Settings


def test_no_proxy_anywhere_sets_nothing():
    assert proxy.child_env(Settings(), environ={}) == {}


def test_settings_proxy_wins_over_environment():
    settings = Settings(proxy_url="http://studio:8080")
    env = proxy.child_env(settings, environ={"HTTPS_PROXY": "http://stale:3128"})
    assert env["HTTPS_PROXY"] == "http://studio:8080"
    assert env["https_proxy"] == "http://studio:8080"
    assert env["ALL_PROXY"] == "http://studio:8080"


def test_machine_proxy_is_inherited_when_the_field_is_empty():
    # The bug this whole feature starts from: a correctly configured machine
    # must not be un-configured by the panel.
    env = proxy.child_env(Settings(), environ={"HTTPS_PROXY": "http://studio:8080"})
    assert env["HTTPS_PROXY"] == "http://studio:8080"


def test_localhost_always_bypasses_the_proxy():
    # fx MCP and opencode's own TUI server live on localhost; proxying them
    # is a hang, not an error.
    env = proxy.child_env(Settings(proxy_url="http://studio:8080"), environ={})
    parts = env["NO_PROXY"].split(",")
    assert parts[:3] == ["localhost", "127.0.0.1", "::1"]
    assert env["no_proxy"] == env["NO_PROXY"]


def test_no_proxy_merges_machine_and_settings_without_duplicates():
    settings = Settings(proxy_url="http://studio:8080", no_proxy="render.local, localhost")
    env = proxy.child_env(settings, environ={"NO_PROXY": "vault.local"})
    assert env["NO_PROXY"] == "localhost,127.0.0.1,::1,vault.local,render.local"


def test_ca_bundle_goes_out_under_every_name_a_runtime_reads():
    settings = Settings(ca_bundle="/etc/ssl/studio.pem")
    env = proxy.child_env(settings, environ={})
    for name in ("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "HAP_CA_BUNDLE"):
        assert env[name] == "/etc/ssl/studio.pem"


def test_empty_fields_never_produce_empty_variables():
    # An empty NODE_EXTRA_CA_CERTS is worse than an absent one: Node treats it
    # as a path and fails.
    assert proxy.child_env(Settings(), environ={}) == {}


def test_sanitize_strips_the_password():
    assert proxy.sanitize("http://bob:hunter2@studio:8080") == "http://bob:***@studio:8080"
    assert proxy.sanitize("http://studio:8080") == "http://studio:8080"
    assert proxy.sanitize("not a url") == "not a url"


def test_diagnostics_hides_the_proxy_password():
    text = settings_module.diagnostics(Settings(proxy_url="http://bob:hunter2@studio:8080"))
    assert "hunter2" not in text
    assert "proxy: http://bob:***@studio:8080" in text

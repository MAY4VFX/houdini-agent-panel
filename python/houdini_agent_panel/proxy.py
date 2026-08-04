"""Settings to environment variables for a studio proxy.

One module owns the variable names because there are eighteen of them and
six agents that each read a different subset. Everything here is pure: a
`Settings` in, a dict out, so a test can check the whole matrix without a
process, a network, or Houdini.
"""

from __future__ import annotations

import os
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

#: Every spelling an agent might read. Claude Code prefers `https_proxy`
#: over `HTTPS_PROXY`; Codex and Gemini also read `ALL_PROXY`. Setting all
#: six is cheaper than tracking who reads which.
PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")

#: `NODE_EXTRA_CA_CERTS` for the npx agents, `SSL_CERT_FILE` for the Rust and
#: Go binaries, `REQUESTS_CA_BUNDLE` for anything Python they shell out to,
#: and `HAP_CA_BUNDLE` so `network.py` sees the same file.
CA_VARS = ("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "HAP_CA_BUNDLE")

#: Never proxied. The fx MCP server is on localhost (`scene.py`) and
#: opencode's TUI talks to its own local HTTP server: sending those through
#: a corporate proxy hangs instead of failing.
LOCAL_BYPASS = ("localhost", "127.0.0.1", "::1")


def _environ(environ: "Mapping[str, str] | None") -> "Mapping[str, str]":
    return os.environ if environ is None else environ


def effective_proxy(settings, environ: "Mapping[str, str] | None" = None) -> str:
    """The proxy in force: the artist's field, else whatever the machine says.

    The inheritance half is the important one. A studio that already exports
    `HTTPS_PROXY` machine-wide is correctly configured, and the panel's job
    is to pass that through, not to blank it.
    """
    typed = (getattr(settings, "proxy_url", "") or "").strip()
    if typed:
        return typed
    env = _environ(environ)
    for name in PROXY_VARS:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return ""


def effective_ca_bundle(settings, environ: "Mapping[str, str] | None" = None) -> str:
    typed = (getattr(settings, "ca_bundle", "") or "").strip()
    if typed:
        return typed
    env = _environ(environ)
    for name in CA_VARS:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return ""


def _split(text: str) -> list[str]:
    """Split a NO_PROXY value. Comma or whitespace, both are in the wild."""
    return [item.strip() for item in text.replace(" ", ",").split(",") if item.strip()]


def no_proxy_value(settings, environ: "Mapping[str, str] | None" = None) -> str:
    env = _environ(environ)
    parts: list[str] = list(LOCAL_BYPASS)
    inherited = env.get("NO_PROXY") or env.get("no_proxy") or ""
    for chunk in (inherited, getattr(settings, "no_proxy", "") or ""):
        for item in _split(chunk):
            if item not in parts:
                parts.append(item)
    return ",".join(parts)


def child_env(settings, environ: "Mapping[str, str] | None" = None) -> dict[str, str]:
    """Environment additions for a spawned agent, npx, or npm.

    Empty in, empty out: a machine with no proxy and no custom CA gets no
    variables at all, because an empty `NODE_EXTRA_CA_CERTS` is a path Node
    tries to read and fails on.
    """
    env: dict[str, str] = {}

    address = effective_proxy(settings, environ)
    if address:
        for name in PROXY_VARS:
            env[name] = address
        bypass = no_proxy_value(settings, environ)
        env["NO_PROXY"] = bypass
        env["no_proxy"] = bypass

    bundle = effective_ca_bundle(settings, environ)
    if bundle:
        for name in CA_VARS:
            env[name] = bundle

    return env


def sanitize(url: str) -> str:
    """A proxy URL safe to print. The password becomes `***`.

    Diagnostics get pasted into bug reports and the logbook goes to disk;
    neither is a place for the studio's proxy password.
    """
    try:
        parts = urlsplit(url)
        if not parts.password:
            return url
        userinfo = f"{parts.username or ''}:***"
        netloc = f"{userinfo}@{parts.hostname or ''}"
        if parts.port:
            netloc = f"{netloc}:{parts.port}"
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except ValueError:
        return url


__all__ = [
    "CA_VARS",
    "LOCAL_BYPASS",
    "PROXY_VARS",
    "child_env",
    "effective_ca_bundle",
    "effective_proxy",
    "no_proxy_value",
    "sanitize",
]

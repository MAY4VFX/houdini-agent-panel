"""The panel's single door to the network.

Everything that reaches outward — the registry, PyPI, the announcements
feed, nodejs.org, agent archives — must accept a ``fetch`` parameter of
this type. Two reasons.

First: a test shouldn't depend on the internet, and mocking one protocol is
cheaper than patching ``urllib`` in six modules.

Second, more importantly: design.md records a promise that with
announcements and telemetry turned off, the panel makes zero requests. That
promise is only checkable if requests physically go through a single
function a test can count.
"""

from __future__ import annotations

import os
import ssl
import urllib.error
import urllib.request
from typing import Callable, Protocol

#: The panel identifies itself honestly: a studio admin who spots this in
#: proxy logs should be able to tell what software is reaching out.
USER_AGENT = "houdini-agent-panel"

DEFAULT_TIMEOUT = 30.0

#: Custom root CA bundle for studios with an intercepting proxy.
CA_BUNDLE_ENV = "HAP_CA_BUNDLE"

_ssl_context: ssl.SSLContext | None = None

#: Settings-sourced proxy URL and CA bundle path, set via `configure()`.
#: `None` means "nothing explicit" — fall back to what the environment
#: already provides (see `configure()` and `ssl_context()`).
_proxy_url: str | None = None
_ca_bundle_override: str | None = None
_opener: urllib.request.OpenerDirector | None = None


def configure(*, proxy: str | None = None, ca_bundle: str | None = None) -> None:
    """Apply the artist's Network settings to every request this module makes.

    Called once at startup and again every time the artist changes the
    Network settings — an empty string or `None` means "nothing set", not
    "set to empty". This is the only writer of the module's proxy/CA
    state, and it always drops the cached SSL context and opener: without
    that, a changed setting would silently keep using the old proxy or
    bundle until Houdini restarts, which is exactly the kind of thing that
    turns a five-minute studio fix into a support ticket.
    """
    global _proxy_url, _ca_bundle_override, _ssl_context, _opener
    _proxy_url = proxy or None
    _ca_bundle_override = ca_bundle or None
    _ssl_context = None
    _opener = None


def ssl_context() -> ssl.SSLContext:
    """A TLS context that also works inside Houdini.

    The Python that ships with Houdini is built without a root CA bundle:
    any HTTPS request from it fails with
    ``CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate``
    (verified by running inside Houdini 22.0.368). And the panel reaches
    the network from exactly there — for the registry, agents, Node,
    versions, and announcements. Without this, none of its network
    functions work.

    So we take the bundle from ``certifi``: it already ships alongside our
    dependencies anyway. Disabling certificate verification isn't an
    option: we download executable files over these connections.

    A studio behind a TLS-inspecting proxy (Zscaler, Netskope, a corporate
    Squid) presents its own certificate, so verification against certifi
    alone fails there — not because the studio is doing anything wrong,
    but because certifi has no way to know about a CA that only that
    studio trusts. `configure(ca_bundle=...)` is how the Network setting
    reaches this function; `HAP_CA_BUNDLE` is the older, env-only way to
    say the same thing and stays as a fallback for it. Either way, this
    only ever *adds* a trusted issuer — there is no setting that disables
    verification, and there must never be one: the panel downloads and
    runs executables over these connections.
    """
    global _ssl_context
    if _ssl_context is not None:
        return _ssl_context

    override = _ca_bundle_override or os.environ.get(CA_BUNDLE_ENV)
    if override and os.path.exists(override):
        _ssl_context = ssl.create_default_context(cafile=override)
        return _ssl_context

    try:
        import certifi

        _ssl_context = ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - outside Houdini there's usually a system bundle
        _ssl_context = ssl.create_default_context()
    return _ssl_context


def _opener_director() -> urllib.request.OpenerDirector:
    """The opener every real request in this module goes through.

    Built once and cached (reset by `configure()`) so a proxy setting
    doesn't get re-parsed on every single request. With no explicit proxy
    (`configure()` never called, or called with `proxy=None`) this behaves
    exactly like plain `urllib.request.urlopen()` did before this
    function existed: `build_opener()` still adds its own default
    `ProxyHandler`, which reads `*_PROXY`/`*_proxy` from the environment
    and, on macOS, the system network settings — a studio that already
    exports the proxy machine-wide keeps working unchanged. An explicit
    `configure(proxy=...)` overrides that discovery instead of merging
    with it, so the artist's setting is never second-guessed by a stray
    environment variable.
    """
    global _opener
    if _opener is not None:
        return _opener

    handlers: list[urllib.request.BaseHandler] = []
    if _proxy_url:
        handlers.append(urllib.request.ProxyHandler({"http": _proxy_url, "https": _proxy_url}))
    handlers.append(urllib.request.HTTPSHandler(context=ssl_context()))
    _opener = urllib.request.build_opener(*handlers)
    return _opener


class NetworkError(RuntimeError):
    """Anything that prevented getting a response. The reason is in the text."""


class Fetcher(Protocol):
    def __call__(self, url: str, *, timeout: float = DEFAULT_TIMEOUT) -> bytes: ...


#: Progress callback for long downloads. ``total`` is None if the server
#: didn't send a Content-Length.
Progress = Callable[[int, "int | None", str], None]


def urlopen_fetch(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> bytes:
    """Fetch a URL in full. Fine for JSON, not for archives."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with _opener_director().open(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        raise NetworkError(f"{url}: HTTP {exc.code} {exc.reason}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise NetworkError(f"{url}: {exc}") from exc


def stream_fetch(
    url: str,
    destination,
    *,
    timeout: float = DEFAULT_TIMEOUT,
    progress: Progress | None = None,
    chunk_size: int = 1 << 16,
) -> int:
    """Download a URL into an open binary file, reporting progress.

    Agent and Node archives are tens of megabytes. There's no reason to
    read them into memory whole just to write them back out, and there's
    nothing to draw a progress bar from without a streamed download.
    Returns the number of bytes written.
    """
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with _opener_director().open(request, timeout=timeout) as response:
            raw_length = response.headers.get("Content-Length")
            total = int(raw_length) if raw_length and raw_length.isdigit() else None
            done = 0
            while True:
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                destination.write(chunk)
                done += len(chunk)
                if progress is not None:
                    progress(done, total, url.rsplit("/", 1)[-1])
            return done
    except urllib.error.HTTPError as exc:
        raise NetworkError(f"{url}: HTTP {exc.code} {exc.reason}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise NetworkError(f"{url}: {exc}") from exc


def fetch_json(url: str, *, fetch: Fetcher | None = None, timeout: float = DEFAULT_TIMEOUT):
    """Fetch and parse JSON. Garbage in the response is also a ``NetworkError``."""
    import json

    payload = (fetch or urlopen_fetch)(url, timeout=timeout)
    try:
        return json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise NetworkError(f"{url}: response did not parse as JSON: {exc}") from exc

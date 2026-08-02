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
    """
    global _ssl_context
    if _ssl_context is not None:
        return _ssl_context

    override = os.environ.get(CA_BUNDLE_ENV)
    if override and os.path.exists(override):
        _ssl_context = ssl.create_default_context(cafile=override)
        return _ssl_context

    try:
        import certifi

        _ssl_context = ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - outside Houdini there's usually a system bundle
        _ssl_context = ssl.create_default_context()
    return _ssl_context


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
        with urllib.request.urlopen(request, timeout=timeout, context=ssl_context()) as response:
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
        with urllib.request.urlopen(request, timeout=timeout, context=ssl_context()) as response:
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

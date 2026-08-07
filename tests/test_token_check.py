"""`token_check`: does a captured token actually work?

The measurements behind this module (docs/facts/acp-sdk.md §27) came from
the owner's own machine: the whole 108-character token returned HTTP 200
from `GET /v1/models`, and the same token one character short (§26)
returned HTTP 401.
"""

from __future__ import annotations

import urllib.error

import pytest

from houdini_agent_panel import token_check

#: Captured at import, before `conftest`'s autouse stub replaces it. That
#: stub exists so no other test can accidentally reach the real API; this
#: file is the one place that must exercise the real function.
_REAL_VERIFY = token_check.verify


@pytest.fixture(autouse=True)
def real_verify(monkeypatch):
    monkeypatch.setattr("houdini_agent_panel.token_check.verify", _REAL_VERIFY)


class _FakeResponse:
    def __init__(self, status: int = 200) -> None:
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    def __init__(self, outcome) -> None:
        self.outcome = outcome
        self.requests: list = []

    def open(self, request, timeout=None):
        self.requests.append(request)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def test_a_working_token_is_valid():
    opener = _FakeOpener(_FakeResponse(200))
    assert token_check.verify("sk-ant-oat01-whole", opener=opener) == token_check.VALID


def test_a_401_is_rejected_so_it_never_overwrites_a_working_token():
    """The whole point. §26's corrupted token answered exactly this."""
    error = urllib.error.HTTPError(token_check.MODELS_URL, 401, "Unauthorized", {}, None)
    opener = _FakeOpener(error)
    assert token_check.verify("sk-ant-at01-short", opener=opener) == token_check.REJECTED


def test_a_403_is_not_rejected():
    """"Authenticated, but this request isn't allowed" is a real answer
    about a readable token. Refusing to store it would throw away a
    credential the API just proved it could read."""
    error = urllib.error.HTTPError(token_check.MODELS_URL, 403, "Forbidden", {}, None)
    opener = _FakeOpener(error)
    assert token_check.verify("sk-ant-oat01-whole", opener=opener) == token_check.UNKNOWN


def test_no_network_is_unknown_not_rejected():
    """An artist with no connection still keeps the token they minted."""
    opener = _FakeOpener(urllib.error.URLError("no route to host"))
    assert token_check.verify("sk-ant-oat01-whole", opener=opener) == token_check.UNKNOWN


def test_a_timeout_is_unknown_too():
    opener = _FakeOpener(TimeoutError("timed out"))
    assert token_check.verify("sk-ant-oat01-whole", opener=opener) == token_check.UNKNOWN


def test_an_empty_token_is_rejected_without_asking_anyone():
    opener = _FakeOpener(_FakeResponse(200))
    assert token_check.verify("", opener=opener) == token_check.REJECTED
    assert opener.requests == [], "an empty token is not worth a request"


def test_the_request_carries_the_oauth_bearer_headers():
    opener = _FakeOpener(_FakeResponse(200))
    token_check.verify("sk-ant-oat01-whole", opener=opener)

    request = opener.requests[0]
    assert request.full_url == token_check.MODELS_URL
    headers = {k.lower(): v for k, v in request.header_items()}
    assert headers["authorization"] == "Bearer sk-ant-oat01-whole"
    assert headers["anthropic-beta"] == token_check.OAUTH_BETA
    assert "anthropic-version" in headers


def test_models_is_used_rather_than_messages():
    """A sign-in check must not bill the artist — listing models
    authenticates identically and invokes no model."""
    assert token_check.MODELS_URL.endswith("/v1/models")

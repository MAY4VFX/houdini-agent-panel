"""Does a captured OAuth token actually work?

The panel has, three separate times, stored a token that was structurally
plausible and completely useless: the wrong anchor (docs/facts/acp-sdk.md
§21), a value truncated by line wrapping (§25), and a value one character
short because an escape sequence ate it (§26). Every one of those was
reported to the artist as a **successful sign-in**, because the only
question ever asked was whether a token EXISTED.

The owner's own account of it is the part that decided this module::

    "the time before last, I checked the chat before signing in and it
     worked, and after signing in it broke"

That is the real cost. A capture overwrites whatever was stored, so an
artist with a working token who signs in again — the obvious thing to try
when something looks wrong — destroys the one good credential they had.
Verifying costs one request; not verifying cost a whole working day.

Measured against the real API from the owner's machine (2026-08-08), the
same token in both cases:

===========================  ============================================
token                        ``GET /v1/models``
===========================  ============================================
whole, 108 characters        HTTP 200
one character short (§26)    HTTP 401 ``authentication_error``
===========================  ============================================

``/v1/models`` is the right question to ask: it authenticates exactly like
a prompt does, and it spends nothing — no model is invoked, so a check
that runs on every sign-in never costs the artist anything.
"""

from __future__ import annotations

import urllib.error
import urllib.request

from . import network

#: Not `api.anthropic.com/v1/messages`: that would bill the artist for a
#: sign-in check. Listing models authenticates the same way and is free.
MODELS_URL = "https://api.anthropic.com/v1/models"

#: The header that makes the API read `Authorization: Bearer` as a
#: subscription OAuth token rather than an API key. Measured: without it
#: the same token still authenticates, but naming it keeps this request
#: honest about what kind of credential it is carrying.
OAUTH_BETA = "oauth-2025-04-20"

#: Short on purpose. This runs between "the artist finished signing in"
#: and "the panel says so"; a slow network must not turn a successful
#: sign-in into a hang. A timeout lands on `UNKNOWN`, which stores the
#: token exactly as the panel did before this module existed.
TIMEOUT = 10.0

#: The token authenticated. Store it.
VALID = "valid"
#: The API refused it — 401/403. Storing this would overwrite a working
#: credential with a broken one, which is the failure this exists to stop.
REJECTED = "rejected"
#: No answer either way: offline, proxy down, timeout, anything else.
#: Never a reason to throw away a token — an artist on a plane still
#: deserves their sign-in to be kept.
UNKNOWN = "unknown"


def verify(token: str, *, opener=None, timeout: float = TIMEOUT) -> str:
    """`VALID`, `REJECTED` or `UNKNOWN` for `token`.

    Goes through `network`'s own opener, so the studio proxy and CA
    bundle the artist configured apply here exactly as they do to every
    other request the panel makes. `opener` is for tests.
    """
    if not token:
        return REJECTED

    request = urllib.request.Request(
        MODELS_URL,
        headers={
            "authorization": f"Bearer {token}",
            "anthropic-version": "2023-06-01",
            "anthropic-beta": OAUTH_BETA,
            "User-Agent": network.USER_AGENT,
        },
    )
    director = opener if opener is not None else network._opener_director()
    try:
        with director.open(request, timeout=timeout) as response:
            return VALID if 200 <= getattr(response, "status", 200) < 300 else UNKNOWN
    except urllib.error.HTTPError as exc:
        # 401 is a refused credential. 403 is "authenticated, but this
        # request isn't allowed" — a real answer about the token being
        # readable, and not something a retry of the sign-in will fix, so
        # it is deliberately NOT `REJECTED`: refusing to store a token
        # the API just proved it can read would be worse than useless.
        return REJECTED if exc.code == 401 else UNKNOWN
    except (urllib.error.URLError, OSError):
        return UNKNOWN

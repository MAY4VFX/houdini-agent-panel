"""Composing and sending an in-panel bug report.

Pure Python, no Qt — `ui/bugreport_view.py` reads/edits these pieces on
screen, `ui/bugreport_worker.py` sends the final payload off the main
thread. Kept separate the same way `updates.py`/`announcements.py` are:
this module is what a test exercises directly, without a `QApplication`.

The receiving service is `houdini-panel-bugreport`
(`~/Github/houdini-panel-bugreport`, not this repo) — read for the real
payload shape rather than guessed:

    POST /v1/reports
    {
        "project": "houdini-agent-panel",   # required, routes to a repo server-side
        "title": "...",                     # required, 3-200 chars
        "body": "...",                      # required, 10-8000 chars
        "panel_version": "...",             # optional, <=200 chars
        "houdini_version": "...",           # optional, <=200 chars
        "os": "...",                        # optional, <=200 chars
        "agent_id": "..."                   # optional, <=200 chars
    }
    -> 201 {"issue_url": "https://github.com/..."}

The owner explicitly ruled out a screenshot and explicitly chose these
three attachments: versions/system, the tail of the panel's own log, and
the last few messages of the current conversation. What matters more than
the feature itself: the conversation tail is the content of the artist's
own work, going to a PUBLIC issue tracker. So nothing here silently
decides what gets sent — every piece gathered here is meant to land in an
EDITABLE field the artist reads before anything leaves the machine
(`ui/bugreport_view.py`'s own docstring has the UI side of that promise).

Redaction happens at TWO points, deliberately not one:

- Here, once, when each attachment is first gathered — so what the artist
  sees already has known credential shapes replaced with `[REDACTED]`,
  and never has to notice one in the log tail or conversation excerpt to
  begin with.
- Again in `ui/bugreport_view.py`, immediately before sending, over
  whatever is in the fields AT THAT MOMENT — including anything the
  artist typed or pasted themselves. If that second pass changes
  anything, sending is held and the field is updated with what was
  redacted, so the artist reads the ACTUAL text before it goes out a
  second time — never a silent rewrite between "I read this" and "this
  left the machine".

The service does its own redaction too (`bugreport/redact.py`, same
credential shapes, ported here rather than imported — the two repos don't
share a dependency) — neither side is the only guard.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:
    from .transcript_model import Entry

#: The key `houdini-panel-bugreport`'s `projects.json` maps to this repo —
#: never a repo path or owner/repo pair, so this process can only ever
#: file into whatever the SERVICE'S own registry says that key means.
PROJECT_KEY = "houdini-agent-panel"

#: Not live yet (team lead, filing this task) — configurable in Settings
#: for exactly that reason, this is only ever the starting default.
DEFAULT_ENDPOINT = "https://issue.ai-vfx.com/v1/reports"

#: Mirrors `bugreport/validation.py` in the service repo — used here only
#: to give the artist an honest, immediate reason before a round trip,
#: never trusted instead of the server's own check (`looks_like_report`
#: runs there regardless of what this side thinks).
TITLE_MIN = 3
TITLE_MAX = 200
BODY_MIN = 10
BODY_MAX = 8000
FIELD_MAX = 200

#: How much of the panel's own log and the conversation to pre-fill —
#: generous enough to actually show the failure, small enough that the
#: rest of a report (the artist's own words, system info) still fits
#: comfortably under `BODY_MAX` once everything is joined. Not a hard
#: cap: the artist can always type more into the same editable field, the
#: server's own `BODY_MAX` is what actually enforces a ceiling.
_LOG_TAIL_MAX_LINES = 60
_CONVERSATION_TAIL_MAX_MESSAGES = 6
_CONVERSATION_MESSAGE_MAX_CHARS = 800


class BugReportError(RuntimeError):
    """Raised with a message already written for the artist — the same
    shape `self_update.SelfUpdateError` uses, for the same reason:
    classifying a failure happens once, here, not re-derived in the UI
    from a raw status code."""


# --- redaction ---------------------------------------------------------

_REDACTED = "[REDACTED]"

#: Ported from `bugreport/redact.py` in the service repo, not imported —
#: the two repos share no dependency, and this needs to run before
#: anything is even shown on screen, independent of whether the network
#: request ever happens. Keep these two lists in sync by hand; a secret
#: shape either side misses is still caught by the OTHER side, which is
#: the whole reason there are two.
_TOKEN_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{36,}"),  # GitHub PAT / OAuth / app tokens
    re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),  # GitHub fine-grained PAT
    re.compile(r"sk-(live|proj|ant)?-?[A-Za-z0-9]{20,}"),  # OpenAI/Anthropic-style
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key id
    re.compile(r"AIza[0-9A-Za-z\-_]{35}"),  # Google API key
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),  # Slack tokens
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),  # JWT
]

_PEM_PATTERN = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)

_ASSIGNMENT_PATTERN = re.compile(
    r"(?im)\b((?:api[_-]?key|secret|token|password|passwd|access[_-]?key|"
    r"private[_-]?key|client[_-]?secret)s?)\b\s*[:=]\s*['\"]?"
    r"([A-Za-z0-9\-_./+=]{12,})['\"]?"
)


def redact_secrets(text: str) -> tuple[str, bool]:
    """`(redacted_text, changed)` — `changed` is how the UI knows to say
    "this was redacted" instead of silently altering the artist's words.

    Anything not recognisably one of these shapes is left untouched on
    purpose (same reasoning as the service's own copy of this list): the
    goal is catching known credential formats, not a second guard that
    mangles a stack trace or an ordinary URL.
    """
    if not text:
        return text or "", False
    redacted = _PEM_PATTERN.sub(_REDACTED, text)
    for pattern in _TOKEN_PATTERNS:
        redacted = pattern.sub(_REDACTED, redacted)
    redacted = _ASSIGNMENT_PATTERN.sub(lambda m: f"{m.group(1)}: {_REDACTED}", redacted)
    return redacted, redacted != text


# --- gathering context ---------------------------------------------------


@dataclass
class SystemFields:
    """The four optional, structured fields the service's own schema
    defines — shown to the artist as plain, editable text (not hidden
    behind a checkbox), and sent exactly as shown; nothing about the
    request is composed anywhere the artist hasn't already read it."""

    panel_version: str = ""
    houdini_version: str = ""
    os: str = ""
    agent_id: str = ""

    def as_text(self) -> str:
        return (
            f"Panel version: {self.panel_version}\n"
            f"Houdini version: {self.houdini_version}\n"
            f"OS: {self.os}\n"
            f"Agent: {self.agent_id}"
        )


def gather_system_fields(agent_id: str) -> SystemFields:
    """Must be called from the main thread — `scene.houdini_version()`
    falls back to `import hou` when `$HOUDINI_VERSION` isn't set, and
    `hou` is never touched off the main thread (this project's own rule).
    Every piece is wrapped so a single unavailable fact (fx not running,
    `hou` import failing for an unrelated reason) doesn't blank the rest —
    diagnostics must never raise, same principle as `settings.diagnostics`.
    """
    import platform
    import sys

    from . import __version__ as panel_version

    try:
        from . import scene

        houdini_version = scene.houdini_version()
    except Exception:  # noqa: BLE001 - a bug report must not itself crash the panel
        houdini_version = ""

    try:
        os_text = f"{platform.platform()} (python {sys.version.split()[0]})"
    except Exception:  # noqa: BLE001
        os_text = ""

    return SystemFields(
        panel_version=panel_version,
        houdini_version=houdini_version or "",
        os=os_text,
        agent_id=agent_id or "",
    )


def read_log_tail(path, *, max_lines: int = _LOG_TAIL_MAX_LINES) -> tuple[str, bool]:
    """`(text, redacted)` — the last `max_lines` of the panel's own log.

    `logbook.py`'s own rule is "never leak — no scene contents, no prompt
    text, no session ids" (its module docstring), so this is expected to
    already be clean; the redaction pass here is the same belt-and-braces
    treatment every other attachment gets, not a sign this one is
    special. A missing or unreadable log is not an error — it just means
    there's nothing to attach yet (a fresh install, or logging turned off
    via `HAP_LOG=0`).
    """
    try:
        text = path.read_text("utf-8", errors="replace")
    except OSError:
        return "", False
    lines = text.splitlines()[-max_lines:]
    return redact_secrets("\n".join(lines))


def conversation_tail_text(
    entries: Sequence["Entry"],
    *,
    max_messages: int = _CONVERSATION_TAIL_MAX_MESSAGES,
    max_chars_per_message: int = _CONVERSATION_MESSAGE_MAX_CHARS,
) -> tuple[str, bool]:
    """`(text, redacted)` — the last few user/agent turns, as plain "Who:
    text" lines.

    Only `user`/`agent` entries — not `tool`/`plan`/`activity`/`thought`,
    which read as internal bookkeeping rather than "the conversation" a
    report is meant to show; a tool call's own content can already be
    reproduced from the artist's own description of what they asked for.
    Each message is truncated on its own (not just the whole block), so
    one very long turn doesn't crowd out the ones around it that might
    actually show the failure.
    """
    turns = [e for e in entries if e.kind in ("user", "agent") and e.text.strip()]
    tail = turns[-max_messages:] if max_messages > 0 else []
    lines: list[str] = []
    for entry in tail:
        who = "You" if entry.kind == "user" else "Agent"
        text = entry.text.strip()
        if len(text) > max_chars_per_message:
            text = text[:max_chars_per_message].rstrip() + "…"
        lines.append(f"{who}: {text}")
    return redact_secrets("\n\n".join(lines))


# --- sending -------------------------------------------------------------


def _extract_error_detail(body: bytes) -> str:
    """The service's error responses use `{"detail": "..."}` for anything
    raised as `HTTPException` (404/422/429/502/503) and `{"error": "..."}`
    for the one check that runs before the body is even parsed (413, the
    request-size middleware) — both are checked, falling back to the raw
    body if neither key is there, since an unrecognised error shape is
    still better shown than swallowed."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return body.decode("utf-8", errors="replace").strip()
    if isinstance(payload, dict):
        for key in ("detail", "error"):
            value = payload.get(key)
            if value:
                return str(value)
    return body.decode("utf-8", errors="replace").strip()


def post_report(
    endpoint: str,
    payload: dict,
    *,
    env: dict[str, str],
    timeout: float = 30.0,
) -> str:
    """POSTs `payload` as JSON to `endpoint`. Returns the issue URL.

    `env` is the composed environment `TerminalLoginWorker.build_env`
    already produces (the artist's login-shell proxy widened by Houdini's
    own blind one, then the studio proxy from Settings on top) — reused
    here rather than re-derived, same reasoning `self_update.py` already
    applies for the same kind of subprocess-shaped proxy problem, just
    read for its `HTTPS_PROXY`/`HTTP_PROXY` values instead of handed to a
    child process, since this is a plain in-process request, not a spawn.
    TLS verification reuses `network.ssl_context()` — the same certifi
    bundle (or studio CA override) every other network call in this
    panel already needs for the same reason (Houdini's own Python ships
    with no root CA bundle at all).

    Distinguishes a request that never reached the server (DNS, refused,
    timed out — the case this endpoint's own "not live yet" status makes
    the one actually testable today) from one the server answered with an
    error, since the two need different next steps from the artist.
    """
    from . import network as network_module

    data = json.dumps(payload).encode("utf-8")
    proxy = (
        env.get("HTTPS_PROXY") or env.get("HTTP_PROXY")
        or env.get("https_proxy") or env.get("http_proxy")
    )
    handlers: list[urllib.request.BaseHandler] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    handlers.append(urllib.request.HTTPSHandler(context=network_module.ssl_context()))
    opener = urllib.request.build_opener(*handlers)

    request = urllib.request.Request(
        endpoint,
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": network_module.USER_AGENT,
        },
    )
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        detail = _extract_error_detail(exc.read())
        raise BugReportError(f"The server rejected the report (HTTP {exc.code}): {detail}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise BugReportError(f"Could not reach {endpoint}: {exc.reason if hasattr(exc, 'reason') else exc}") from exc

    try:
        result = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise BugReportError(f"The server's response didn't look like JSON: {exc}") from exc

    issue_url = result.get("issue_url") if isinstance(result, dict) else None
    if not issue_url:
        raise BugReportError("The server accepted the report but didn't say where it filed the issue.")
    return str(issue_url)


__all__ = [
    "PROJECT_KEY",
    "DEFAULT_ENDPOINT",
    "TITLE_MIN",
    "TITLE_MAX",
    "BODY_MIN",
    "BODY_MAX",
    "FIELD_MAX",
    "BugReportError",
    "SystemFields",
    "redact_secrets",
    "gather_system_fields",
    "read_log_tail",
    "conversation_tail_text",
    "post_report",
]

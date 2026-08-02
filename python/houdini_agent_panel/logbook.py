"""On-disk log — the only way to diagnose the panel inside a real Houdini.

Written after a diagnosis that went in circles. Everything worked headless:
the agent connected, the session was created, all six agents answered. In the
artist's Houdini nothing worked at all — and there was not a single line
anywhere to say why. ``paths.logs_dir()`` existed and nobody ever wrote to it.

Hence the rules here:

**Never raise.** A log that breaks the panel is worse than no log. Every entry
point swallows its own errors.

**Never leak.** The same discipline as telemetry: no scene contents, no
prompt text, no session ids. Paths do get written — this file stays on the
artist's own machine and is only sent along deliberately — but nothing from
inside the conversation.

**Bounded.** One file, rotated by size. An agent that spews to stderr must not
fill up the disk.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import platform
import sys

from . import paths

LOG_FILE_NAME = "panel.log"

#: Two megabytes and one backup. Enough for several sessions of a chatty
#: agent, small enough to attach to a bug report without thinking about it.
_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 1

#: Set to "0" to turn the log off entirely.
ENABLE_ENV = "HAP_LOG"

_configured = False


def logger(name: str = "houdini_agent_panel") -> logging.Logger:
    return logging.getLogger(name)


def log_path():
    return paths.logs_dir() / LOG_FILE_NAME


def setup(*, force: bool = False) -> None:
    """Attach a file handler to the package logger. Idempotent.

    Attaches to our own logger rather than the root one deliberately: the
    panel lives inside someone else's process, and hijacking root logging
    would mean writing Houdini's own messages into our file — and possibly
    changing how Houdini's messages behave.
    """
    global _configured
    if _configured and not force:
        return
    if os.environ.get(ENABLE_ENV) == "0":
        _configured = True
        return

    try:
        target = log_path()
        handler = logging.handlers.RotatingFileHandler(
            target, maxBytes=_MAX_BYTES, backupCount=_BACKUP_COUNT, encoding="utf-8"
        )
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
        )
        package_logger = logger()
        # Repeated setup() calls (two panel tabs, a reload) must not stack
        # handlers — otherwise every line lands in the file N times.
        for existing in list(package_logger.handlers):
            if isinstance(existing, logging.handlers.RotatingFileHandler):
                package_logger.removeHandler(existing)
                existing.close()
        package_logger.addHandler(handler)
        package_logger.setLevel(logging.DEBUG)
        # Houdini's console is not ours to spam: our records go to the file
        # and stop there.
        package_logger.propagate = False
    except Exception:  # noqa: BLE001 - a log has no right to break the panel
        _configured = True
        return

    _configured = True
    _log_environment()


def _log_environment() -> None:
    """The header of every session — the answers to the questions asked first."""
    try:
        from . import __version__

        log = logger()
        log.info("--- panel start ---")
        log.info("panel %s from %s", __version__, os.path.dirname(__file__))
        log.info("python %s", sys.version.split()[0])
        log.info("platform %s", platform.platform())
        log.info("PATH=%s", os.environ.get("PATH", ""))
        try:
            from .ui.qt import QT_SOURCE, QT_VERSION

            log.info("qt %s via %s", QT_VERSION, QT_SOURCE)
        except Exception as exc:  # noqa: BLE001
            log.warning("qt unavailable: %r", exc)
        try:
            from . import node as node_module

            log.info("system node: %s", node_module.find_system_node())
        except Exception as exc:  # noqa: BLE001
            log.warning("node lookup failed: %r", exc)
        try:
            from . import scene

            log.info("houdini %s, fx port %s", scene.houdini_version(), scene.fx_port())
        except Exception as exc:  # noqa: BLE001
            log.warning("houdini unavailable: %r", exc)
    except Exception:  # noqa: BLE001
        return


def attach_client(client) -> None:
    """Write everything the ACP client reports into the log.

    Called from the panel. Deliberately subscribes only to signals that carry
    no conversation content: connection state, failures, and the agent's own
    stderr. Message chunks are not logged — that's the artist's text.
    """
    log = logger("houdini_agent_panel.client")
    try:
        client.connected.connect(
            lambda info: log.info("connected: %s %s", info.name, info.version)
        )
        client.disconnected.connect(lambda reason: log.warning("disconnected: %s", reason))
        client.failed.connect(lambda message: log.error("failed: %s", message))
        client.auth_required.connect(
            lambda methods: log.info("auth required: %s", [m.id for m in methods])
        )
        client.error.connect(lambda _sid, message: log.error("agent error: %s", message))
        client.log_line.connect(lambda line: log.debug("agent stderr: %s", line))
        client.session_started.connect(lambda _sid, _state: log.info("session started"))
        client.turn_finished.connect(lambda _sid, reason: log.info("turn finished: %s", reason))
    except Exception:  # noqa: BLE001
        return


def diagnostics_tail(lines: int = 200) -> str:
    """The tail of the log, for the "Copy diagnostics" button."""
    try:
        text = log_path().read_text("utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.splitlines()[-lines:])

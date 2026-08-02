"""On-disk log.

Written after a diagnosis that went in circles: everything worked headless,
nothing worked in the artist's Houdini, and there was not one line anywhere
saying why. These tests pin the properties that make the log trustworthy
enough to leave switched on in someone else's process.
"""

from __future__ import annotations

import logging

from houdini_agent_panel import logbook


def _reset():
    logbook._configured = False
    package_logger = logbook.logger()
    for handler in list(package_logger.handlers):
        package_logger.removeHandler(handler)
        handler.close()


def test_setup_writes_a_session_header(data_dir):
    _reset()
    logbook.setup()

    text = logbook.log_path().read_text("utf-8")
    assert "--- panel start ---" in text
    assert "panel " in text and "python " in text


def test_setup_twice_does_not_double_every_line(data_dir):
    """Two panel tabs call setup() each. Stacked handlers would write every
    record N times and make the log unreadable exactly when it's needed."""
    _reset()
    logbook.setup()
    logbook.setup(force=True)

    logbook.logger("houdini_agent_panel.test").error("boom")
    text = logbook.log_path().read_text("utf-8")

    assert text.count("boom") == 1


def test_logging_never_raises_when_the_directory_is_unusable(monkeypatch):
    """A log that breaks the panel is worse than no log."""
    _reset()

    def explode():
        raise OSError("read-only filesystem")

    monkeypatch.setattr(logbook.paths, "logs_dir", explode)
    logbook.setup()  # must not raise

    logbook.logger("houdini_agent_panel.test").error("still fine")


def test_disabled_by_env_writes_nothing(data_dir, monkeypatch):
    _reset()
    monkeypatch.setenv(logbook.ENABLE_ENV, "0")
    logbook.setup()

    assert not logbook.log_path().exists()


def test_our_records_do_not_leak_into_houdinis_own_logging(data_dir):
    """The panel lives inside someone else's process: our records go to our
    file and stop there."""
    _reset()
    logbook.setup()

    assert logbook.logger().propagate is False


def test_attach_client_survives_a_client_without_signals():
    """attach_client is called on a real client, but must not be the thing
    that breaks a panel if the client's shape ever changes."""
    _reset()

    class Bare:
        pass

    logbook.attach_client(Bare())  # must not raise


def test_diagnostics_tail_returns_last_lines(data_dir):
    _reset()
    logbook.setup()
    log = logbook.logger("houdini_agent_panel.test")
    for index in range(50):
        log.info("line-%d", index)

    tail = logbook.diagnostics_tail(lines=5)

    assert "line-49" in tail
    assert "line-10" not in tail
    assert len(tail.splitlines()) == 5


def test_rotation_is_bounded(data_dir):
    _reset()
    logbook.setup()
    handlers = [
        h for h in logbook.logger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert handlers, "a file handler must be attached"
    assert handlers[0].maxBytes > 0, "an unbounded log can fill the artist's disk"

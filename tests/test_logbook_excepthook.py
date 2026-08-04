"""An exception nobody would otherwise see reaches the log — and the process
still reports it the way it did before.

An exception escaping `QThread.run()` (or any Qt slot) is printed by PySide
to raw stderr, not through `logging`. Inside Houdini nobody reads stderr, so
a real failure left nothing in the one file we ask artists to send us.
Measured on 20.5.445 and 22.0.368: of `sys.unraisablehook`,
`threading.excepthook`, `qInstallMessageHandler` and `sys.excepthook`, only
the last one sees it.

The second test is the one that matters. Overriding a process-global hook
from inside someone else's application is only defensible while it stays
strictly additive: Houdini's own crash reporting, or another plugin's, must
still run.
"""

from __future__ import annotations

import logging
import sys

import pytest

from houdini_agent_panel import logbook


@pytest.fixture
def fresh_hook():
    original = sys.excepthook
    logbook._reset_excepthook_for_tests()
    yield
    sys.excepthook = original
    logbook._reset_excepthook_for_tests()


def _raise_and_report() -> tuple:
    try:
        raise RuntimeError("worker died with nobody watching")
    except RuntimeError:
        return sys.exc_info()


def test_the_previous_hook_still_runs(fresh_hook):
    """The whole justification for touching a global. If this breaks, the
    panel has quietly disabled its host's crash reporting."""
    seen: list[str] = []
    sys.excepthook = lambda t, v, tb: seen.append(str(v))

    logbook._install_excepthook()
    sys.excepthook(*_raise_and_report())

    assert seen == ["worker died with nobody watching"]


def test_the_exception_reaches_our_log(fresh_hook, caplog):
    logbook._install_excepthook()
    with caplog.at_level(logging.ERROR, logger="houdini_agent_panel"):
        sys.excepthook(*_raise_and_report())

    assert any("unhandled exception" in r.message for r in caplog.records)
    assert any(r.exc_info for r in caplog.records), "the traceback must be kept"


def test_installing_twice_does_not_stack_the_chain(fresh_hook):
    """Two panel tabs call `setup()`; a hook chained onto itself would log
    the same failure twice and grow with every reload."""
    calls: list[int] = []
    sys.excepthook = lambda t, v, tb: calls.append(1)

    logbook._install_excepthook()
    logbook._install_excepthook()
    logbook._install_excepthook()
    sys.excepthook(*_raise_and_report())

    assert len(calls) == 1


def test_a_broken_logger_does_not_stop_the_previous_hook(fresh_hook, monkeypatch):
    """Failing inside the failure handler is how one bug hides another."""
    seen: list[str] = []
    sys.excepthook = lambda t, v, tb: seen.append(str(v))

    def _explode():
        raise OSError("log file is gone")

    monkeypatch.setattr(logbook, "logger", _explode)
    logbook._install_excepthook()
    sys.excepthook(*_raise_and_report())

    assert seen == ["worker died with nobody watching"]

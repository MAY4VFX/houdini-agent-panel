"""Tests for `orphans.py` — the record-then-sweep half of the crash-orphan
fix (may-hub task, 2026-08-04): `AcpClient.stop()` runs on `aboutToQuit`/
`atexit`, neither of which fires when Houdini is SIGKILLed or crashes
outright, so a spawned agent process can outlive it. Reproduced directly
before writing any of this (see the commit message): a real
`subprocess.Popen`'d child survives its host being `kill -9`'d and is
reparented to PID 1.

`_process_alive`/`_command_line`/`_terminate` are monkeypatched for the
logic tests (the three `sweep()` outcomes) so those don't depend on real
OS process behaviour; the tests at the bottom spawn a REAL process to prove
the POSIX path this project's Houdini actually runs under works end to end,
without mocking anything.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from houdini_agent_panel import orphans


def test_record_started_then_stopped_round_trips():
    orphans.record_started(
        agent_id="claude-acp", pid=12345, command="node", args=["/x/claude-agent-acp"], cwd="/tmp/scene"
    )

    records = orphans._load()
    assert "12345" in records
    record = records["12345"]
    assert record.agent_id == "claude-acp"
    assert record.command == "node"
    assert record.args == ["/x/claude-agent-acp"]
    assert record.cwd == "/tmp/scene"
    assert record.started_at  # a real timestamp, not left blank

    orphans.record_stopped(12345)
    assert orphans._load() == {}


def test_record_stopped_on_an_untracked_pid_is_a_no_op():
    orphans.record_stopped(999)  # never recorded — must not raise
    assert orphans._load() == {}


def test_sweep_with_nothing_recorded_touches_nothing(monkeypatch):
    saved = []
    monkeypatch.setattr(orphans, "_save", lambda records: saved.append(records))

    assert orphans.sweep() == []
    assert saved == []  # not even an empty-file rewrite


def test_sweep_drops_a_record_whose_process_is_already_gone(monkeypatch):
    """The common case on a healthy shutdown that still left a record
    behind for some other reason: nothing to kill, nothing worth keeping."""
    orphans.record_started(agent_id="claude-acp", pid=42, command="node", args=["x"], cwd="/tmp")
    monkeypatch.setattr(orphans, "_process_alive", lambda pid: False)

    cleaned = orphans.sweep()

    assert cleaned == []
    assert orphans._load() == {}


def test_sweep_terminates_and_drops_a_verified_match(monkeypatch):
    orphans.record_started(
        agent_id="claude-acp", pid=42, command="node", args=["/x/claude-agent-acp"], cwd="/tmp/scene"
    )
    monkeypatch.setattr(orphans, "_process_alive", lambda pid: True)
    monkeypatch.setattr(orphans, "_matches", lambda record: True)
    terminated = []
    monkeypatch.setattr(orphans, "_terminate", lambda pid: terminated.append(pid))

    cleaned = orphans.sweep()

    assert terminated == [42]
    assert cleaned == [orphans.SweptAgent(agent_id="claude-acp", pid=42)]
    assert orphans._load() == {}


def test_sweep_leaves_an_unverified_live_process_and_its_record_alone(monkeypatch):
    """The safety-critical case. A live process at the recorded PID that
    does NOT verify (a recycled PID running something else entirely) must
    not be touched — and the record must not silently vanish either, per
    the explicit "never kill by PID alone, never forget something
    suspicious" rule this was built under."""
    orphans.record_started(
        agent_id="claude-acp", pid=42, command="node", args=["/x/claude-agent-acp"], cwd="/tmp/scene"
    )
    monkeypatch.setattr(orphans, "_process_alive", lambda pid: True)
    monkeypatch.setattr(orphans, "_matches", lambda record: False)
    terminated = []
    monkeypatch.setattr(orphans, "_terminate", lambda pid: terminated.append(pid))

    cleaned = orphans.sweep()

    assert cleaned == []
    assert terminated == []
    assert "42" in orphans._load()


def test_sweep_handles_a_mix_of_all_three_outcomes(monkeypatch):
    orphans.record_started(agent_id="gone", pid=1, command="node", args=["a"], cwd="/tmp")
    orphans.record_started(agent_id="ours", pid=2, command="node", args=["b"], cwd="/tmp")
    orphans.record_started(agent_id="unverified", pid=3, command="node", args=["c"], cwd="/tmp")

    def alive(pid: int) -> bool:
        return pid != 1

    def matches(record) -> bool:
        return record.pid == 2

    terminated = []
    monkeypatch.setattr(orphans, "_process_alive", alive)
    monkeypatch.setattr(orphans, "_matches", matches)
    monkeypatch.setattr(orphans, "_terminate", lambda pid: terminated.append(pid))

    cleaned = orphans.sweep()

    assert terminated == [2]
    assert cleaned == [orphans.SweptAgent(agent_id="ours", pid=2)]
    remaining = orphans._load()
    assert set(remaining) == {"3"}


def test_matches_requires_both_command_and_cwd(monkeypatch):
    record = orphans.RunningAgent(
        agent_id="a", pid=1, command="node", args=["/x/claude-agent-acp"], cwd="/tmp/scene"
    )

    monkeypatch.setattr(orphans, "_command_line", lambda pid: ("node /x/claude-agent-acp", "/tmp/scene"))
    assert orphans._matches(record) is True

    monkeypatch.setattr(orphans, "_command_line", lambda pid: ("node /x/claude-agent-acp", "/tmp/elsewhere"))
    assert orphans._matches(record) is False

    monkeypatch.setattr(orphans, "_command_line", lambda pid: ("node /y/some-other-agent", "/tmp/scene"))
    assert orphans._matches(record) is False

    monkeypatch.setattr(orphans, "_command_line", lambda pid: None)
    assert orphans._matches(record) is False


# --- real processes, no mocking — the POSIX path this project ships on ----


def test_process_alive_reflects_a_real_process():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"])
    try:
        assert orphans._process_alive(proc.pid) is True
    finally:
        proc.kill()
        proc.wait(timeout=5)
    assert orphans._process_alive(proc.pid) is False


@pytest.mark.skipif(os.name == "nt", reason="the POSIX path — see the module docstring")
def test_end_to_end_a_real_orphan_is_found_and_terminated():
    """The actual bug, reproduced and then fixed, in one test: spawn a real
    process, record it exactly as `client.py::do_start` would, and confirm
    `sweep()` finds it, verifies it for real (no mocking `_matches` or
    `_command_line` here), and actually kills it."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], cwd="/tmp"
    )
    try:
        orphans.record_started(
            agent_id="claude-acp",
            pid=proc.pid,
            command=sys.executable,
            args=["-c", "import time; time.sleep(30)"],
            cwd="/tmp",
        )

        cleaned = orphans.sweep()

        assert cleaned == [orphans.SweptAgent(agent_id="claude-acp", pid=proc.pid)]
        proc.wait(timeout=5)
        assert proc.poll() is not None
        assert orphans._load() == {}
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)


@pytest.mark.skipif(os.name == "nt", reason="the POSIX path — see the module docstring")
def test_end_to_end_a_real_process_with_the_wrong_cwd_is_left_running():
    """The other half of the same real-process test: same command, wrong
    recorded cwd — must survive `sweep()` untouched."""
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], cwd="/tmp"
    )
    try:
        orphans.record_started(
            agent_id="claude-acp",
            pid=proc.pid,
            command=sys.executable,
            args=["-c", "import time; time.sleep(30)"],
            cwd="/tmp/not-actually-where-it-runs",
        )

        cleaned = orphans.sweep()

        assert cleaned == []
        assert proc.poll() is None  # still alive
        assert str(proc.pid) in orphans._load()
    finally:
        proc.kill()
        proc.wait(timeout=5)

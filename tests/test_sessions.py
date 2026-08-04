"""Tests for `sessions.py` — needs a QApplication (Qt signals), but no network and no Houdini."""

from __future__ import annotations

from houdini_agent_panel import sessions
from houdini_agent_panel.sessions import SessionPool, SessionState


def _state(session_id: str, *, cwd: str = "/tmp/shot") -> SessionState:
    return SessionState(session_id=session_id, title="New conversation", cwd=cwd, created_at=0.0)


def test_add_puts_the_session_in_the_pool(qapp):
    pool = SessionPool()
    pool.add(_state("s1"))
    assert pool.get("s1") is not None


def test_add_emits_added_once_and_changed_on_update(qapp):
    pool = SessionPool()
    added = []
    changed = []
    pool.added.connect(added.append)
    pool.changed.connect(changed.append)

    pool.add(_state("s1"))
    pool.add(_state("s1"))  # same id — an update, not a new session

    assert added == ["s1"]
    assert changed == ["s1"]


def test_all_preserves_insertion_order(qapp):
    pool = SessionPool()
    pool.add(_state("s1"))
    pool.add(_state("s2"))
    pool.add(_state("s3"))

    assert [s.session_id for s in pool.all()] == ["s1", "s2", "s3"]


def test_remove_drops_the_session_and_emits_once(qapp):
    """The pool has no notion of "current" any more (see its docstring) —
    that's a per-tab fact now (`AgentPanel._current_session_id`), covered at
    the panel level in test_ui_panel.py, not here."""
    pool = SessionPool()
    pool.add(_state("s1"))
    pool.add(_state("s2"))
    pool.add(_state("s3"))

    removed = []
    pool.removed.connect(removed.append)

    pool.remove("s2")

    assert removed == ["s2"]
    assert pool.get("s2") is None
    assert [s.session_id for s in pool.all()] == ["s1", "s3"]


def test_remove_last_session_leaves_the_pool_empty(qapp):
    pool = SessionPool()
    pool.add(_state("s1"))
    pool.remove("s1")

    assert pool.all() == []


def test_remove_unknown_id_is_a_noop(qapp):
    pool = SessionPool()
    pool.add(_state("s1"))
    pool.remove("ghost")
    assert pool.all() != []


def test_pool_singleton_returns_same_instance_per_agent_id(qapp):
    sessions.reset_pool_for_tests()
    try:
        first = sessions.pool("claude-acp")
        second = sessions.pool("claude-acp")
        assert first is second
        assert sessions.pool("gemini") is not first
    finally:
        sessions.reset_pool_for_tests()

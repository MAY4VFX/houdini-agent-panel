"""Тесты `sessions.py` — нужен QApplication (сигналы Qt), но не сеть и не Houdini."""

from __future__ import annotations

from houdini_agent_panel import sessions
from houdini_agent_panel.sessions import SessionPool, SessionState


def _state(session_id: str, *, cwd: str = "/tmp/shot") -> SessionState:
    return SessionState(session_id=session_id, title="Новый разговор", cwd=cwd, created_at=0.0)


def test_add_makes_session_current_by_default(qapp):
    pool = SessionPool()
    pool.add(_state("s1"))
    assert pool.current().session_id == "s1"
    assert pool.get("s1") is not None


def test_add_emits_added_once_and_changed_on_update(qapp):
    pool = SessionPool()
    added = []
    changed = []
    pool.added.connect(added.append)
    pool.changed.connect(changed.append)

    pool.add(_state("s1"))
    pool.add(_state("s1"))  # тот же id — обновление, не новая сессия

    assert added == ["s1"]
    assert changed == ["s1"]


def test_all_preserves_insertion_order(qapp):
    pool = SessionPool()
    pool.add(_state("s1"))
    pool.add(_state("s2"))
    pool.add(_state("s3"))

    assert [s.session_id for s in pool.all()] == ["s1", "s2", "s3"]


def test_set_current_emits_signal_and_updates_current(qapp):
    pool = SessionPool()
    pool.add(_state("s1"))
    pool.add(_state("s2"))

    seen = []
    pool.current_changed.connect(seen.append)
    pool.set_current("s2")

    assert pool.current().session_id == "s2"
    assert seen == ["s2"]


def test_set_current_to_same_session_is_a_noop(qapp):
    pool = SessionPool()
    pool.add(_state("s1"))

    seen = []
    pool.current_changed.connect(seen.append)
    pool.set_current("s1")

    assert seen == []


def test_set_current_unknown_id_is_ignored(qapp):
    pool = SessionPool()
    pool.add(_state("s1"))

    pool.set_current("ghost")

    assert pool.current().session_id == "s1"


def test_remove_current_falls_back_to_last_remaining(qapp):
    pool = SessionPool()
    pool.add(_state("s1"))
    pool.add(_state("s2"))
    pool.add(_state("s3"))
    pool.set_current("s2")

    removed = []
    current_changed = []
    pool.removed.connect(removed.append)
    pool.current_changed.connect(current_changed.append)

    pool.remove("s2")

    assert removed == ["s2"]
    assert pool.get("s2") is None
    assert [s.session_id for s in pool.all()] == ["s1", "s3"]
    # текущей стала последняя оставшаяся сессия
    assert pool.current().session_id == "s3"
    assert current_changed == ["s3"]


def test_remove_last_session_leaves_no_current(qapp):
    pool = SessionPool()
    pool.add(_state("s1"))
    pool.remove("s1")

    assert pool.current() is None
    assert pool.all() == []


def test_remove_unknown_id_is_a_noop(qapp):
    pool = SessionPool()
    pool.add(_state("s1"))
    pool.remove("ghost")
    assert pool.all() != []


def test_pool_singleton_returns_same_instance(qapp):
    sessions.reset_pool_for_tests()
    try:
        first = sessions.pool()
        second = sessions.pool()
        assert first is second
    finally:
        sessions.reset_pool_for_tests()

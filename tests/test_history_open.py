"""Opening history must not require sending a message to see its latest turn."""
from __future__ import annotations

import pytest

from houdini_agent_panel import conversations_store as store, sessions
from houdini_agent_panel.ui import panel as panel_mod
from tests.test_session_resume import _stored, _make_widget, _info


@pytest.fixture(autouse=True)
def isolated(qapp, monkeypatch):
    monkeypatch.setattr(panel_mod.scene, 'hip_dir', lambda: '/tmp')
    monkeypatch.setattr(panel_mod.scene, 'mcp_servers', lambda: [])
    monkeypatch.setattr(panel_mod.scene, 'mcp_python_status', lambda: '')
    monkeypatch.setattr(panel_mod._RefreshWorker, 'start', lambda self: None)
    panel_mod.reset_shared_state_for_tests()
    yield
    panel_mod.reset_shared_state_for_tests()


def test_open_fetches_missing_reply_without_a_prompt(qapp, monkeypatch):
    conversation = _stored('History', 'previous question', agent_session_id='saved-session')
    store.save([conversation])
    widget = _make_widget()
    qapp.processEvents()
    client = panel_mod.shared_client('claude-acp')
    client._agent_info = _info()
    client._running = True
    prompts = []

    def replay(**kwargs):
        client.message_chunk.emit('saved-session', 'latest', 'latest saved agent reply')
        client.session_loaded.emit('saved-session', sessions.SessionState(
            session_id='saved-session', title='History', cwd='/tmp', created_at=0.0))

    monkeypatch.setattr(client, 'load_session', replay)
    monkeypatch.setattr(client, 'prompt', lambda *args: prompts.append(args))
    widget._conversations.session_selected.emit(panel_mod._RESTORED_PREFIX + conversation.id)
    qapp.processEvents()
    texts = [e.text for e in widget._transcript._model.entries()]
    widget.shutdown()
    assert 'latest saved agent reply' in texts
    assert 'previous question' in texts
    assert not prompts


def test_unrelated_save_does_not_overwrite_newer_history_from_another_process(qapp):
    conversation = _stored('History', 'previous question', agent_session_id='saved-session')
    store.save([conversation])
    widget = _make_widget()
    qapp.processEvents()

    # Another Houdini process continues the same stored conversation.
    conversation.entries += [
        {'kind': 'user', 'id': 'u2', 'text': 'latest question'},
        {'kind': 'agent', 'id': 'agent:a2', 'text': 'latest answer'},
    ]
    store.save([conversation])
    widget._persist_conversations()
    saved = next(c for c in store.load() if c.id == conversation.id)
    widget.shutdown()
    assert [e['text'] for e in saved.entries] == [
        'previous question', 'latest question', 'latest answer',
    ]


def test_open_refreshes_newer_local_user_and_agent_messages_even_offline(qapp):
    conversation = _stored('History', 'previous question', agent_session_id='saved-session')
    store.save([conversation])
    widget = _make_widget()
    qapp.processEvents()
    conversation.entries += [
        {'kind': 'user', 'id': 'u2', 'text': 'latest question'},
        {'kind': 'agent', 'id': 'agent:a2', 'text': 'latest answer'},
    ]
    store.save([conversation])
    widget._conversations.session_selected.emit(panel_mod._RESTORED_PREFIX + conversation.id)
    qapp.processEvents()
    texts = [e.text for e in widget._transcript._model.entries()]
    widget.shutdown()
    assert texts == ['previous question', 'latest question', 'latest answer']


def _connected_history(qapp, monkeypatch):
    conversation = _stored('History', 'previous question', agent_session_id='saved-session')
    store.save([conversation])
    widget = _make_widget()
    qapp.processEvents()
    client = panel_mod.shared_client('claude-acp')
    client._agent_info = _info()
    client._running = True
    calls = []
    monkeypatch.setattr(client, 'load_session', lambda **kw: calls.append(kw))
    return widget, client, conversation, calls


def test_finishing_history_load_does_not_switch_back_from_another_chat(qapp, monkeypatch):
    widget, client, conversation, calls = _connected_history(qapp, monkeypatch)
    widget._pool.add(sessions.SessionState('other', 'Other', '/tmp', 0.0))
    widget._conversations.session_selected.emit(panel_mod._RESTORED_PREFIX + conversation.id)
    widget._conversations.session_selected.emit('other')
    client.session_loaded.emit('saved-session', sessions.SessionState('saved-session', '', '/tmp', 0.0))
    qapp.processEvents()
    current_id = widget._current_session_id
    widget.shutdown()
    assert current_id == 'other'
    assert len(calls) == 1


def test_history_load_does_not_switch_an_unrelated_sibling_tab(qapp, monkeypatch):
    widget, client, conversation, calls = _connected_history(qapp, monkeypatch)
    other = _make_widget()
    qapp.processEvents()
    widget._pool.add(sessions.SessionState('other', 'Other', '/tmp', 0.0))
    other._set_current_session('other')
    widget._conversations.session_selected.emit(panel_mod._RESTORED_PREFIX + conversation.id)
    client.session_loaded.emit('saved-session', sessions.SessionState('saved-session', '', '/tmp', 0.0))
    qapp.processEvents()
    current_id = other._current_session_id
    widget.shutdown()
    other.shutdown()
    assert current_id == 'other'


def test_failed_browse_keeps_history_without_starting_a_new_conversation(qapp, monkeypatch):
    widget, client, conversation, calls = _connected_history(qapp, monkeypatch)
    new_sessions = []
    monkeypatch.setattr(client, 'new_session', lambda **kw: new_sessions.append(kw))
    key = panel_mod._RESTORED_PREFIX + conversation.id
    widget._conversations.session_selected.emit(key)
    client.session_load_failed.emit('saved-session', 'not found')
    qapp.processEvents()
    texts = [e.text for e in widget._transcript._model.entries()]
    adopting = widget._adopting_restored
    widget.shutdown()
    assert not new_sessions
    assert adopting is None
    assert 'previous question' in texts


def test_two_tabs_share_one_load_and_keep_replay_when_the_other_tab_is_writer(qapp, monkeypatch):
    first, client, conversation, calls = _connected_history(qapp, monkeypatch)
    # Create the second tab without a real running transport (otherwise boot
    # would correctly request a new session before we install the test state).
    client._running = False
    second = _make_widget()
    qapp.processEvents()
    client._running = True
    assert first._writes_shared_state()
    key = panel_mod._RESTORED_PREFIX + conversation.id
    second._conversations.session_selected.emit(key)
    first._conversations.session_selected.emit(key)
    client.message_chunk.emit('saved-session', 'latest', 'latest answer')
    client.session_loaded.emit('saved-session', sessions.SessionState('saved-session', '', '/tmp', 0.0))
    qapp.processEvents()
    shapes = [[e.text for e in tab._transcript._model.entries()] for tab in (first, second)]
    currents = [tab._current_session_id for tab in (first, second)]
    first.shutdown()
    second.shutdown()
    assert len(calls) == 1
    assert currents == ['saved-session', 'saved-session']
    assert shapes == [['previous question', 'latest answer']] * 2


def test_prompt_sent_while_another_history_loads_goes_to_its_own_session(qapp, monkeypatch):
    a = _stored('A', 'question A', agent_session_id='session-a')
    b = _stored('B', 'question B', agent_session_id='session-b')
    store.save([a, b])
    widget = _make_widget()
    qapp.processEvents()
    client = panel_mod.shared_client('claude-acp')
    client._agent_info = _info()
    client._running = True
    loads, prompts = [], []
    monkeypatch.setattr(client, 'load_session', lambda **kw: loads.append(kw['session_id']))
    monkeypatch.setattr(client, 'prompt', lambda sid, blocks: prompts.append((sid, blocks)))
    widget._conversations.session_selected.emit(panel_mod._RESTORED_PREFIX + a.id)
    widget._conversations.session_selected.emit(panel_mod._RESTORED_PREFIX + b.id)
    widget._on_submitted([{'type': 'text', 'text': 'continue B'}])
    client.session_loaded.emit('session-a', sessions.SessionState('session-a', '', '/tmp', 0.0))
    assert not prompts
    client.session_loaded.emit('session-b', sessions.SessionState('session-b', '', '/tmp', 0.0))
    qapp.processEvents()
    widget.shutdown()
    assert loads == ['session-a', 'session-b']
    assert prompts == [('session-b', [{'type': 'text', 'text': 'continue B'}])]


def test_open_does_not_discard_local_unsaved_edits(qapp):
    conversation = _stored('History', 'previous question', agent_session_id='saved-session')
    store.save([conversation])
    widget = _make_widget()
    qapp.processEvents()
    key = panel_mod._RESTORED_PREFIX + conversation.id
    widget._model(key).append_user('local unsaved question')
    widget._conversations.session_selected.emit(key)
    texts = [e.text for e in widget._transcript._model.entries()]
    widget.shutdown()
    assert texts == ['previous question', 'local unsaved question']


def test_browsing_history_does_not_send_saved_queue(qapp, monkeypatch):
    conversation = _stored('History', 'previous question', agent_session_id='saved-session')
    conversation.entries.append({'kind': 'queued', 'id': 'q1', 'text': 'queued work'})
    store.save([conversation])
    widget = _make_widget()
    qapp.processEvents()
    client = panel_mod.shared_client('claude-acp')
    client._agent_info = _info()
    client._running = True
    monkeypatch.setattr(client, 'load_session', lambda **kw: None)
    prompts = []
    monkeypatch.setattr(client, 'prompt', lambda *args: prompts.append(args))
    widget._conversations.session_selected.emit(panel_mod._RESTORED_PREFIX + conversation.id)
    client.session_loaded.emit('saved-session', sessions.SessionState('saved-session', '', '/tmp', 0.0))
    qapp.processEvents()
    widget.shutdown()
    assert not prompts


def test_late_replay_is_saved_without_sending_a_new_message(qapp, monkeypatch):
    widget, client, conversation, calls = _connected_history(qapp, monkeypatch)
    widget._conversations.session_selected.emit(panel_mod._RESTORED_PREFIX + conversation.id)
    client.session_loaded.emit('saved-session', sessions.SessionState('saved-session', '', '/tmp', 0.0))
    widget._persist_conversations()
    widget._end_persist_cooldown()
    client.message_chunk.emit('saved-session', 'late', 'late reply')
    # Fire the real coalesced save without sleeping for its wall-clock delay.
    widget._end_persist_cooldown()
    saved = next(c for c in store.load() if c.id == conversation.id)
    widget.shutdown()
    assert 'late reply' in [e['text'] for e in saved.entries]


@pytest.mark.parametrize('action', ['disconnect', 'switch_agent'])
def test_interrupted_history_load_does_not_block_future_loads(qapp, monkeypatch, action):
    widget, client, conversation, calls = _connected_history(qapp, monkeypatch)
    widget._conversations.session_selected.emit(panel_mod._RESTORED_PREFIX + conversation.id)
    if action == 'disconnect':
        widget._on_disconnected('connection lost')
    else:
        widget._rejoin_agent('codex-acp')
    pending_load = widget._loading_session_id
    adopting = widget._adopting_restored
    widget.shutdown()
    assert pending_load is None
    assert adopting is None

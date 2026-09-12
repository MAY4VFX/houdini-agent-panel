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


@pytest.mark.parametrize('completion', ['load', 'new_session_fallback'])
def test_resuming_old_history_keeps_its_position_in_the_drawer(qapp, monkeypatch, completion):
    old = _stored('Older chat', 'old question', agent_session_id='old-session')
    old.created_at = old.updated_at = 100.0
    newer = _stored('Newer chat', 'new question', agent_session_id='newer-session')
    newer.created_at = newer.updated_at = 200.0
    store.save([old, newer])
    widget = _make_widget()
    qapp.processEvents()
    client = panel_mod.shared_client('claude-acp')
    client._agent_info = _info(supports_load_session=completion == 'load')
    client._running = True
    monkeypatch.setattr(client, 'load_session', lambda **kw: None)
    monkeypatch.setattr(client, 'new_session', lambda **kw: None)
    key = panel_mod._RESTORED_PREFIX + old.id
    before = [button.toolTip() for button in widget._conversations._buttons.values()]
    widget._conversations._buttons[key].click()
    if completion == 'new_session_fallback':
        # The same identity transplant is used when an agent cannot resume.
        widget._adopt_or_resume(key)
    resumed = sessions.SessionState('old-session', 'New chat', '/tmp', 1000.0)
    signal = client.session_loaded if completion == 'load' else client.session_started
    signal.emit('old-session', resumed)
    qapp.processEvents()
    after = [button.toolTip() for button in widget._conversations._buttons.values()]
    widget.shutdown()
    assert before == ['Newer chat', 'Older chat']
    assert after == before, 'opening history must not make it a newly created conversation'


@pytest.mark.parametrize('reading_back', [False, True])
def test_delayed_resume_keeps_the_visible_message_in_place(qapp, monkeypatch, reading_back):
    conversation = _stored('History', 'question', agent_session_id='saved-session')
    conversation.entries = [
        {'kind': 'agent', 'id': f'agent:m{i}', 'text': f'Reply {i}\n' + 'some prose ' * 30}
        for i in range(35)
    ]
    store.save([conversation])
    widget = _make_widget()
    qapp.processEvents()
    widget.resize(640, 640)
    widget.show()
    client = panel_mod.shared_client('claude-acp')
    client._agent_info = _info()
    client._running = True
    monkeypatch.setattr(client, 'load_session', lambda **kw: None)
    key = panel_mod._RESTORED_PREFIX + conversation.id
    widget._conversations.session_selected.emit(key)
    for _ in range(5):
        qapp.processEvents()
    view = widget._transcript
    bar = view.verticalScrollBar()
    assert bar.maximum() > 0
    bar.setValue(bar.maximum() // 2 if reading_back else bar.maximum())
    for _ in range(3):
        qapp.processEvents()
    before = bar.value()
    before_max = bar.maximum()
    # A later event-loop turn, after the user has already started reading.
    client.session_loaded.emit('saved-session', sessions.SessionState('saved-session', '', '/tmp', 1000))
    for _ in range(5):
        qapp.processEvents()
    after = bar.value()
    after_max = bar.maximum()
    current_conversation = widget._conversation_ids[widget._current_session_id]
    widget.shutdown()
    assert current_conversation == conversation.id
    assert abs(after - before) <= 4, (before, after, before_max, after_max)


def test_replayed_events_do_not_pull_the_reader_below_the_saved_reply(qapp, monkeypatch):
    from types import SimpleNamespace
    from houdini_agent_panel.ui.qt import QtCore

    widget, client, conversation, calls = _connected_history(qapp, monkeypatch)
    key = panel_mod._RESTORED_PREFIX + conversation.id
    model = widget._model(key)
    for i in range(20):
        model.apply_chunk(f'm{i}', f'Saved reply {i}: ' + 'long answer ' * 25)
    widget.resize(640, 640)
    widget.show()
    widget._conversations.session_selected.emit(key)
    for _ in range(5):
        qapp.processEvents()
    view = widget._transcript
    last_row = view._rows['agent:m19']
    before = last_row.mapTo(view.viewport(), QtCore.QPoint(0, 0)).y()
    for i in range(8):
        client.tool_call.emit('saved-session', SimpleNamespace(
            tool_call_id=f'historical-tool-{i}', title=f'Earlier tool {i}',
            kind='read', status='completed', content=[], locations=[],
        ))
    client.session_loaded.emit('saved-session', sessions.SessionState('saved-session', '', '/tmp', 0))
    for _ in range(5):
        qapp.processEvents()
    after = view._rows['agent:m19'].mapTo(view.viewport(), QtCore.QPoint(0, 0)).y()
    widget.shutdown()
    assert abs(after - before) <= 4, (before, after)


def test_history_refresh_has_an_explicit_status_without_moving_the_feed(qapp, monkeypatch):
    widget, client, conversation, calls = _connected_history(qapp, monkeypatch)
    widget._conversations.session_selected.emit(panel_mod._RESTORED_PREFIX + conversation.id)
    view = widget._transcript
    assert view.history_status_text() == 'Showing saved history · refreshing…'
    client.session_loaded.emit('saved-session', sessions.SessionState('saved-session', '', '/tmp', 0))
    assert view.history_status_text() == 'History refreshed'
    widget.shutdown()


def test_refresh_preserves_the_reading_anchor_when_an_earlier_reply_grows(qapp, monkeypatch):
    from houdini_agent_panel.ui.qt import QtCore, QtWidgets

    widget, client, conversation, calls = _connected_history(qapp, monkeypatch)
    key = panel_mod._RESTORED_PREFIX + conversation.id
    for i in range(30):
        widget._model(key).apply_chunk(f'm{i}', f'Reply {i}: ' + 'saved text ' * 30)
    widget.resize(640, 640)
    widget.show()
    widget._conversations.session_selected.emit(key)
    for _ in range(5):
        qapp.processEvents()
    view = widget._transcript
    bar = view.verticalScrollBar()
    bar.setValue(bar.maximum() // 2)
    row_id, row = next((key, row) for key, row in view._rows.items()
                       if row.y() + row.height() > bar.value())
    before = row.mapTo(view.viewport(), QtCore.QPoint(0, 0)).y()
    paint_positions = []
    prose = row.findChild(QtWidgets.QTextBrowser)

    class PaintSpy(QtCore.QObject):
        def eventFilter(self, obj, event):
            if event.type() == QtCore.QEvent.Paint and obj in (row, prose.viewport(), view.viewport()):
                paint_positions.append(row.mapTo(view.viewport(), QtCore.QPoint()).y())
            return False

    spy = PaintSpy()
    qapp.installEventFilter(spy)
    old_text = widget._model(key).chunk_entry('m0').text
    client.message_chunk.emit('saved-session', 'm0', old_text + '\n' + 'extra recovered text ' * 300)
    client.session_loaded.emit('saved-session', sessions.SessionState('saved-session', '', '/tmp', 0))
    for _ in range(8):
        qapp.processEvents()
    qapp.removeEventFilter(spy)
    after = view._rows[row_id].mapTo(view.viewport(), QtCore.QPoint(0, 0)).y()
    widget.shutdown()
    assert abs(before - after) <= 4, (row_id, before, after)
    assert paint_positions, "the test must observe actual painted frames"
    assert all(abs(y - before) <= 4 for y in paint_positions), (before, paint_positions)


def test_refresh_does_not_clear_a_text_selection(qapp, monkeypatch):
    from houdini_agent_panel.ui.qt import QtGui, QtWidgets

    widget, client, conversation, calls = _connected_history(qapp, monkeypatch)
    widget._conversations.session_selected.emit(panel_mod._RESTORED_PREFIX + conversation.id)
    prose = widget._transcript.findChild(QtWidgets.QTextBrowser)
    cursor = prose.textCursor()
    cursor.setPosition(0)
    cursor.setPosition(8, QtGui.QTextCursor.KeepAnchor)
    prose.setTextCursor(cursor)
    selected = prose.textCursor().selectedText()
    assert selected
    client.session_loaded.emit('saved-session', sessions.SessionState('saved-session', '', '/tmp', 0))
    assert widget._transcript.findChild(QtWidgets.QTextBrowser) is prose
    assert prose.textCursor().selectedText() == selected
    widget.shutdown()


def test_leaving_the_conversation_clears_its_refresh_status(qapp, monkeypatch):
    widget, client, conversation, calls = _connected_history(qapp, monkeypatch)
    widget._pool.add(sessions.SessionState('other', 'Other', '/tmp', 0))
    widget._conversations.session_selected.emit(panel_mod._RESTORED_PREFIX + conversation.id)
    widget._conversations.session_selected.emit('other')
    client.session_loaded.emit('saved-session', sessions.SessionState('saved-session', '', '/tmp', 0))
    assert widget._current_session_id == 'other'
    assert widget._transcript.history_status_text() == ''
    widget.shutdown()


def test_disconnect_clears_the_refresh_in_progress_status(qapp, monkeypatch):
    widget, client, conversation, calls = _connected_history(qapp, monkeypatch)
    widget._conversations.session_selected.emit(panel_mod._RESTORED_PREFIX + conversation.id)
    widget._on_disconnected('lost connection')
    text = widget._transcript.history_status_text()
    widget.shutdown()
    assert text == 'Showing saved history · agent disconnected'

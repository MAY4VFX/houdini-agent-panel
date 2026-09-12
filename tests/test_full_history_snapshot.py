from types import SimpleNamespace as NS

from houdini_agent_panel.transcript_model import TranscriptModel


def test_saved_tail_keeps_thought_and_tool_rows_in_order():
    model = TranscriptModel()
    model.append_user('question')
    model.apply_chunk('thought', 'Checking the scene', thought=True)
    model.apply_tool_call(NS(tool_call_id='tool', title='Read scene', kind='read',
                             status='completed', content=[], locations=[]))
    model.apply_chunk('answer', 'Final answer')
    restored = TranscriptModel()
    restored.load_records(model.to_records())
    assert [(e.kind, e.id, e.text) for e in restored.entries()] == [
        (e.kind, e.id, e.text) for e in model.entries()
    ]
    assert restored.entries()[2].tool.title == 'Read scene'


def _text(kind, mid, text):
    return NS(session_update=kind, message_id=mid, content=NS(type='text', text=text))


def test_replay_restores_true_chronology_instead_of_appending_old_tools_after_answer():
    model = TranscriptModel()
    user = model.append_user('question')
    model.apply_chunk('answer', 'Final answer')
    updates = [
        _text('user_message_chunk', 'u1', 'question'),
        _text('agent_thought_chunk', 't1', 'Checking scene'),
        NS(session_update='tool_call', tool_call_id='tool', title='Read scene',
           kind='read', status='completed', content=[], locations=[]),
        _text('agent_message_chunk', 'answer', 'Final '),
        _text('agent_message_chunk', 'answer', 'answer'),
    ]
    model.replace_from_replay(updates)
    assert [e.kind for e in model.entries()] == ['user', 'thought', 'tool', 'agent']
    assert model.entries()[0].id == user.id
    assert model.entries()[-1].text == 'Final answer'
    model.replace_from_replay(updates)
    assert len(model.entries()) == 4, 'reopening must not duplicate the tail'
    restored = TranscriptModel()
    restored.load_records(model.to_records())
    assert [e.kind for e in restored.entries()] == ['user', 'thought', 'tool', 'agent']


def test_replay_retains_a_local_user_message_the_agent_did_not_record():
    model = TranscriptModel()
    model.append_user('question')
    model.apply_chunk('answer', 'answer')
    model.append_user('unsaved at the agent')
    model.replace_from_replay([
        _text('user_message_chunk', 'u1', 'question'),
        _text('agent_message_chunk', 'answer', 'answer'),
    ])
    assert [e.text for e in model.entries()] == ['question', 'answer', 'unsaved at the agent']


def test_zero_duration_activity_remains_zero_after_restart():
    model = TranscriptModel()
    model.load_records([{'kind': 'activity', 'id': 'a', 'text': '', 'elapsed': 0.0}])
    assert model.to_records()[0]['elapsed'] == 0.0


def test_replay_of_an_old_identical_prompt_does_not_acknowledge_a_new_queued_message():
    model = TranscriptModel()
    old = model.append_user('again')
    model.apply_chunk('answer', 'done')
    model.queue_message('pending', 'again')
    model.replace_from_replay([
        _text('user_message_chunk', 'old-user-id', 'again'),
        _text('agent_message_chunk', 'answer', 'done'),
    ])
    assert [(e.id, e.kind) for e in model.entries()] == [
        (old.id, 'user'), ('agent:answer', 'agent'), ('pending', 'queued')]

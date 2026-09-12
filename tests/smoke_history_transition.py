"""Native Houdini history checks: full-cache tail, legacy first load and reading.

Run with hython. All data is temporary; the agent is simulated and no model/API
request is made. Paint events catch intermediate frames that final-position
assertions miss on Houdini's Qt. --source tests the checkout before packaging.
"""
import os
import tempfile
os.environ['HAP_DATA_DIR'] = tempfile.mkdtemp(prefix='hap-history-native-')
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace as NS

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', action='store_true')
parser.add_argument('--expected-version')
parser.add_argument('--mode', choices=['all', 'warm', 'cold', 'reading'], default='all')
args = parser.parse_args()
if args.source:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
import hou
import houdini_agent_panel
from houdini_agent_panel import client as client_mod, conversations_store as store, sessions
from houdini_agent_panel.transcript_model import TranscriptModel
from houdini_agent_panel.ui import panel as panel_mod
from houdini_agent_panel.ui.qt import QtCore, QtGui, QtWidgets

if args.expected_version:
    assert houdini_agent_panel.__version__ == args.expected_version
panel_mod.AgentPanel._boot = lambda self: None
panel_mod.scene.mcp_servers = lambda: []
panel_mod.scene.mcp_python_status = lambda: ''
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def text_update(kind, mid, text):
    return NS(session_update=kind, message_id=mid, content=NS(type='text', text=text))


def run(mode):
    root = tempfile.mkdtemp(prefix='hap-history-' + mode + '-')
    os.environ['HAP_DATA_DIR'] = root
    panel_mod.scene.hip_dir = lambda: root
    updates = [text_update('user_message_chunk', 'u1', 'question')]
    updates += [text_update('agent_message_chunk', f'm{i}', f'Reply {i}: ' + 'Saved text. ' * 40)
                for i in range(35)]
    updates += [text_update('agent_thought_chunk', 'thought', 'Checking the scene')]
    updates += [NS(session_update='tool_call', tool_call_id=f't{i}', title=f'Inspect scene {i}',
                   kind='read', status='completed', content=[], locations=[]) for i in range(8)]
    updates += [text_update('agent_message_chunk', 'final', 'Final answer')]
    cache = TranscriptModel()
    cache.replace_from_replay(updates)
    old = store.StoredConversation.new('History', 'claude-acp', root)
    old.agent_session_id = 'old-session'
    old.created_at = old.updated_at = 100
    old.entries = cache.to_records()
    old.transcript_version = 2
    if mode == 'cold':
        old.entries = [e for e in old.entries if e['kind'] in ('user', 'agent')]
        old.transcript_version = 1
    newer = store.StoredConversation.new('Newer chat', 'claude-acp', root)
    newer.created_at = newer.updated_at = 200
    newer.entries = [{'kind': 'user', 'id': 'other', 'text': 'Another conversation'}]
    store.save([old, newer])
    widget = panel_mod.AgentPanel()
    widget._rejoin_agent('claude-acp')
    client = panel_mod.shared_client('claude-acp')
    client._running = True
    client._agent_info = client_mod.AgentInfo(name='smoke', version='1', protocol_version=1,
        supports_image=False, supports_audio=False, supports_embedded_context=False,
        supports_load_session=True, supports_logout=False, auth_methods=())
    client.load_session = lambda **kw: None
    client.prompt = lambda *a: (_ for _ in ()).throw(AssertionError('unexpected prompt'))
    widget.resize(800, 800)
    widget.show()
    widget._conversations.session_selected.emit(panel_mod._RESTORED_PREFIX + old.id)
    for _ in range(8):
        app.processEvents()
    view = widget._transcript
    bar = view.verticalScrollBar()
    if mode == 'cold':
        assert view.is_history_loading() and not view._content.isVisible()
    else:
        assert view._content.isVisible() and bar.value() == bar.maximum()
        assert view._model.entries()[-1].text == 'Final answer'
        assert len(view._model.entries()) == len(cache.entries())
    if mode == 'reading':
        bar.setValue(bar.maximum() // 2)
    row_id, row = next((key, row) for key, row in view._rows.items()
                       if row.y() + row.height() > bar.value())
    prose = row.findChild(QtWidgets.QTextBrowser)
    if mode == 'reading':
        cursor = prose.textCursor()
        cursor.setPosition(0)
        cursor.setPosition(10, QtGui.QTextCursor.KeepAnchor)
        prose.setTextCursor(cursor)
        selection = cursor.selectedText()
        updates[1] = text_update('agent_message_chunk', 'm0', updates[1].content.text + 'Recovered text. ' * 200)
    before = row.mapTo(view.viewport(), QtCore.QPoint()).y()
    frames, active = [], [False]

    class PaintSpy(QtCore.QObject):
        def eventFilter(self, obj, event):
            if (active[0] and event.type() == QtCore.QEvent.Paint and view._content.isVisible()
                    and isinstance(obj, QtWidgets.QWidget)
                    and (obj is view.viewport() or view.viewport().isAncestorOf(obj))):
                frames.append(row.mapTo(view.viewport(), QtCore.QPoint()).y() if mode == 'reading'
                              else (bar.value(), bar.maximum()))
            return False

    spy = PaintSpy()
    app.installEventFilter(spy)
    widget.grab().save(str(Path(root) / 'before.png'))
    loop = QtCore.QEventLoop()

    def finish():
        active[0] = True
        client.session_loaded.emit('old-session', sessions.SessionState(
            'old-session', '', root, 1000, replay_updates=updates))
        loop.quit()

    QtCore.QTimer.singleShot(1000, finish)
    loop.exec() if hasattr(loop, 'exec') else loop.exec_()
    for _ in range(12):
        app.processEvents()
    app.removeEventFilter(spy)
    assert frames, 'no painted frames observed'
    if mode == 'reading':
        assert all(abs(y - before) <= 4 for y in frames), (before, frames)
        assert view._rows[row_id] is row and prose.textCursor().selectedText() == selection
    else:
        assert all(abs(pos - maximum) <= 4 for pos, maximum in frames), frames
    assert widget._conversation_ids[widget._current_session_id] == old.id
    assert [b.toolTip() for b in widget._conversations._buttons.values()] == ['Newer chat', 'History']
    assert view._model.entries()[-1].text == 'Final answer'
    assert len(view._model.entries()) == len(cache.entries())
    assert not view.is_history_loading()
    widget.grab().save(str(Path(root) / 'after.png'))
    widget._persist_conversations()
    assert next(c for c in store.load() if c.id == old.id).transcript_version == 2
    print(json.dumps({'mode': mode, 'houdini': hou.applicationVersionString(),
        'panel': houdini_agent_panel.__version__, 'module': houdini_agent_panel.__file__,
        'frames': len(frames), 'positions': list(dict.fromkeys(frames)),
        'tail_and_identity': 'PASS', 'screenshots': root}), flush=True)
    widget.shutdown()
    panel_mod.reset_shared_state_for_tests()
    widget.close()
    widget.deleteLater()
    app.processEvents()


for mode in ['warm', 'cold', 'reading'] if args.mode == 'all' else [args.mode]:
    run(mode)

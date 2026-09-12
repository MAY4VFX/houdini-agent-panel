"""Run with hython; isolated data and a simulated delayed agent, no API calls.

Unlike a final-position assertion, observing Paint events catches a single
wrong intermediate frame. Houdini's Qt 6.5/6.8 exposed this while the stock
PySide6 pytest environment did not. Screenshots go into the temporary data root.
"""
import os, tempfile
os.environ['HAP_DATA_DIR'] = tempfile.mkdtemp(prefix='hap-transition-')
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import sys, json, argparse
from pathlib import Path
parser = argparse.ArgumentParser(description="Native Houdini history-transition smoke test (no real agent)")
parser.add_argument('--source', action='store_true', help='Load this checkout instead of the installed panel')
parser.add_argument('--expected-version')
args = parser.parse_args()
if args.source:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
import hou
import houdini_agent_panel
from houdini_agent_panel import client as client_mod, conversations_store as store, sessions
from houdini_agent_panel.ui import panel as panel_mod
from houdini_agent_panel.ui.qt import QtCore, QtGui, QtWidgets
from types import SimpleNamespace

root = os.environ['HAP_DATA_DIR']
if args.expected_version:
    assert houdini_agent_panel.__version__ == args.expected_version
panel_mod.scene.hip_dir = lambda: root
panel_mod.scene.mcp_servers = lambda: []
panel_mod.scene.mcp_python_status = lambda: ''
panel_mod.AgentPanel._boot = lambda self: None
old = store.StoredConversation.new('History', 'claude-acp', root)
old.agent_session_id = 'old-session'
old.created_at = old.updated_at = 100
old.entries = [{'kind': 'agent', 'id': f'agent:m{i}', 'text': f'Reply {i}: ' + 'Saved conversation text. ' * 20} for i in range(35)]
newer = store.StoredConversation.new('Newer chat', 'claude-acp', root)
newer.created_at = newer.updated_at = 200
newer.entries = [{'kind': 'user', 'id': 'u1', 'text': 'Another conversation'}]
store.save([old, newer])
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
widget = panel_mod.AgentPanel()
widget._rejoin_agent('claude-acp')
client = panel_mod.shared_client('claude-acp')
client._running = True
client._agent_info = client_mod.AgentInfo(name='smoke', version='1', protocol_version=1,
    supports_image=False, supports_audio=False, supports_embedded_context=False,
    supports_load_session=True, supports_logout=False, auth_methods=())
client.load_session = lambda **kw: None
client.prompt = lambda *args: (_ for _ in ()).throw(AssertionError('unexpected prompt'))
widget.resize(800, 800)
widget.show()
widget._conversations.session_selected.emit(panel_mod._RESTORED_PREFIX + old.id)
for _ in range(6):
    app.processEvents()
view = widget._transcript
bar = view.verticalScrollBar()
bar.setValue(bar.maximum() // 2)
row_id, row = next((key, row) for key, row in view._rows.items() if row.y() + row.height() > bar.value())
prose = row.findChild(QtWidgets.QTextBrowser)
cursor = prose.textCursor()
cursor.setPosition(0)
cursor.setPosition(10, QtGui.QTextCursor.KeepAnchor)
prose.setTextCursor(cursor)
selection = cursor.selectedText()
before = row.mapTo(view.viewport(), QtCore.QPoint()).y()
assert view.history_status_text() == 'Showing saved history · refreshing…'
image_root = str(Path(root) / ('transition-' + hou.applicationVersionString()))
widget.grab().save(image_root + '-before.png')

paint_positions = []
painting_replay = [False]
class PaintSpy(QtCore.QObject):
    def eventFilter(self, obj, event):
        if (painting_replay[0] and event.type() == QtCore.QEvent.Paint
                and obj in (row, prose.viewport(), view.viewport())):
            paint_positions.append(row.mapTo(view.viewport(), QtCore.QPoint()).y())
        return False
spy = PaintSpy()
app.installEventFilter(spy)
# A real Qt timer lets the saved transcript stay visible before the load resolves.
def finish():
    painting_replay[0] = True
    first = widget._model(panel_mod._RESTORED_PREFIX + old.id).chunk_entry('m0').text
    client.message_chunk.emit('old-session', 'm0', first + '\n' + 'Recovered earlier text. ' * 150)
    for i in range(8):
        client.tool_call.emit('old-session', SimpleNamespace(tool_call_id=f't{i}', title=f'Earlier tool {i}',
            kind='read', status='completed', content=[], locations=[]))
    client.message_chunk.emit('old-session', 'latest', 'Latest recovered reply')
    client.session_loaded.emit('old-session', sessions.SessionState('old-session', '', root, 1000))
    loop.quit()

loop = QtCore.QEventLoop()
QtCore.QTimer.singleShot(1000, finish)
loop.exec() if hasattr(loop, 'exec') else loop.exec_()
for _ in range(10):
    app.processEvents()
app.removeEventFilter(spy)
after = view._rows[row_id].mapTo(view.viewport(), QtCore.QPoint()).y()
assert paint_positions, 'no transition frames observed'
assert all(abs(y - before) <= 4 for y in paint_positions), paint_positions
assert abs(after - before) <= 4, (before, after)
assert view._rows[row_id] is row
assert prose.textCursor().selectedText() == selection
assert widget._conversation_ids[widget._current_session_id] == old.id
assert [b.toolTip() for b in widget._conversations._buttons.values()] == ['Newer chat', 'History']
assert view.history_status_text() == 'History refreshed'
assert any(e.text == 'Latest recovered reply' for e in view._model.entries())
widget.grab().save(image_root + '-after.png')
print(json.dumps({'houdini': hou.applicationVersionString(), 'panel': houdini_agent_panel.__version__,
    'module': houdini_agent_panel.__file__, 'anchor': [before, after], 'paint_positions': paint_positions, 'selection': 'PASS',
    'conversation_identity': 'PASS', 'drawer_order': 'PASS', 'latest_reply': 'PASS',
    'screenshots': image_root}, ensure_ascii=False), flush=True)
widget.shutdown()
panel_mod.reset_shared_state_for_tests()
widget.close()

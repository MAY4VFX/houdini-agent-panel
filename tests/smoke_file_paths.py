"""Run with hython; --source tests the checkout. No file manager is launched."""
import os, tempfile
os.environ['HAP_DATA_DIR'] = tempfile.mkdtemp(prefix='hap-path-click-')
os.environ['QT_QPA_PLATFORM'] = 'offscreen'
import sys
from pathlib import Path
if '--source' in sys.argv:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
from houdini_agent_panel import __version__
from houdini_agent_panel.ui import file_paths
from houdini_agent_panel.ui.qt import QtCore, QtGui, QtWidgets
from houdini_agent_panel.ui.transcript import _ProseBlock, _CodeBlock
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
root = Path(os.environ['HAP_DATA_DIR'])
file = root / 'my render.mov'
file.touch()
opened = []
file_paths.reveal = opened.append
for cls in [_ProseBlock, _CodeBlock]:
    widget = cls() if cls is _ProseBlock else cls(str(file))
    if cls is _ProseBlock:
        widget.set_text(f'Готово 🎬: `{file}` — mp4')
    widget.resize(1200, 160)
    widget.show()
    app.processEvents()
    plain = widget.toPlainText()
    cursor = widget.textCursor()
    offset = plain.index('render') + 2
    cursor.setPosition(len(plain[:offset].encode('utf-16-le')) // 2)
    point = widget.cursorRect(cursor).center()
    for modifier in [QtCore.Qt.NoModifier, QtCore.Qt.ControlModifier]:
        event = QtGui.QMouseEvent(QtCore.QEvent.MouseButtonRelease, QtCore.QPointF(point),
            QtCore.Qt.LeftButton, QtCore.Qt.NoButton, modifier)
        before = len(opened)
        app.sendEvent(widget.viewport(), event)
        assert len(opened) == before + (modifier == QtCore.Qt.ControlModifier), (cls, plain)
    assert opened[-1] == file
    widget.close()
print('PASS', __version__, QtCore.qVersion(), 'prose + code; Cmd-click; spaces and emoji; ordinary click unchanged')

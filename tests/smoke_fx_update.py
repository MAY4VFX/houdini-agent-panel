"""Native update-button smoke with real uv/pip, isolated prefs and dependencies.

Run with hython --wheel <GitHub-built wheel>. The wrapper substitutes that
unpublished wheel for the worker's panel version pin and directs installation
into temporary prefs/data. No real agent or user's package file is touched.
"""
import os
import tempfile
os.environ['HAP_DATA_DIR'] = tempfile.mkdtemp(prefix='hap-fx-update-')
os.environ['QT_QPA_PLATFORM'] = 'offscreen'

import argparse
import json
import shutil
import sys
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--wheel', required=True, type=Path)
parser.add_argument('--fx-version', default='2.14.2')
args = parser.parse_args()
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'python'))
from houdini_agent_panel import deps, paths, mcp_runtime
from houdini_agent_panel.ui import panel as panel_mod, self_update
from houdini_agent_panel.ui.qt import QtCore, QtWidgets
from houdini_agent_panel.updates import Update

root = Path(os.environ['HAP_DATA_DIR'])
prefs = root / 'prefs' / 'packages'
prefs.mkdir(parents=True)
uvx = shutil.which('uvx') or str(Path.home() / '.local/bin/uvx')
# Use the stock CPython shipped next to hython, not a second Houdini process.
plain_python = next(path for path in mcp_runtime.plain_python_candidates(
    Path(sys.executable), sys.version_info[:2]) if path.exists())
wrapper = root / 'uvx-test'
wrapper.write_text(f'''#!{plain_python}
import os, sys
argv = sys.argv[1:]
assert argv[argv.index('--from') + 1].startswith('houdini-agent-panel=='), argv
argv[argv.index('--from') + 1] = {str(args.wheel.resolve())!r}
argv += ['--houdini-dir', {str(prefs)!r}, '--find-links', {str(args.wheel.resolve().parent)!r}]
os.execv({uvx!r}, [{uvx!r}] + argv)
''')
wrapper.chmod(0o755)
self_update.which = lambda *a, **k: str(wrapper)
app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
panel_mod.scene.hip_dir = lambda: str(root)
panel_mod.AgentPanel._boot = lambda self: None
widget = panel_mod.AgentPanel()
widget.show()
widget._show_page(widget.PAGE_SETTINGS)
view = widget._settings_view
update = Update('fx', 'fxhoudinimcp', 'MCP upstream', '2.10.0', args.fx_version)
view._on_check_now_done({'fx': update})
view._fx_update_button.click()
worker = widget._panel_update_worker
assert worker is not None
assert 'updating to' in view._fx_version_label.text()
assert not view._fx_update_button.isVisible()
worker.progressed.connect(lambda line: print(line, flush=True))
loop = QtCore.QEventLoop()
worker.finished.connect(loop.quit)
QtCore.QTimer.singleShot(660000, loop.quit)
loop.exec()
assert worker.wait(1000), 'installer did not finish'
app.processEvents()
assert widget._panel_update_restart_pending == update, view._fx_version_label.text()
assert not view._fx_update_button.isVisible()
assert 'restart Houdini' in view._fx_version_label.text()
view._on_check_now_done({'fx': update})
assert not view._fx_update_button.isVisible(), 'stale check restored Update button'
target = paths.deps_dir()
assert deps.installed_version(target, 'fxhoudinimcp') == args.fx_version
payload = json.loads((prefs / 'houdini_agent_panel.json').read_text())
assert str(root) in str(payload)
widget.shutdown()
print(json.dumps({'result': 'PASS', 'fx_version': args.fx_version, 'deps': str(target),
                  'settings': view._fx_version_label.text()}), flush=True)

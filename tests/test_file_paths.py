from houdini_agent_panel.ui.file_paths import path_at


def test_absolute_inline_path_with_spaces(tmp_path):
    file = tmp_path / 'my render.mov'
    file.touch()
    text = f'Готово: {file} — h264'
    assert path_at(text, text.index('render')) == file
    assert path_at(text, text.index('h264')) is None


def test_relative_path_and_line_number(tmp_path):
    file = tmp_path / 'scripts' / 'render.py'
    file.parent.mkdir()
    file.touch()
    assert path_at('scripts/render.py:12', 10, str(tmp_path)) == file
    assert path_at('https://example.org/file.mov', 15, str(tmp_path)) is None


def test_cmd_click_inline_code_reveals_file(qapp, tmp_path, monkeypatch):
    from houdini_agent_panel.ui import file_paths
    from houdini_agent_panel.ui.transcript import _ProseBlock
    from houdini_agent_panel.ui.qt import QtCore, QtGui
    from PySide6.QtTest import QTest
    file = tmp_path / 'render.mov'
    file.touch()
    widget = _ProseBlock()
    widget.resize(900, 100)
    widget.set_text(f'Готово: `{file}`')
    widget.show()
    qapp.processEvents()
    cursor = widget.textCursor()
    cursor.setPosition(widget.toPlainText().index('render.mov') + 3)
    point = widget.cursorRect(cursor).center()
    opened = []
    monkeypatch.setattr(file_paths, 'reveal', opened.append)
    QTest.mouseClick(widget.viewport(), QtCore.Qt.LeftButton, QtCore.Qt.NoModifier, point)
    assert opened == []
    QTest.mouseClick(widget.viewport(), QtCore.Qt.LeftButton, QtCore.Qt.ControlModifier, point)
    assert opened == [file]


def test_reveal_selects_file_in_finder_without_launching_it(tmp_path, monkeypatch):
    from houdini_agent_panel.ui import file_paths
    from types import SimpleNamespace
    file = tmp_path / 'preview.mov'
    file.touch()
    calls = []
    monkeypatch.setattr(file_paths.sys, 'platform', 'darwin')
    monkeypatch.setattr(file_paths.childproc, 'run', lambda argv, **kw: calls.append(argv))
    monkeypatch.setattr(file_paths.threading, 'Thread', lambda target, **kw: SimpleNamespace(start=target))
    file_paths.reveal(file)
    file_paths.reveal(tmp_path)
    assert calls == [['open', '-R', str(file)], ['open', str(tmp_path)]]

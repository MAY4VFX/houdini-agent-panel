"""Resolve paths only on an explicit modified click, never while streaming."""
from pathlib import Path
import re
import sys
import threading

from .. import childproc
from .qt import QtCore

_START = re.compile(r"(?<![\w:/])(?:~/|/|[A-Za-z]:[\\/]|(?:\.{1,2}/)?[\w.-]+/)")


def path_at(text: str, position: int, base: str = "") -> Path | None:
    if any(match.start() <= position < match.end()
           for match in re.finditer(r"[A-Za-z][A-Za-z0-9+.-]*://\S+", text)):
        return None
    for match in _START.finditer(text):
        start = match.start()
        if start > position:
            break
        end = text.find('\n', start)
        end = len(text) if end < 0 else end
        # Longest existing prefix wins, including spaces in a filename.
        ends = {end} | {start + m.start() for m in re.finditer(r'\s', text[start:end])}
        for stop in sorted(ends, reverse=True):
            candidate = text[start:stop].rstrip('`\"\'.,;:!?)]}')
            candidate = re.sub(r':\d+(?::\d+)?$', '', candidate)
            if not (start <= position < start + len(candidate)):
                continue
            path = Path(candidate).expanduser()
            if not path.is_absolute():
                if not base:
                    continue
                path = Path(base) / path
            try:
                if path.exists():
                    return path
            except OSError:
                continue
    return None


def path_base(widget) -> str:
    while widget is not None:
        if hasattr(widget, '_path_base'):
            return widget._path_base
        widget = widget.parentWidget()
    return ''


def link_path(url, base: str = '') -> Path | None:
    if url.scheme() not in ('', 'file'):
        return None
    value = url.toLocalFile() if url.isLocalFile() else url.path()
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        if not base:
            return None
        path = Path(base) / path
    return path


def reveal(path: Path) -> None:
    if sys.platform == 'darwin':
        argv = ['open', str(path)] if path.is_dir() else ['open', '-R', str(path)]
    elif sys.platform == 'win32':
        argv = ['explorer', str(path)] if path.is_dir() else ['explorer', '/select,', str(path)]
    else:
        argv = ['xdg-open', str(path if path.is_dir() else path.parent)]
    def run():
        try:
            childproc.run(argv, timeout=10, capture_output=True)
        except Exception:
            pass
    threading.Thread(target=run, daemon=True).start()


class PathClickMixin:
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        modifier = "Cmd" if sys.platform == "darwin" else "Ctrl"
        self.setToolTip(f"{modifier}-click a local path to show it in the file manager")

    def mouseReleaseEvent(self, event):
        # Qt maps ControlModifier to Command on macOS.
        if event.button() == QtCore.Qt.LeftButton and event.modifiers() & QtCore.Qt.ControlModifier:
            point = event.position().toPoint() if hasattr(event, 'position') else event.pos()
            cursor = self.cursorForPosition(point)
            text = self.toPlainText()
            position = len(text.encode('utf-16-le')[:cursor.position() * 2].decode('utf-16-le', errors='ignore'))
            base = path_base(self)
            anchor = self.anchorAt(point) if hasattr(self, 'anchorAt') else ''
            path = link_path(QtCore.QUrl(anchor), base) if anchor else None
            if path is None:
                path = path_at(text, position, base)
            if path is not None:
                reveal(path)
                event.accept()
                return
        super().mouseReleaseEvent(event)

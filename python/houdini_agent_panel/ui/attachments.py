"""How an attachment presents itself — shared by the composer and the feed.

Two places show the same thing and must not disagree about it: the chip row
inside the input card (before sending) and the message in the transcript
(after). Both need the same two answers — what is this called, and is there
a picture in it — so the answers live here rather than being written twice.

Deliberately tolerant about shapes. The blocks come from
`composer.build_attachment_block` (`image` / `resource` / `audio`), but a
conversation read back from disk carries the stripped record
`transcript_model._attachment_record` writes — same keys, no payload. Both
must render, so everything here is `.get()` and a fallback.
"""

from __future__ import annotations

import base64
from urllib.parse import unquote

from .qt import QtCore, QtGui


def source_uri(block: dict) -> str:
    """The file this attachment came from, `""` if the block doesn't say.

    `image`/`audio` carry `uri` at the top level (ImageContentBlock's own
    optional field), an embedded `resource` carries it one level down.
    """
    uri = block.get("uri") or (block.get("resource") or {}).get("uri") or ""
    return str(uri)


def label(block: dict) -> str:
    """A file name where there is one, otherwise the kind of thing it is."""
    uri = source_uri(block)
    name = unquote(uri.rsplit("/", 1)[-1]) if uri else ""
    if name:
        return name
    kind = block.get("type")
    if kind == "image":
        return "Image"
    if kind == "audio":
        return "Audio"
    if kind == "resource":
        return "File"
    return "Attachment"


def pixmap(block: dict, size: int) -> "QtGui.QPixmap | None":
    """A preview scaled to fit `size`, or `None` if there is nothing to show.

    `None` covers every honest case: a non-image block, an image whose
    payload was stripped on the way to disk, and data Qt can't decode (an
    EXR or a 32-bit TIFF is a perfectly good attachment for the agent and
    still has no Qt image plugin behind it).
    """
    if block.get("type") != "image":
        return None
    data = block.get("data")
    if not isinstance(data, str) or not data:
        return None
    try:
        raw = base64.b64decode(data)
    except (ValueError, TypeError):
        return None
    image = QtGui.QPixmap()
    if not image.loadFromData(raw):
        return None
    if image.width() > size or image.height() > size:
        image = image.scaled(
            size, size, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation
        )
    return image


def rounded(source: "QtGui.QPixmap", radius: int) -> "QtGui.QPixmap":
    """The same preview with its corners cut.

    A stylesheet `border-radius` does nothing to a `QLabel`'s pixmap — the
    label's own background is what gets rounded, and the picture keeps its
    square corners on top of it. Rounding has to happen in the pixels.
    """
    result = QtGui.QPixmap(source.size())
    result.setDevicePixelRatio(source.devicePixelRatio())
    result.fill(QtCore.Qt.transparent)
    painter = QtGui.QPainter(result)
    try:
        painter.setRenderHint(QtGui.QPainter.Antialiasing)
        path = QtGui.QPainterPath()
        path.addRoundedRect(QtCore.QRectF(source.rect()), radius, radius)
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, source)
    finally:
        painter.end()
    return result


__all__ = ["label", "pixmap", "rounded", "source_uri"]

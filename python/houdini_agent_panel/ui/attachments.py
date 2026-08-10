"""How an attachment presents itself — shared by the composer and the feed.

Two places show the same thing and must not disagree about it: the chip row
inside the input card (before sending) and the message in the transcript
(after). Both need the same two answers — what is this called, and is there
a picture in it — so the answers live here rather than being written twice.

Deliberately tolerant about shapes. The blocks come from
`composer.build_attachment_block` (`image` / `resource` / `audio`) or a
large paste turned into an embedded `resource` with a synthetic uri
(`composer._pasted_text_block`, `composer.PASTED_TEXT_URI_SCHEME`), but a
conversation read back from disk carries the stripped record
`transcript_model._attachment_record` writes — same keys, no image payload
(a pasted text's own words are the one payload that record keeps, up to a
cap — see its own docstring). Both must render, so everything here is
`.get()` and a fallback.
"""

from __future__ import annotations

import base64
from urllib.parse import unquote

from .qt import QtCore, QtGui

#: Must match `composer.PASTED_TEXT_URI_SCHEME` exactly — duplicated
#: rather than imported because `composer.py` already imports this module
#: (`from . import attachments`), and the reverse import would cycle.
_PASTED_TEXT_URI_SCHEME = "pasted-text:"


def source_uri(block: dict) -> str:
    """The file this attachment came from, `""` if the block doesn't say.

    `image`/`audio` carry `uri` at the top level (ImageContentBlock's own
    optional field), an embedded `resource` carries it one level down —
    and, once a record has been through `_attachment_record`/disk, THAT
    also flattens it back to the top level, so both forms are checked.
    """
    uri = block.get("uri") or (block.get("resource") or {}).get("uri") or ""
    return str(uri)


def is_pasted_text(block: dict) -> bool:
    """Whether `block` is a large paste carried as an embedded resource
    (`composer._pasted_text_block`) rather than an attached FILE
    (`build_attachment_block`, always a real `file://` uri) — the only
    thing that tells the two apart, since both are `type: "resource"`.
    """
    return source_uri(block).startswith(_PASTED_TEXT_URI_SCHEME)


def _resource_text(block: dict) -> str:
    """The pasted words themselves — top-level `text` for a record read
    back off disk (`_attachment_record` flattens it there, same as `uri`/
    `mimeType`), nested `resource.text` for the live block a paste just
    built and hasn't been saved yet."""
    text = block.get("text")
    if text is None:
        text = (block.get("resource") or {}).get("text")
    return str(text or "")


def label(block: dict) -> str:
    """A file name where there is one, otherwise the kind of thing it is.

    A pasted-text resource never has a real file behind it to name (see
    `is_pasted_text`) so it's checked before the filename lookup, by its
    own count of lines rather than a generic name: "Pasted text, 342
    lines" says more than "File" would.
    """
    if is_pasted_text(block):
        return _pasted_text_label(_resource_text(block))
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


def _pasted_text_label(text: str) -> str:
    """Counts lines the way an artist would: a trailing newline isn't an
    extra empty one, and anything non-empty is at least one line — a
    stored record that got trimmed on the way to disk
    (`transcript_model._attachment_record`) still counts the lines it
    actually kept, which is the honest number for what a click can show,
    not a claim about what the paste originally had.
    """
    if not text:
        return "Pasted text"
    lines = text.count("\n") + (0 if text.endswith("\n") else 1)
    lines = max(lines, 1)
    noun = "line" if lines == 1 else "lines"
    return f"Pasted text, {lines} {noun}"


def tooltip(block: dict) -> str:
    """The full detail behind a chip — for a pasted-text resource, the
    pasted text itself (there is no filename to show instead, and this is
    the only place in the UI an artist can read it back before sending).
    Every other kind keeps `label`'s own short answer, same as today.

    `truncated_chars` (`transcript_model._attachment_record`'s own field,
    set only once a stored record's text was cut for size) is said
    plainly rather than left for the artist to notice the text just stops
    — the same "never drop something without a word" rule as everywhere
    else attachments are handled.
    """
    if not is_pasted_text(block):
        return label(block)
    text = _resource_text(block)
    truncated = block.get("truncated_chars")
    if truncated:
        text = f"{text}\n\n… {truncated} more character(s) not shown (trimmed on save)."
    return text


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
    if image.width() <= size and image.height() <= size:
        return image
    return image.scaled(
        size, size, QtCore.Qt.KeepAspectRatio, QtCore.Qt.SmoothTransformation
    )


__all__ = ["is_pasted_text", "label", "pixmap", "source_uri", "tooltip"]

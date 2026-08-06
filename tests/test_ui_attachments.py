"""An attachment belongs to the message it was attached to.

Attaching a picture used to leave a chip in the composer and nothing at all
in the feed: the message went out with the image, the transcript showed only
the typed words, and scrolling back gave no way to tell which render the
agent had actually been shown.
"""

from __future__ import annotations

import base64

from houdini_agent_panel.transcript_model import TranscriptModel
from houdini_agent_panel.ui import attachments
from houdini_agent_panel.ui.qt import QtCore, QtGui
from houdini_agent_panel.ui.transcript import TranscriptView


def _png_bytes(size: int = 8) -> bytes:
    image = QtGui.QImage(size, size, QtGui.QImage.Format_RGB32)
    image.fill(QtGui.QColor("#3f7fbf"))
    buffer = QtCore.QBuffer()
    buffer.open(QtCore.QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(buffer.data())


def _image_block(uri: str = "file:///tmp/hero%20render.png") -> dict:
    return {
        "type": "image",
        "data": base64.b64encode(_png_bytes()).decode("ascii"),
        "mimeType": "image/png",
        "uri": uri,
    }


def _resource_block() -> dict:
    return {
        "type": "resource",
        "resource": {"uri": "file:///tmp/notes.txt", "text": "hi", "mimeType": "text/plain"},
    }


# --- presentation helpers --------------------------------------------------


def test_label_prefers_the_file_name(qapp):
    assert attachments.label(_image_block()) == "hero render.png"
    assert attachments.label(_resource_block()) == "notes.txt"
    assert attachments.label({"type": "image", "data": ""}) == "Image"
    assert attachments.label({"type": "audio"}) == "Audio"


def test_pixmap_only_for_images_with_a_payload(qapp):
    assert attachments.pixmap(_image_block(), 64) is not None
    assert attachments.pixmap(_resource_block(), 64) is None
    # A restored conversation keeps the chip and loses the pixels.
    assert attachments.pixmap({"type": "image", "uri": "file:///a.png"}, 64) is None


def test_pixmap_fits_the_requested_box(qapp):
    big = {"type": "image", "data": base64.b64encode(_png_bytes(400)).decode("ascii")}
    preview = attachments.pixmap(big, 100)
    assert max(preview.width(), preview.height()) == 100


# --- the model ---------------------------------------------------------------


def test_a_user_entry_carries_its_attachments():
    model = TranscriptModel()
    entry = model.append_user("look at this", [_image_block()])
    assert entry.attachments == [_image_block()]


def test_records_keep_the_name_and_drop_the_payload():
    model = TranscriptModel()
    model.append_user("look", [_image_block(), _resource_block()])

    records = model.to_records()
    assert records[0]["attachments"] == [
        {"type": "image", "uri": "file:///tmp/hero%20render.png", "mimeType": "image/png"},
        {"type": "resource", "uri": "file:///tmp/notes.txt", "mimeType": "text/plain"},
    ]
    # The base64 blob is the whole point of stripping: a saved conversation
    # must not grow by the size of every image ever sent into it.
    assert "data" not in records[0]["attachments"][0]


def test_an_attachment_only_message_survives_a_round_trip():
    model = TranscriptModel()
    model.append_user("", [_image_block()])

    restored = TranscriptModel()
    restored.load_records(model.to_records())

    entries = restored.entries()
    assert len(entries) == 1
    assert entries[0].attachments[0]["uri"] == "file:///tmp/hero%20render.png"


# --- the feed ------------------------------------------------------------------


def test_the_feed_draws_the_image_inside_the_message(qapp):
    model = TranscriptModel()
    view = TranscriptView()
    view.set_model(model)

    entry = model.append_user("what is wrong here?", [_image_block()])
    view.refresh(entry.id)

    row = view._rows[entry.id]
    assert row._attachments is not None
    previews = [child for child in row._attachments.children() if hasattr(child, "pixmap")]
    assert any(not child.pixmap().isNull() for child in previews)
    # ...and the typed words are still there, in the same row.
    assert row._segments[0].toPlainText() == "what is wrong here?"


def test_streaming_a_message_does_not_rebuild_the_attachment_strip(qapp):
    model = TranscriptModel()
    view = TranscriptView()
    view.set_model(model)
    entry = model.append_user("one", [_image_block()])
    view.refresh(entry.id)
    strip = view._rows[entry.id]._attachments

    entry.text = "one two"
    view.refresh(entry.id)

    assert view._rows[entry.id]._attachments is strip


def test_a_file_with_no_preview_falls_back_to_a_named_chip(qapp):
    model = TranscriptModel()
    view = TranscriptView()
    view.set_model(model)

    entry = model.append_user("check this", [_resource_block()])
    view.refresh(entry.id)

    strip = view._rows[entry.id]._attachments
    texts = [child.text() for child in strip.findChildren(object) if hasattr(child, "text")]
    assert "notes.txt" in texts

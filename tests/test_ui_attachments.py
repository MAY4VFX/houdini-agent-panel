"""An attachment belongs to the message it was attached to.

Attaching a picture used to leave a chip in the composer and nothing at all
in the feed: the message went out with the image, the transcript showed only
the typed words, and scrolling back gave no way to tell which render the
agent had actually been shown.
"""

from __future__ import annotations

import base64

import pytest

from houdini_agent_panel import conversations_store as store
from houdini_agent_panel import sessions
from houdini_agent_panel.transcript_model import TranscriptModel
from houdini_agent_panel.ui import attachments
from houdini_agent_panel.ui import composer as composer_mod
from houdini_agent_panel.ui import panel as panel_mod
from houdini_agent_panel.ui.qt import QtCore, QtGui
from houdini_agent_panel.ui.transcript import TranscriptView, _MessageRow


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


# --- the full round trip, through the panel and onto disk -------------------
#
# Everything above exercises `TranscriptModel` directly — real coverage, but
# it never proves `ui/panel.py::_on_submitted` actually hands attachments to
# the model it just tested, or that `conversations_store` still has them
# after `_persist_conversations` writes and something reads the file back.
# Investigated after a report ("вложения не сохраняются", conversations.json
# had zero `attachments` keys across 50 real conversations) that turned out
# to be old data: those particular messages were sent under panel 0.8.20,
# hours before `Entry.attachments` existed at all (shipped in 0.8.21).
# Nothing upstream of here was actually broken — but nothing had ever proven
# that either, so this closes the real gap the report surfaced.


@pytest.fixture
def isolated_panel(qapp, monkeypatch):
    monkeypatch.setattr(panel_mod.scene, "hip_dir", lambda: "/tmp")
    monkeypatch.setattr(
        panel_mod.scene, "mcp_servers",
        lambda: [{"name": "fxhoudini", "command": "python", "args": [], "env": []}],
    )
    monkeypatch.setattr(panel_mod._RefreshWorker, "start", lambda self: None)
    panel_mod.reset_shared_state_for_tests()
    yield
    panel_mod.reset_shared_state_for_tests()


def test_a_sent_attachment_survives_persist_and_reload_from_disk(isolated_panel, qapp, monkeypatch):
    widget = panel_mod.AgentPanel()
    qapp.processEvents()
    client = panel_mod.shared_client(widget._agent_id)
    state = sessions.SessionState(
        session_id="s1", title="New conversation", cwd="/tmp", created_at=0.0
    )
    client.session_started.emit(state.session_id, state)
    qapp.processEvents()
    monkeypatch.setattr(client, "prompt", lambda sid, blocks: None)

    widget._on_submitted([{"type": "text", "text": "look at this"}, _image_block()])
    widget._persist_conversations()

    conversation_id = widget._conversation_ids[state.session_id]
    reloaded = next(c for c in store.load() if c.id == conversation_id)
    user_entries = [e for e in reloaded.entries if e.get("kind") == "user"]
    assert user_entries, "the sent message must be on disk"
    assert user_entries[-1].get("attachments") == [
        {"type": "image", "uri": "file:///tmp/hero%20render.png", "mimeType": "image/png"},
    ]
    widget.shutdown()


def test_queued_messages_sent_together_stay_separate_bubbles(qapp):
    """The queue-batch send (b09f083: everything queued goes out in ONE
    `session/prompt` call, but "each keeps its own transcript entry") was
    the other suspect for the same report — three images with text between
    them, in what read as a single message bubble. Reproduced here as three
    queued-then-promoted entries, the exact shape `_drain_queue` produces:
    each gets its own `_MessageRow` and its own `_AttachmentStrip`, nothing
    merges them into one. If this ever goes red, that IS the bug — today it
    doesn't, so what the owner saw was several genuinely separate bubbles
    sitting close together, not blocks landing in the wrong strip.
    """
    model = TranscriptModel()
    view = TranscriptView()
    view.set_model(model)

    entries = [
        model.queue_message(f"q{i}", f"message {i}", [_image_block(f"file:///tmp/img{i}.png")])
        for i in range(3)
    ]
    for entry in entries:
        view.refresh(entry.id)
    for entry in entries:
        model.promote_queued(entry.id)
        view.refresh(entry.id)

    unique_rows = list({id(row): row for row in view._rows.values()}.values())
    assert len(unique_rows) == 3, "each queued message keeps its own row"
    for row in unique_rows:
        assert isinstance(row, _MessageRow)
        assert row._attachments is not None


# --- a large paste, turned into a `resource` attachment with a synthetic
# uri (`ui/composer.py::_pasted_text_block`, `_LARGE_PASTE_LINE_CHARS`) —
# the non-destructive replacement for cutting the line: nothing is dropped,
# the whole thing rides along as its own block, same as an image or a real
# file attachment already does. `resource`, not `text`: a plain `type:
# "text"` block was tried first and dropped — `ui/panel.py::_on_submitted`
# reads `type == "text"` to mean "the artist's own typed words," so a
# second one merged into the plain message and the chip was lost the
# moment it left the composer. -----------------------------------------


def _text_block(text: str, uri: str = "pasted-text:///test") -> dict:
    """The LIVE shape `composer._pasted_text_block` builds — nested under
    `resource`, same as any other embedded resource. `uri` defaults to
    something `attachments.is_pasted_text` recognises; override it only to
    prove that check actually looks at the uri, not just the block's type.
    """
    return {"type": "resource", "resource": {"uri": uri, "text": text, "mimeType": "text/plain"}}


def _stored_text_record(text: str, uri: str = "pasted-text:///test", **extra) -> dict:
    """The shape `transcript_model._attachment_record` writes to disk —
    flat, same as `uri`/`mimeType` already are for any other `resource`."""
    record = {"type": "resource", "uri": uri, "mimeType": "text/plain", "text": text}
    record.update(extra)
    return record


def test_pasted_text_is_told_apart_from_a_real_file_resource(qapp):
    assert attachments.is_pasted_text(_text_block("hi")) is True
    assert attachments.is_pasted_text(_resource_block()) is False, (
        "a real text FILE (a genuine file:// uri) must not be mistaken for a paste"
    )


def test_pasted_text_label_counts_lines(qapp):
    assert attachments.label(_text_block("one line")) == "Pasted text, 1 line"
    assert attachments.label(_text_block("a\nb\nc")) == "Pasted text, 3 lines"
    # A trailing newline isn't an extra empty line.
    assert attachments.label(_text_block("a\nb\n")) == "Pasted text, 2 lines"
    assert attachments.label(_text_block("")) == "Pasted text"
    # A restored record is flat, not nested under "resource" — same answer.
    assert attachments.label(_stored_text_record("a\nb\nc")) == "Pasted text, 3 lines"


def test_pasted_text_tooltip_shows_the_full_text(qapp):
    block = _text_block("the whole pasted paragraph, in full")
    assert attachments.tooltip(block) == "the whole pasted paragraph, in full"


def test_pasted_text_tooltip_says_how_much_was_trimmed_on_save(qapp):
    record = _stored_text_record("kept part", truncated_chars=4000)
    tip = attachments.tooltip(record)
    assert tip.startswith("kept part")
    assert "4000" in tip


def test_a_user_entry_carries_pasted_text_as_an_attachment():
    model = TranscriptModel()
    block = _text_block("x" * 5000)
    entry = model.append_user("what does this mean?", [block])
    assert entry.attachments == [block]


def test_the_feed_draws_a_chip_not_an_image_for_pasted_text(qapp):
    model = TranscriptModel()
    view = TranscriptView()
    view.set_model(model)

    entry = model.append_user("check this output", [_text_block("line one\nline two")])
    view.refresh(entry.id)

    row = view._rows[entry.id]
    assert row._attachments is not None
    labels = [
        child.text()
        for child in row._attachments.findChildren(object)
        if hasattr(child, "text") and callable(child.text)
    ]
    assert "Pasted text, 2 lines" in labels


def test_attachment_record_keeps_a_moderate_pasted_text_in_full():
    from houdini_agent_panel.transcript_model import _attachment_record

    block = _text_block("a modest paste, easily kept whole", uri="pasted-text:///abc")
    record = _attachment_record(block)
    assert record == {
        "type": "resource",
        "uri": "pasted-text:///abc",
        "mimeType": "text/plain",
        "text": "a modest paste, easily kept whole",
    }
    assert "truncated_chars" not in record


def test_attachment_record_caps_a_huge_pasted_text_and_says_how_much_was_cut(monkeypatch):
    """Unlike an image's base64 payload, a pasted text's payload IS the
    artist's own words — worth keeping on disk, not stripped outright, but
    still bounded: `conversations.json` autosaves on every prompt and turn,
    and nothing should be able to make that grow without limit."""
    import houdini_agent_panel.transcript_model as tm

    monkeypatch.setattr(tm, "_MAX_STORED_PASTE_CHARS", 10)
    record = tm._attachment_record(_text_block("0123456789ABCDEF"))
    assert record["text"] == "0123456789"
    assert record["truncated_chars"] == 6


def test_attachment_record_leaves_a_real_file_resource_alone(monkeypatch):
    """The cap and `truncated_chars` are a `pasted-text:` thing only — a
    real text FILE attachment (`build_attachment_block`, embedded via
    `supports_embedded_context`) keeps whatever `_attachment_record`
    already did for it before this feature existed: no `text` field at
    all, uri and mimeType only."""
    import houdini_agent_panel.transcript_model as tm

    monkeypatch.setattr(tm, "_MAX_STORED_PASTE_CHARS", 10)
    record = tm._attachment_record(_resource_block())
    assert "text" not in record
    assert "truncated_chars" not in record


def test_a_restored_pasted_text_chip_shows_what_was_actually_kept():
    """A conversation read back from disk only ever has what `_attachment_
    record` kept — `load_records` must carry it straight through, same as
    any other attachment field."""
    model = TranscriptModel()
    model.append_user("", [_text_block("kept")])
    restored = TranscriptModel()
    restored.load_records(model.to_records())

    entries = restored.entries()
    assert entries[0].attachments == [_stored_text_record("kept")]
    assert attachments.label(entries[0].attachments[0]) == "Pasted text, 1 line"


def test_a_large_pasted_text_survives_persist_and_reload_from_disk(qapp, monkeypatch):
    """The full chain, not just the model in isolation — `ui/panel.py::
    _on_submitted` through `conversations_store`, the same round trip
    `test_a_sent_attachment_survives_persist_and_reload_from_disk` already
    proves for an image."""
    monkeypatch.setattr(panel_mod.scene, "hip_dir", lambda: "/tmp")
    monkeypatch.setattr(
        panel_mod.scene, "mcp_servers",
        lambda: [{"name": "fxhoudini", "command": "python", "args": [], "env": []}],
    )
    monkeypatch.setattr(panel_mod._RefreshWorker, "start", lambda self: None)
    panel_mod.reset_shared_state_for_tests()
    try:
        widget = panel_mod.AgentPanel()
        qapp.processEvents()
        client = panel_mod.shared_client(widget._agent_id)
        state = sessions.SessionState(
            session_id="s1", title="New conversation", cwd="/tmp", created_at=0.0
        )
        client.session_started.emit(state.session_id, state)
        qapp.processEvents()
        monkeypatch.setattr(client, "prompt", lambda sid, blocks: None)

        pasted = "the pasted paragraph\nspanning a couple of lines"
        widget._on_submitted([{"type": "text", "text": "look at this"}, _text_block(pasted)])
        widget._persist_conversations()

        conversation_id = widget._conversation_ids[state.session_id]
        reloaded = next(c for c in store.load() if c.id == conversation_id)
        user_entries = [e for e in reloaded.entries if e.get("kind") == "user"]
        assert user_entries[-1].get("attachments") == [_stored_text_record(pasted)]
        widget.shutdown()
    finally:
        panel_mod.reset_shared_state_for_tests()

"""Composer tests: field growth, capability gating, slash popup, attachments, blocking."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from houdini_agent_panel.client import AgentInfo
from houdini_agent_panel.sessions import AvailableCommand, SessionMode, Usage
from houdini_agent_panel.ui import theme
from houdini_agent_panel.ui import composer as composer_mod
from houdini_agent_panel.ui.composer import (
    Composer,
    _image_block_from_qimage,
    _is_marketplace_command,
    _looks_like_image,
    _parse_enum_hint,
    attachment_rejection_reason,
    build_attachment_block,
)


def _info(**overrides) -> AgentInfo:
    base = dict(
        name="test-agent",
        version="1.0",
        protocol_version=1,
        supports_image=False,
        supports_audio=False,
        supports_embedded_context=False,
        supports_load_session=False,
        supports_logout=False,
        auth_methods=(),
    )
    base.update(overrides)
    return AgentInfo(**base)


def _type_text(edit: QtWidgets.QPlainTextEdit, text: str) -> None:
    """Put text in the input field, firing `textChanged` as real typing would.

    `QtTest.QTest.keyClicks` won't do: it only handles ASCII (it hits an
    `ASSERT` in `qasciikey.cpp` on anything else), and an artist types in
    whatever language they like.
    """
    edit.setFocus()
    cursor = edit.textCursor()
    cursor.insertText(text)


def _press_enter(edit: QtWidgets.QWidget, *, shift: bool = False) -> None:
    modifiers = QtCore.Qt.ShiftModifier if shift else QtCore.Qt.NoModifier
    QtTest.QTest.keyClick(edit, QtCore.Qt.Key_Return, modifiers)


# --- capability gating --------------------------------------------------------


def test_attach_button_hidden_without_capability(qapp):
    composer = Composer()
    composer.show()
    composer.set_capabilities(_info(), "")
    assert not composer._attach_button.isVisible()


@pytest.mark.parametrize("field", ["supports_image", "supports_embedded_context"])
def test_attach_button_visible_with_capability(qapp, field):
    composer = Composer()
    composer.show()
    composer.set_capabilities(_info(**{field: True}), "")
    assert composer._attach_button.isVisible()


def test_attach_button_hidden_without_agent(qapp):
    composer = Composer()
    composer.show()
    composer.set_capabilities(None, "")
    assert not composer._attach_button.isVisible()


def test_voice_button_hidden_without_audio_and_whisper(qapp):
    composer = Composer()
    composer.show()
    composer.set_capabilities(_info(), "")
    assert not composer._voice_button.isVisible()


def test_voice_button_visible_with_whisper_endpoint_even_without_audio_capability(qapp, monkeypatch):
    composer = Composer()
    composer.show()
    # The recording backend is faked — this is about the button's visibility,
    # not about a real microphone.
    monkeypatch.setattr(
        composer._voice_button,
        "_backend_factory",
        lambda: (object(), ""),
    )
    composer.set_capabilities(_info(), "http://127.0.0.1:9000")
    assert composer._voice_button.isVisible()


def test_mode_chip_hidden_until_agent_sends_modes(qapp):
    composer = Composer()
    composer.show()
    assert not composer.mode_chip.isVisible()

    composer.mode_chip.set_modes([SessionMode("code", "Code"), SessionMode("ask", "Ask")], "code")
    assert composer.mode_chip.isVisible()

    composer.mode_chip.set_modes([], None)
    assert not composer.mode_chip.isVisible()


def test_mode_chip_selection_forwards_to_composer_signal(qapp):
    composer = Composer()
    composer.show()

    composer.mode_chip.set_modes([SessionMode("code", "Code")], "code")
    received = []
    composer.mode_selected.connect(received.append)
    composer.mode_chip.mode_selected.emit("code")
    assert received == ["code"]


def test_composer_set_modes_is_a_facade_over_mode_chip(qapp):
    """The panel feeds modes through `Composer.set_modes` instead of reaching
    into the nested `mode_chip` (architecture.md §10)."""
    composer = Composer()
    composer.show()
    assert not composer.mode_chip.isVisible()

    composer.set_modes([SessionMode("code", "Code"), SessionMode("ask", "Ask")], "ask")
    assert composer.mode_chip.isVisible()

    received = []
    composer.mode_selected.connect(received.append)
    composer.mode_chip.mode_selected.emit("code")
    assert received == ["code"]

    composer.set_modes([], None)
    assert not composer.mode_chip.isVisible()


# --- sending text -------------------------------------------------------------


def test_enter_submits_text_block(qapp):
    composer = Composer()
    composer.show()
    received = []
    composer.submitted.connect(received.append)

    _type_text(composer._text_edit, "hello")
    _press_enter(composer._text_edit)

    assert received == [[{"type": "text", "text": "hello"}]]
    assert composer._text_edit.toPlainText() == ""


def test_shift_enter_inserts_newline_without_submitting(qapp):
    composer = Composer()
    composer.show()
    received = []
    composer.submitted.connect(received.append)

    _type_text(composer._text_edit, "line1")
    _press_enter(composer._text_edit, shift=True)
    _type_text(composer._text_edit, "line2")

    assert received == []
    assert composer._text_edit.toPlainText() == "line1\nline2"


def test_empty_input_does_not_emit_submitted(qapp):
    composer = Composer()
    composer.show()
    received = []
    composer.submitted.connect(received.append)
    _press_enter(composer._text_edit)
    assert received == []


def test_composer_uses_same_centered_736px_rail_as_precision_mockup(qapp):
    host = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    composer = Composer(host)
    layout.addWidget(composer)
    host.resize(900, 180)
    host.show()
    qapp.processEvents()

    surface_pos = composer._surface.mapTo(composer, QtCore.QPoint(0, 0))
    right_gutter = composer.width() - surface_pos.x() - composer._surface.width()

    assert composer._surface.width() == 736
    assert abs(surface_pos.x() - right_gutter) <= 1


def _hosted_composer(qapp, width: int, height: int = 200) -> tuple["QtWidgets.QWidget", "Composer"]:
    """Same wrapping `test_composer_uses_same_centered_736px_rail_as_
    precision_mockup` already uses, not a bare top-level `Composer()`.

    A `Composer()` with no parent IS itself the top-level window in a
    test, and Qt clamps an explicit `resize()` on a layout-managed
    top-level window to the layout's CURRENT minimum size before
    `resizeEvent` ever runs — so shrinking it below whatever the surface's
    fixed width happened to be a moment ago silently no-ops (confirmed
    directly: asked for 200px, got back 572, the previous width, until a
    SECOND resize finally took). A host widget with its own layout does
    not have that quirk; `composer.resize(...)` inside it behaves like
    real embedding inside `AgentPanel`'s own `_body_layout`.
    """
    host = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(host)
    layout.setContentsMargins(0, 0, 0, 0)
    composer = Composer(host)
    layout.addWidget(composer)
    host.resize(width, height)
    host.show()
    qapp.processEvents()
    return host, composer


def test_bug_report_link_click_emits_signal(qapp):
    _host, composer = _hosted_composer(qapp, 600)

    seen = []
    composer.bug_report_link_clicked.connect(lambda: seen.append(True))
    composer._bug_report_link.click()

    assert seen == [True]


def test_bug_report_link_sits_under_the_input_box_not_beside_it(qapp):
    """Owner's placement, by screenshot: the thin strip BELOW the composer,
    not the header, not a corner floating over the transcript. Checked
    against the input box's own geometry (not a fixed pixel guess) so this
    stays true if either one's size ever changes."""
    _host, composer = _hosted_composer(qapp, 600)

    link = composer._bug_report_link
    surface = composer._surface
    assert link.y() >= surface.y() + surface.height()
    # Centred under the input box itself (a later owner request), not on
    # the composer's own width — the two differ at almost every panel
    # width, since the surface is clamped/centred with its own margins.
    link_center = link.x() + link.width() / 2
    surface_center = surface.x() + surface.width() / 2
    assert abs(link_center - surface_center) <= 1


def test_bug_report_link_tracks_the_surface_so_a_drawer_cannot_shift_it(qapp):
    """The conversation drawer draws INSIDE `TranscriptView`'s existing
    gutter and never touches the composer's own geometry at all
    (`AgentPanel._body_layout`'s own note) — this composer never even
    hears about the drawer. What this actually guards is the weaker, more
    local claim that has to hold for that to matter: the link's X is
    derived from THE SAME `surface_x`/width used to position the input
    box and the buddy sprite, at every width, not a value that could ever
    independently drift from them.
    """
    host, composer = _hosted_composer(qapp, 400)

    for width in (400, 900, 500):
        host.resize(width, 200)
        qapp.processEvents()
        surface = composer._surface
        link = composer._bug_report_link
        expected_x = surface.x() + (surface.width() - link.width()) // 2
        assert abs(link.x() - expected_x) <= 1


def test_bug_report_link_hides_on_a_narrow_docked_panel(qapp):
    """Deliberate choice for the narrowest docked widths (owner asked for
    one, rather than whatever clipping/wrapping fell out of the layout):
    hidden, not clipped illegibly against the surface's own edge — Settings
    keeps its own entry point reachable regardless of width."""
    host, composer = _hosted_composer(qapp, 600)
    assert composer._bug_report_link.isVisible() is True

    host.resize(200, 200)
    qapp.processEvents()
    assert composer._bug_report_link.isVisible() is False

    host.resize(600, 200)
    qapp.processEvents()
    assert composer._bug_report_link.isVisible() is True


def test_submitted_includes_attachments_after_text(qapp, tmp_path):
    composer = Composer()
    composer.show()
    composer.set_capabilities(_info(supports_image=True), "")

    image_path = tmp_path / "pic.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfakepngdata")
    assert composer.add_attachment(image_path) is True

    received = []
    composer.submitted.connect(received.append)
    _type_text(composer._text_edit, "look")
    _press_enter(composer._text_edit)

    assert len(received) == 1
    blocks = received[0]
    assert blocks[0] == {"type": "text", "text": "look"}
    assert blocks[1]["type"] == "image"
    assert blocks[1]["mimeType"] == "image/png"
    assert base64.b64decode(blocks[1]["data"]) == image_path.read_bytes()
    # Attachments and text are cleared after sending.
    assert composer._text_edit.toPlainText() == ""


def test_add_attachment_without_capability_is_rejected(qapp, tmp_path):
    composer = Composer()
    composer.show()
    composer.set_capabilities(_info(), "")  # neither image nor embeddedContext
    path = tmp_path / "pic.png"
    path.write_bytes(b"data")
    assert composer.add_attachment(path) is False


# --- "+" reopening the picker by itself ----------------------------------------
#
# Reported for real: attached an image through "+", the chip appeared
# correctly, and then the file picker opened again on its own. `QFileDialog.
# getOpenFileNames` on macOS is a native SHEET, not an application-modal
# dialog — the rest of the app's event loop keeps running while it's up, so a
# fast double-click can deliver its second press straight to the still-
# enabled button while the first dialog is still showing. Counting picker
# invocations, not just checking an attachment landed (a second, cancelled
# dialog leaves the SAME one attachment either way).


def test_attach_click_invokes_the_picker_exactly_once(qapp, monkeypatch, tmp_path):
    composer = Composer()
    composer.show()
    composer.set_capabilities(_info(supports_image=True), "")

    calls = []
    monkeypatch.setattr(
        QtWidgets.QFileDialog, "getOpenFileNames", lambda *a, **kw: (calls.append(1), ([], ""))[1]
    )

    composer._attach_button.click()

    assert len(calls) == 1


def test_a_second_click_while_the_picker_is_still_open_is_dropped(qapp, monkeypatch, tmp_path):
    """Simulates the reported shape directly: the SECOND click lands WHILE
    the first dialog call is still executing (a real fast double-click
    landing on the still-enabled button before the sheet visually steals
    focus) — not a later, independent click. Without `_attach_dialog_open`
    guarding `_on_attach_clicked`, this reopens the dialog a second time
    before the first has even returned."""
    composer = Composer()
    composer.show()
    composer.set_capabilities(_info(supports_image=True), "")

    calls = []

    def fake_dialog(*_args, **_kwargs):
        calls.append(1)
        if len(calls) == 1:
            # The reentrant click, fired from mid-dialog — exactly what a
            # native sheet not blocking the rest of the event loop allows.
            composer._attach_button.click()
        return ([], "")

    monkeypatch.setattr(QtWidgets.QFileDialog, "getOpenFileNames", fake_dialog)

    composer._attach_button.click()

    assert len(calls) == 1, "a click landing while the picker is already open must be dropped"


def test_drag_and_drop_never_opens_the_file_picker(qapp, monkeypatch, tmp_path):
    """`add_attachment` is shared by the picker, drag-and-drop and paste —
    but only the picker ever calls `QFileDialog` at all. Confirms the fault
    can only be in `_on_attach_clicked`'s own wiring, not in the shared
    `add_attachment` tail every route funnels through."""
    composer = Composer()
    composer.show()
    composer.set_capabilities(_info(supports_image=True), "")

    calls = []
    monkeypatch.setattr(
        QtWidgets.QFileDialog, "getOpenFileNames", lambda *a, **kw: (calls.append(1), ([], ""))[1]
    )

    image_path = tmp_path / "pic.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfakepngdata")
    # `mime` held as its own named reference, not inlined into the
    # constructor call: `QDropEvent` does not take ownership of the
    # `QMimeData` it's given, and a temporary with nothing else referencing
    # it can be garbage-collected out from under the event (measured: a
    # segfault inside `dropEvent` with the inline form).
    mime = _mime_with_file_url(image_path)
    event = QtGui.QDropEvent(
        QtCore.QPointF(0, 0),
        QtCore.Qt.CopyAction,
        mime,
        QtCore.Qt.LeftButton,
        QtCore.Qt.NoModifier,
    )
    composer.dropEvent(event)

    assert len(composer._attachments) == 1
    assert calls == []


def test_pasting_an_image_file_never_opens_the_file_picker(qapp, monkeypatch, tmp_path):
    composer = Composer()
    composer.show()
    composer.set_capabilities(_info(supports_image=True), "")

    calls = []
    monkeypatch.setattr(
        QtWidgets.QFileDialog, "getOpenFileNames", lambda *a, **kw: (calls.append(1), ([], ""))[1]
    )

    image_path = tmp_path / "pic.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfakepngdata")
    composer._text_edit.insertFromMimeData(_mime_with_file_url(image_path))

    assert len(composer._attachments) == 1
    assert calls == []


# --- build_attachment_block directly -------------------------------------------


def test_build_attachment_block_image(tmp_path):
    path = tmp_path / "a.png"
    path.write_bytes(b"binarydata")
    block = build_attachment_block(path, _info(supports_image=True))
    assert block["type"] == "image"
    assert block["mimeType"] == "image/png"
    assert base64.b64decode(block["data"]) == b"binarydata"


def test_build_attachment_block_text_resource(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("hello world", "utf-8")
    block = build_attachment_block(path, _info(supports_embedded_context=True))
    assert block["type"] == "resource"
    assert block["resource"]["text"] == "hello world"
    assert block["resource"]["uri"] == path.resolve().as_uri()


def test_build_attachment_block_none_without_capability(tmp_path):
    path = tmp_path / "a.png"
    path.write_bytes(b"data")
    assert build_attachment_block(path, _info()) is None


def test_build_attachment_block_image_over_the_cap_is_refused(tmp_path, monkeypatch):
    """Nothing capped this before pasted images existed — a file attached
    through the dialog or drag-and-drop went straight from disk to a
    base64 blob, whatever its size."""
    monkeypatch.setattr(composer_mod, "_MAX_ATTACHMENT_BYTES", 4)
    path = tmp_path / "a.png"
    path.write_bytes(b"more than four bytes")
    assert build_attachment_block(path, _info(supports_image=True)) is None


# --- pasting images --------------------------------------------------------


def _mime_with_image(color: str = "red", size: int = 8) -> QtCore.QMimeData:
    pixmap = QtGui.QPixmap(size, size)
    pixmap.fill(QtGui.QColor(color))
    mime = QtCore.QMimeData()
    mime.setImageData(pixmap.toImage())
    return mime


def _mime_with_file_url(path: Path) -> QtCore.QMimeData:
    mime = QtCore.QMimeData()
    mime.setUrls([QtCore.QUrl.fromLocalFile(str(path))])
    return mime


def test_looks_like_image_by_extension():
    assert _looks_like_image("/tmp/shot.png") is True
    assert _looks_like_image("/tmp/shot.JPG") is True
    assert _looks_like_image("/tmp/scene.hip") is False
    assert _looks_like_image("/tmp/no_extension") is False


def test_pasting_plain_text_is_unaffected(qapp):
    """The overwhelming common case — a paste with no image on the
    clipboard at all must reach Qt's own default handling untouched."""
    composer = Composer()
    composer.show()
    composer.set_capabilities(_info(supports_image=True), "")

    mime = QtCore.QMimeData()
    mime.setText("make the rotor emit dust")
    composer._text_edit.insertFromMimeData(mime)

    assert composer._text_edit.toPlainText() == "make the rotor emit dust"
    assert composer._attachments == []


def test_pasting_a_raw_image_adds_an_attachment_when_supported(qapp):
    """A screenshot copied to the clipboard — `hasImage`, no file behind
    it (measured for real on this Mac with `screencapture -c`:
    `hasImage=True`, `hasUrls=False`, `hasText=False`)."""
    composer = Composer()
    composer.show()
    composer.set_capabilities(_info(supports_image=True), "")

    composer._text_edit.insertFromMimeData(_mime_with_image())

    assert len(composer._attachments) == 1
    assert composer._attachments[0]["type"] == "image"
    assert composer._attachments[0]["mimeType"] == "image/png"
    # Nothing was typed into the field — a raw image paste has no text
    # form worth inserting.
    assert composer._text_edit.toPlainText() == ""


def test_pasting_a_raw_image_is_rejected_without_supports_image(qapp):
    composer = Composer()
    composer.show()
    composer.set_capabilities(_info(), "")
    rejected = []
    composer.attachment_rejected.connect(rejected.append)

    composer._text_edit.insertFromMimeData(_mime_with_image())

    assert composer._attachments == []
    assert rejected and "before pasting" in rejected[0]


def test_pasting_a_copied_image_file_reuses_the_attach_path(qapp, tmp_path):
    """The "Finder file-copy gives a path, not pixels" case — a `QMimeData`
    built from a file URL, the documented shape for a file copied in
    Finder (this project's own `screencapture -c` test confirmed the
    RAW-PIXEL case directly; driving Finder itself to confirm this exact
    shape was not possible in this sandbox — see the write-up). Routed
    through `add_attachment`, the same path drag-and-drop already uses."""
    composer = Composer()
    composer.show()
    composer.set_capabilities(_info(supports_image=True), "")
    image_path = tmp_path / "pic.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nfakepngdata")

    composer._text_edit.insertFromMimeData(_mime_with_file_url(image_path))

    assert len(composer._attachments) == 1
    assert base64.b64decode(composer._attachments[0]["data"]) == image_path.read_bytes()
    assert composer._text_edit.toPlainText() == ""


def test_a_file_url_does_not_fall_through_to_its_own_path_as_text(qapp, tmp_path):
    """Qt derives a text form of a URL list too (`hasText` is also True for
    a `hasUrls`-only `QMimeData`, measured directly) — checked here so a
    copied image file can never silently paste as its raw path instead of
    being recognised."""
    composer = Composer()
    composer.show()
    composer.set_capabilities(_info(supports_image=True), "")
    image_path = tmp_path / "pic.png"
    image_path.write_bytes(b"data")

    composer._text_edit.insertFromMimeData(_mime_with_file_url(image_path))

    assert str(image_path) not in composer._text_edit.toPlainText()
    assert composer._text_edit.toPlainText() == ""


def test_pasting_a_non_image_file_url_falls_through_to_text(qapp, tmp_path):
    """Only images are this feature's concern — a non-image file reference
    on the clipboard is left to Qt's own default (usually its path, or
    nothing), not silently swallowed as an unrecognised attachment."""
    composer = Composer()
    composer.show()
    composer.set_capabilities(_info(supports_image=True), "")
    other = tmp_path / "notes.txt"
    other.write_text("hi", "utf-8")

    composer._text_edit.insertFromMimeData(_mime_with_file_url(other))

    assert composer._attachments == []


def test_image_and_text_together_the_image_wins(qapp):
    """A clipboard that holds both (some apps put a caption alongside a
    copied image) — measured directly: `hasImage` and `hasText` can both
    be true at once. There is essentially never a meaningful text
    alternative to a picture, so the image is what gets attached, not a
    garbled text insertion."""
    composer = Composer()
    composer.show()
    composer.set_capabilities(_info(supports_image=True), "")
    mime = _mime_with_image()
    mime.setText("a caption, incidentally")

    composer._text_edit.insertFromMimeData(mime)

    assert len(composer._attachments) == 1
    assert composer._text_edit.toPlainText() == ""


def test_pasted_image_over_the_cap_is_rejected_with_a_clear_reason(qapp, monkeypatch):
    monkeypatch.setattr(composer_mod, "_MAX_ATTACHMENT_BYTES", 4)
    composer = Composer()
    composer.show()
    composer.set_capabilities(_info(supports_image=True), "")
    rejected = []
    composer.attachment_rejected.connect(rejected.append)

    composer._text_edit.insertFromMimeData(_mime_with_image())

    assert composer._attachments == []
    assert rejected and "too large" in rejected[0].lower()


def test_attachment_rejection_reason_distinguishes_too_large_from_unsupported(tmp_path, monkeypatch):
    small = tmp_path / "small.png"
    small.write_bytes(b"tiny")
    big = tmp_path / "big.png"
    big.write_bytes(b"not actually huge, just over the lowered cap")

    assert attachment_rejection_reason(small, _info(supports_image=True)) is None
    assert attachment_rejection_reason(small, _info()) == "unsupported"

    monkeypatch.setattr(composer_mod, "_MAX_ATTACHMENT_BYTES", 4)
    assert attachment_rejection_reason(big, _info(supports_image=True)) == "too large"


def test_attach_dialog_rejection_message_groups_by_reason(qapp, tmp_path, monkeypatch):
    """"This agent can't take X" is the wrong thing to say about a file
    the agent would gladly take if it were smaller — the two reasons get
    two clauses, not one blanket message."""
    monkeypatch.setattr(composer_mod, "_MAX_ATTACHMENT_BYTES", 4)
    composer = Composer()
    composer.show()
    composer.set_capabilities(_info(supports_image=True), "")
    rejected = []
    composer.attachment_rejected.connect(rejected.append)

    big = tmp_path / "big.png"
    big.write_bytes(b"more than four bytes of image data")
    other = tmp_path / "scene.hip"
    other.write_bytes(b"not an image at all")

    composer._emit_attachment_rejections([big, other])

    assert len(rejected) == 1
    assert "big.png" in rejected[0] and "too large" in rejected[0].lower()
    assert "scene.hip" in rejected[0] and "can't take" in rejected[0]


def test_image_block_from_qimage_round_trips_real_pixels():
    pixmap = QtGui.QPixmap(4, 4)
    pixmap.fill(QtGui.QColor("blue"))
    block = _image_block_from_qimage(pixmap.toImage())

    assert block["type"] == "image"
    assert block["mimeType"] == "image/png"
    decoded = QtGui.QImage()
    decoded.loadFromData(base64.b64decode(block["data"]), "PNG")
    assert decoded.size() == pixmap.toImage().size()


def test_image_block_from_qimage_none_over_the_cap(monkeypatch):
    monkeypatch.setattr(composer_mod, "_MAX_ATTACHMENT_BYTES", 4)
    pixmap = QtGui.QPixmap(64, 64)
    pixmap.fill(QtGui.QColor("blue"))
    assert _image_block_from_qimage(pixmap.toImage()) is None


# --- busy / cancel --------------------------------------------------------------


def test_set_busy_turns_send_button_into_stop_and_emits_cancelled(qapp):
    composer = Composer()
    composer.show()
    received_submit = []
    received_cancel = []
    composer.submitted.connect(received_submit.append)
    composer.cancelled.connect(lambda: received_cancel.append(True))

    composer.set_busy(True)
    _type_text(composer._text_edit, "should be ignored")
    composer._send_button.click()

    assert received_submit == []
    assert received_cancel == [True]


# --- input blocking -------------------------------------------------------------


def test_block_input_disables_only_text_and_send(qapp):
    composer = Composer()
    composer.show()
    composer.set_capabilities(_info(supports_image=True), "")

    assert not composer.is_input_blocked()
    composer.block_input("Update required")

    assert composer.is_input_blocked()
    assert not composer._text_edit.isEnabled()
    assert not composer._send_button.isEnabled()
    # The mode chip is untouched by input blocking.
    assert composer.mode_chip.isEnabled()

    composer.unblock_input()
    assert not composer.is_input_blocked()
    assert composer._text_edit.isEnabled()
    assert composer._send_button.isEnabled()


def test_blocked_input_does_not_submit_on_enter(qapp):
    composer = Composer()
    composer.show()
    received = []
    composer.submitted.connect(received.append)

    _type_text(composer._text_edit, "text")
    composer.block_input("please wait")
    # The field is disabled, so Enter goes straight to the handler:
    # QTest cannot click a disabled widget realistically.
    composer._submit()

    assert received == []


# --- token counter ----------------------------------------------------------------


def test_set_usage_shows_compact_count_and_hides_on_none(qapp):
    composer = Composer()
    composer.show()
    composer.set_usage(Usage(total_tokens=1234))
    assert composer._usage_label.isVisible()
    assert composer._usage_label.text() == "1.2K"

    composer.set_usage(None)
    assert not composer._usage_label.isVisible()


def test_set_usage_shows_used_over_size_for_the_real_acp_shape(qapp):
    """The real `usage_update` has `used`/`size`, never `total_tokens` — see
    docs/facts/acp-sdk.md §4 (`_UsageUpdate`). That's the shape that was
    silently reading as "0" in the live panel before this fix."""
    from types import SimpleNamespace

    composer = Composer()
    composer.show()
    composer.set_usage(SimpleNamespace(used=12_345, size=200_000))
    assert composer._usage_label.isVisible()
    assert composer._usage_label.text() == "12.3K/200K"


# --- slash commands ---------------------------------------------------------------


# --- _parse_enum_hint: the conservative <a|b|c> / [a|b] recognizer ----------------
# Every real hint here is verbatim from a live agent (docs/facts/acp-sdk.md §8).


@pytest.mark.parametrize(
    "hint, expected",
    [
        ("<low|medium|high|xhigh|max|ultracode|auto>", ["low", "medium", "high", "xhigh", "max", "ultracode", "auto"]),
        ("[on|off]", ["on", "off"]),
        ("[red|blue|green|yellow|purple|orange|pink|cyan|default]",
         ["red", "blue", "green", "yellow", "purple", "orange", "pink", "cyan", "default"]),
        ("  [on|off]  ", ["on", "off"]),  # surrounding whitespace is stripped
    ],
)
def test_parse_enum_hint_recognizes_bracketed_alternatives(hint, expected):
    assert _parse_enum_hint(hint) == expected


@pytest.mark.parametrize(
    "hint",
    [
        "<model>",  # a placeholder, not an enum — no "|" to choose between
        "[name]",
        "key=value",  # no brackets at all
        "optional review instructions",
        "<optional custom summarization instructions>",  # free text, not a grammar
        "[reconnect|enable|disable [<server>|all]]",  # nested brackets, a space inside a segment
        "<a| b>",  # a space inside one alternative
        "<a|>",  # an empty alternative
        "",
    ],
)
def test_parse_enum_hint_rejects_everything_else(hint):
    assert _parse_enum_hint(hint) is None


def _commands() -> list[AvailableCommand]:
    return [
        AvailableCommand(name="model", description="change the model"),
        AvailableCommand(name="mode", description="change the mode"),
        AvailableCommand(name="clear", description="clear"),
    ]


def test_slash_popup_shows_and_filters(qapp):
    composer = Composer()
    composer.show()
    composer.set_commands(_commands())

    _type_text(composer._text_edit, "/mo")
    assert composer._popup.isVisible()
    names = [composer._popup.item(i).data(QtCore.Qt.UserRole) for i in range(composer._popup.count())]
    assert names == ["model", "mode"]


def test_slash_popup_finds_a_marketplace_command_by_a_word_inside_its_name(qapp):
    """A prefix-only filter can never find "$may-hub:sync" by typing "sync"
    or "hub" — nobody types a literal "$" first. Real, measured problem at
    ~140 commands on one account (docs/facts/acp-sdk.md §8)."""
    composer = Composer()
    composer.show()
    composer.set_commands(_commands() + [AvailableCommand(name="$may-hub:sync", description="sync")])

    _type_text(composer._text_edit, "/hub")
    names = [composer._popup.item(i).data(QtCore.Qt.UserRole) for i in range(composer._popup.count())]
    assert names == ["$may-hub:sync"]


def test_slash_popup_ranks_prefix_matches_before_contains_matches(qapp):
    composer = Composer()
    composer.show()
    composer.set_commands(
        [
            AvailableCommand(name="$contains-model-in-the-middle", description=""),
            AvailableCommand(name="model", description="change the model"),
        ]
    )
    _type_text(composer._text_edit, "/model")
    names = [composer._popup.item(i).data(QtCore.Qt.UserRole) for i in range(composer._popup.count())]
    assert names == ["model", "$contains-model-in-the-middle"]


def test_marketplace_command_is_tagged_only_by_the_dollar_prefix():
    """The one structural marker any agent actually gives — Codex's own
    `$` prefix. No name-based guessing for agents that give none."""
    from types import SimpleNamespace

    assert _is_marketplace_command(SimpleNamespace(name="$may-hub:sync")) is True
    assert _is_marketplace_command(SimpleNamespace(name="ab-testing")) is False
    assert _is_marketplace_command(SimpleNamespace(name="model")) is False


def test_slash_popup_follows_the_theme_accent(qapp):
    """`::item:selected` used to be a fixed dark grey — it has to come from
    the live theme's own popup-hover tone (`theme.popup_hover_background`)."""
    palette = QtGui.QPalette()
    palette.setColor(QtGui.QPalette.AlternateBase, QtGui.QColor("#223344"))
    qapp.setPalette(palette)

    composer = Composer()
    composer.set_commands(_commands())

    expected = theme.to_hex(theme.popup_background())
    assert expected in composer._popup.styleSheet()


def test_slash_popup_is_scrollbar_free_panel_overlay(qapp):
    host = QtWidgets.QWidget()
    host.resize(800, 700)
    composer = Composer(host)
    composer.setGeometry(32, 520, 736, 160)
    host.show()
    composer.show()
    composer.set_commands(_commands())

    _type_text(composer._text_edit, "/")

    assert composer._popup.parentWidget() is host
    assert composer._popup.horizontalScrollBarPolicy() == QtCore.Qt.ScrollBarAlwaysOff
    assert composer._popup.verticalScrollBarPolicy() == QtCore.Qt.ScrollBarAlwaysOff
    assert composer._popup.y() >= 0
    assert composer._popup.geometry().bottom() < composer._text_edit.mapTo(
        host, QtCore.QPoint()
    ).y()


def test_slash_popup_hidden_without_matching_commands(qapp):
    composer = Composer()
    composer.show()
    composer.set_commands(_commands())
    _type_text(composer._text_edit, "/zzz")
    assert not composer._popup.isVisible()


def test_slash_popup_hidden_after_space(qapp):
    """True only because none of `_commands()`'s fixtures declare an
    `input` hint — a command WITH one keeps the popup open past the space
    to show it (`test_slash_popup_shows_the_hint_for_a_commands_argument`
    and friends, below)."""
    composer = Composer()
    composer.show()
    composer.set_commands(_commands())
    _type_text(composer._text_edit, "/model ")
    assert not composer._popup.isVisible()


def _command_with_input(name: str, hint: str, description: str = ""):
    """A duck-typed `AvailableCommand` carrying an `input.hint`, shaped like
    ACP's real `AvailableCommandInput` (`.input.root.hint`) — not
    `sessions.AvailableCommand`, which has no `input` field at all (see
    `docs/facts/acp-sdk.md` §8)."""
    from types import SimpleNamespace

    return SimpleNamespace(
        name=name,
        description=description,
        input=SimpleNamespace(root=SimpleNamespace(hint=hint)),
    )


def test_slash_popup_shows_the_hint_for_a_commands_argument(qapp):
    """A free-text hint (no `<a|b|c>` shape) is read-only guidance — shown,
    but the popup does not become keyboard-interactive for it."""
    composer = Composer()
    composer.show()
    composer.set_commands(
        [_command_with_input("compact", "optional custom summarization instructions")]
    )
    _type_text(composer._text_edit, "/compact ")

    assert composer._popup.isVisible()
    assert not composer._text_edit.popup_active
    assert composer._popup.current_name() is None  # nothing selectable


def test_slash_popup_offers_selectable_values_for_an_enum_hint(qapp):
    composer = Composer()
    composer.show()
    composer.set_commands(
        [_command_with_input("effort", "<low|medium|high|xhigh|max|ultracode|auto>")]
    )
    _type_text(composer._text_edit, "/effort ")

    assert composer._popup.isVisible()
    assert composer._text_edit.popup_active
    assert composer._popup.current_name() == "low"


def test_accepting_an_argument_choice_inserts_it_after_the_command_name(qapp):
    composer = Composer()
    composer.show()
    composer.set_commands([_command_with_input("fast", "[on|off]")])
    _type_text(composer._text_edit, "/fast ")
    composer._text_edit.navigate_requested.emit(1)
    assert composer._popup.current_name() == "off"

    _press_enter(composer._text_edit)

    assert not composer._popup.isVisible()
    assert composer._text_edit.toPlainText() == "/fast off"


def test_slash_popup_hidden_for_an_unknown_command_name(qapp):
    """A space after something that isn't a real command name is just
    text — there is no command to hint an argument for."""
    composer = Composer()
    composer.show()
    composer.set_commands([_command_with_input("effort", "<low|high>")])
    _type_text(composer._text_edit, "/not-a-real-command ")
    assert not composer._popup.isVisible()


def test_slash_popup_navigation_and_enter_selects(qapp):
    composer = Composer()
    composer.show()
    composer.set_commands(_commands())
    _type_text(composer._text_edit, "/mo")
    assert composer._popup.current_name() == "model"

    composer._text_edit.navigate_requested.emit(1)
    assert composer._popup.current_name() == "mode"

    _press_enter(composer._text_edit)
    assert not composer._popup.isVisible()
    assert composer._text_edit.toPlainText() == "/mode "


def test_slash_popup_escape_closes_without_changing_text(qapp):
    composer = Composer()
    composer.show()
    composer.set_commands(_commands())
    _type_text(composer._text_edit, "/mo")

    QtTest.QTest.keyClick(composer._text_edit, QtCore.Qt.Key_Escape)

    assert not composer._popup.isVisible()
    assert composer._text_edit.toPlainText() == "/mo"


def test_slash_command_sent_as_plain_text(qapp):
    """A command goes to the agent as plain text — we invent no semantics."""
    composer = Composer()
    composer.show()
    composer.set_commands(_commands())
    received = []
    composer.submitted.connect(received.append)

    _type_text(composer._text_edit, "/clear")
    _press_enter(composer._text_edit)  # picks "/clear " from the popup
    _press_enter(composer._text_edit)  # sends it as text

    assert received == [[{"type": "text", "text": "/clear"}]]


# --- growing input --------------------------------------------------------------


def test_text_edit_grows_with_more_lines_then_caps(qapp):
    composer = Composer()
    composer.show()
    composer.show()
    single_line_height = composer._text_edit.height()

    _type_text(composer._text_edit, "1")
    for _ in range(4):
        _press_enter(composer._text_edit, shift=True)
        _type_text(composer._text_edit, "x")
    multi_line_height = composer._text_edit.height()
    assert multi_line_height > single_line_height

    for _ in range(20):
        _press_enter(composer._text_edit, shift=True)
        _type_text(composer._text_edit, "x")
    capped_height = composer._text_edit.height()
    assert capped_height <= multi_line_height * 3  # grows to a ceiling, not forever


# --- agent-side config options (the model picker) ----------------------------


def _option(option_id="model", current="a", choices=(("a", "A"), ("b", "B")), description=""):
    from types import SimpleNamespace

    def _choice(spec):
        # (value, name) or (value, name, description) — most tests don't
        # care about a choice's own description, so it stays optional here.
        value, name, *rest = spec
        return SimpleNamespace(value=value, name=name, description=rest[0] if rest else "")

    return SimpleNamespace(
        id=option_id,
        name=option_id.title(),
        description=description,
        current_value=current,
        choices=tuple(_choice(c) for c in choices),
    )


def test_config_chips_appear_only_for_what_the_agent_sent(qapp):
    composer = Composer()
    composer.show()
    assert composer._config_chips == []

    composer.set_config_options([_option()])
    assert len(composer._config_chips) == 1
    assert composer._config_bar.isVisible()

    composer.set_config_options([])
    assert composer._config_chips == []
    assert not composer._config_bar.isVisible()


def test_config_chip_starts_on_the_agents_current_value(qapp):
    composer = Composer()
    composer.show()
    composer.set_config_options([_option(current="b")])
    assert composer._config_chips[0].currentData() == "b"


def test_single_choice_option_draws_no_chip(qapp):
    """A dropdown with one entry is a label pretending to be a control."""
    composer = Composer()
    composer.show()
    composer.set_config_options([_option(choices=(("only", "Only"),))])
    assert composer._config_chips == []


def test_choosing_a_config_value_reports_id_and_value(qapp):
    composer = Composer()
    composer.show()
    composer.set_config_options([_option(option_id="effort", current="a")])
    received: list[tuple[str, str]] = []
    composer.config_option_selected.connect(lambda cid, value: received.append((cid, value)))

    composer._config_chips[0]._choose(1)

    assert received == [("effort", "b")]


def test_chip_tooltip_is_the_current_choices_own_description(qapp):
    """"Default (recommended)" names nothing on its own — the agent's own
    description of that choice ("Opus 5 with 1M context…") is what actually
    answers "what model is this", and it must not be replaced with our own
    words."""
    composer = Composer()
    composer.show()
    composer.set_config_options(
        [
            _option(
                current="default",
                choices=(
                    ("default", "Default (recommended)", "Opus 5 with 1M context"),
                    ("sonnet", "Sonnet", "Efficient for routine tasks"),
                ),
                description="AI model to use",
            )
        ]
    )
    chip = composer._config_chips[0]
    assert chip._button.toolTip() == "Opus 5 with 1M context"


def test_chip_tooltip_falls_back_to_the_options_own_description(qapp):
    """A choice with no description of its own (Claude's effort levels, for
    instance) still needs SOME tooltip — the option's, same as before this
    was per-choice."""
    composer = Composer()
    composer.show()
    composer.set_config_options(
        [_option(option_id="effort", current="a", description="Available effort levels")]
    )
    chip = composer._config_chips[0]
    assert chip._button.toolTip() == "Available effort levels"


def test_popup_shows_each_choices_description_as_a_second_line(qapp):
    composer = Composer()
    composer.show()
    composer.set_config_options(
        [
            _option(
                option_id="model",
                current="default",
                description="AI model to use",
                choices=(
                    ("default", "Default (recommended)", "Opus 5 with 1M context"),
                    ("sonnet", "Sonnet", ""),
                ),
            )
        ]
    )
    chip = composer._config_chips[0]
    assert chip._items[0] == ("Default (recommended)", "default", "Opus 5 with 1M context")
    # No description of its own — falls back to the OPTION's description,
    # same chain `set_config_options` uses for the chip's own tooltip.
    assert chip._items[1] == ("Sonnet", "sonnet", "AI model to use")


def test_hiding_the_composer_takes_the_slash_palette_with_it(qapp):
    """The palette is reparented to the panel so it isn't clipped, which also
    means hiding the composer stopped hiding it: switching to settings left a
    command list floating over the form."""
    host = QtWidgets.QWidget()
    layout = QtWidgets.QVBoxLayout(host)
    composer = Composer(host)
    layout.addWidget(composer)
    host.resize(800, 400)
    host.show()
    qapp.processEvents()

    composer.set_commands([AvailableCommand(name="clear", description="clear")])
    _type_text(composer._text_edit, "/cl")
    qapp.processEvents()
    assert composer._popup.isVisible()

    composer.setVisible(False)
    qapp.processEvents()

    assert not composer._popup.isVisible()
    assert composer._text_edit.popup_active is False


def _choice(value: str, name: str, description: str = ""):
    class _C:
        pass

    c = _C()
    c.value, c.name, c.description = value, name, description
    return c


def test_a_model_listed_twice_under_one_description_appears_once(qapp):
    """Claude offers "Default (recommended)" and "Opus (1M context)" with the
    identical description, because they are the same model. Listed as sent,
    the picker asks the artist to choose between a thing and itself — Claude
    Code's own picker shows four models and no defaults."""
    from houdini_agent_panel.ui.composer import _named_choices

    opus = "Opus 5 with 1M context · Best for everyday, complex tasks"
    choices = [
        _choice("default", "Default (recommended)", opus),
        _choice("opus[1m]", "Opus (1M context)", opus),
        _choice("sonnet", "Sonnet", "Sonnet 5 · Efficient for routine tasks"),
    ]

    kept, current = _named_choices(choices, "default")

    assert [c.name for c in kept] == ["Opus (1M context)", "Sonnet"], (
        "the alias survived instead of the model it points at"
    )
    assert current == "opus[1m]", (
        "the artist was on the alias; the chip must select what replaced it, "
        "or it shows an empty label"
    )


def test_choices_without_descriptions_are_left_exactly_as_sent(qapp):
    """The rule keys on descriptions matching — the agent's own word that two
    entries are the same. With no descriptions there is nothing to compare and
    nothing may be removed."""
    from houdini_agent_panel.ui.composer import _named_choices

    choices = [_choice("low", "Low"), _choice("high", "High"), _choice("max", "Max")]
    kept, current = _named_choices(choices, "high")

    assert [c.name for c in kept] == ["Low", "High", "Max"]
    assert current == "high"


def test_distinct_models_are_never_collapsed(qapp):
    from houdini_agent_panel.ui.composer import _named_choices

    choices = [
        _choice("opus[1m]", "Opus (1M context)", "Opus 5 · complex tasks"),
        _choice("fable", "Fable", "Fable 5 · hardest tasks"),
        _choice("haiku", "Haiku", "Haiku 4.5 · quick answers"),
    ]
    kept, _ = _named_choices(choices, "fable")
    assert len(kept) == 3

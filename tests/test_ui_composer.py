"""Composer tests: field growth, capability gating, slash popup, attachments, blocking."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from houdini_agent_panel.client import AgentInfo
from houdini_agent_panel.sessions import AvailableCommand, SessionMode, Usage
from houdini_agent_panel.ui.composer import Composer, build_attachment_block


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
    composer = Composer()
    composer.show()
    composer.set_commands(_commands())
    _type_text(composer._text_edit, "/model ")
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

    return SimpleNamespace(
        id=option_id,
        name=option_id.title(),
        description=description,
        current_value=current,
        choices=tuple(SimpleNamespace(value=v, name=n) for v, n in choices),
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
    composer.set_config_options([_option(option_id="reasoning", current="a")])
    received: list[tuple[str, str]] = []
    composer.config_option_selected.connect(lambda cid, value: received.append((cid, value)))

    composer._config_chips[0]._choose(1)

    assert received == [("reasoning", "b")]


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

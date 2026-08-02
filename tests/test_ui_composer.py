"""Тесты Composer: рост поля, capability-гейтинг, слеш-попап, вложения, блокировка."""

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
    """Вставить текст в поле ввода, вызвав `textChanged` как от реального ввода.

    `QtTest.QTest.keyClicks` не годится: она умеет только ASCII (падает
    `ASSERT` на кириллице в `qasciikey.cpp`), а тесты нарочно проверяют
    кириллический текст — то, чем реально пользуется художник.
    """
    edit.setFocus()
    cursor = edit.textCursor()
    cursor.insertText(text)


def _press_enter(edit: QtWidgets.QWidget, *, shift: bool = False) -> None:
    modifiers = QtCore.Qt.ShiftModifier if shift else QtCore.Qt.NoModifier
    QtTest.QTest.keyClick(edit, QtCore.Qt.Key_Return, modifiers)


# --- capability-гейтинг -------------------------------------------------------


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
    # Бэкенд записи подделываем — тест про видимость кнопки, не про реальный микрофон.
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

    composer.mode_chip.set_modes([SessionMode("code", "Код"), SessionMode("ask", "Вопрос")], "code")
    assert composer.mode_chip.isVisible()

    composer.mode_chip.set_modes([], None)
    assert not composer.mode_chip.isVisible()


def test_mode_chip_selection_forwards_to_composer_signal(qapp):
    composer = Composer()
    composer.show()

    composer.mode_chip.set_modes([SessionMode("code", "Код")], "code")
    received = []
    composer.mode_selected.connect(received.append)
    composer.mode_chip.mode_selected.emit("code")
    assert received == ["code"]


def test_composer_set_modes_is_a_facade_over_mode_chip(qapp):
    """Панель кормит режимы через `Composer.set_modes`, не дотягиваясь до
    вложенного `mode_chip` напрямую (architecture.md §10)."""
    composer = Composer()
    composer.show()
    assert not composer.mode_chip.isVisible()

    composer.set_modes([SessionMode("code", "Код"), SessionMode("ask", "Вопрос")], "ask")
    assert composer.mode_chip.isVisible()

    received = []
    composer.mode_selected.connect(received.append)
    composer.mode_chip.mode_selected.emit("code")
    assert received == ["code"]

    composer.set_modes([], None)
    assert not composer.mode_chip.isVisible()


# --- отправка текста ----------------------------------------------------------


def test_enter_submits_text_block(qapp):
    composer = Composer()
    composer.show()
    received = []
    composer.submitted.connect(received.append)

    _type_text(composer._text_edit, "привет")
    _press_enter(composer._text_edit)

    assert received == [[{"type": "text", "text": "привет"}]]
    assert composer._text_edit.toPlainText() == ""


def test_shift_enter_inserts_newline_without_submitting(qapp):
    composer = Composer()
    composer.show()
    received = []
    composer.submitted.connect(received.append)

    _type_text(composer._text_edit, "строка1")
    _press_enter(composer._text_edit, shift=True)
    _type_text(composer._text_edit, "строка2")

    assert received == []
    assert composer._text_edit.toPlainText() == "строка1\nстрока2"


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
    _type_text(composer._text_edit, "смотри")
    _press_enter(composer._text_edit)

    assert len(received) == 1
    blocks = received[0]
    assert blocks[0] == {"type": "text", "text": "смотри"}
    assert blocks[1]["type"] == "image"
    assert blocks[1]["mimeType"] == "image/png"
    assert base64.b64decode(blocks[1]["data"]) == image_path.read_bytes()
    # Вложения и текст очищаются после отправки.
    assert composer._text_edit.toPlainText() == ""


def test_add_attachment_without_capability_is_rejected(qapp, tmp_path):
    composer = Composer()
    composer.show()
    composer.set_capabilities(_info(), "")  # ни image, ни embeddedContext
    path = tmp_path / "pic.png"
    path.write_bytes(b"data")
    assert composer.add_attachment(path) is False


# --- build_attachment_block напрямую -------------------------------------------


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
    _type_text(composer._text_edit, "должно быть проигнорировано")
    composer._send_button.click()

    assert received_submit == []
    assert received_cancel == [True]


# --- блокировка ввода -----------------------------------------------------------


def test_block_input_disables_only_text_and_send(qapp):
    composer = Composer()
    composer.show()
    composer.set_capabilities(_info(supports_image=True), "")

    assert not composer.is_input_blocked()
    composer.block_input("Обновление обязательно")

    assert composer.is_input_blocked()
    assert not composer._text_edit.isEnabled()
    assert not composer._send_button.isEnabled()
    # Мод-чип блокировкой ввода не затрагивается.
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

    _type_text(composer._text_edit, "текст")
    composer.block_input("подождите")
    # Поле выключено — искусственно шлём Enter напрямую в обработчик,
    # т.к. QTest не может кликнуть в disabled-виджет реалистично.
    composer._submit()

    assert received == []


# --- счётчик токенов --------------------------------------------------------------


def test_set_usage_shows_compact_count_and_hides_on_none(qapp):
    composer = Composer()
    composer.show()
    composer.set_usage(Usage(total_tokens=1234))
    assert composer._usage_label.isVisible()
    assert composer._usage_label.text() == "1.2K"

    composer.set_usage(None)
    assert not composer._usage_label.isVisible()


# --- слеш-команды -----------------------------------------------------------------


def _commands() -> list[AvailableCommand]:
    return [
        AvailableCommand(name="model", description="сменить модель"),
        AvailableCommand(name="mode", description="сменить режим"),
        AvailableCommand(name="clear", description="очистить"),
    ]


def test_slash_popup_shows_and_filters(qapp):
    composer = Composer()
    composer.show()
    composer.set_commands(_commands())

    _type_text(composer._text_edit, "/mo")
    assert composer._popup.isVisible()
    names = [composer._popup.item(i).data(QtCore.Qt.UserRole) for i in range(composer._popup.count())]
    assert names == ["model", "mode"]


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
    """Команда уходит агенту обычным текстом — своей семантики не изобретаем."""
    composer = Composer()
    composer.show()
    composer.set_commands(_commands())
    received = []
    composer.submitted.connect(received.append)

    _type_text(composer._text_edit, "/clear")
    _press_enter(composer._text_edit)  # выбирает "/clear " из попапа
    _press_enter(composer._text_edit)  # отправляет как текст

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
    assert capped_height <= multi_line_height * 3  # растёт до потолка, не бесконечно

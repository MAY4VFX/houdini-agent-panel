"""Composer tests: field growth, capability gating, slash popup, attachments, blocking."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest
from PySide6 import QtCore, QtGui, QtTest, QtWidgets

from houdini_agent_panel.client import AgentInfo
from houdini_agent_panel.sessions import AvailableCommand, SessionMode, Usage
from houdini_agent_panel.ui import theme
from houdini_agent_panel.ui.composer import (
    Composer,
    _is_marketplace_command,
    _parse_enum_hint,
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

"""Attaching files: filters that match capabilities, and no silent drops.

Reported from a live panel: "I tried to pick a file and nothing worked,
some kind of filters or something." Two causes — the dialog offered every
file on disk with no hint about what the agent takes, and a file the agent
couldn't accept was dropped without a word.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from houdini_agent_panel.client import AgentInfo
from houdini_agent_panel.ui.composer import Composer


def _info(**overrides) -> AgentInfo:
    base = dict(
        name="Agent",
        version="1",
        protocol_version=1,
        supports_image=True,
        supports_audio=False,
        supports_embedded_context=True,
        supports_load_session=False,
        supports_logout=False,
        auth_methods=(),
    )
    base.update(overrides)
    return AgentInfo(**base)


@pytest.fixture
def composer(qapp):
    widget = Composer()
    yield widget
    widget.deleteLater()


def test_filter_offers_everything_when_the_agent_takes_embedded_context(composer):
    composer.set_capabilities(_info(), "")

    assert "All files (*)" in composer._attachment_filter()


def test_filter_leads_with_images_when_that_is_all_the_agent_takes(composer):
    composer.set_capabilities(_info(supports_embedded_context=False), "")

    assert composer._attachment_filter().startswith("Images (")


def test_rejected_file_is_reported_not_swallowed(composer, tmp_path):
    """The artist has no way to guess an agent's capabilities — refusing in
    silence is indistinguishable from a broken button."""
    composer.set_capabilities(_info(supports_image=False, supports_embedded_context=False), "")
    messages: list[str] = []
    composer.attachment_rejected.connect(messages.append)

    target = tmp_path / "scene.hip"
    target.write_bytes(b"\x00\x01binary")

    assert composer.add_attachment(target) is False

    composer._on_attach_clicked = None  # not used; the signal path is what matters
    composer.attachment_rejected.emit("This agent can't take: scene.hip")
    assert messages and "scene.hip" in messages[-1]


def test_attaching_without_an_agent_says_so(composer):
    messages: list[str] = []
    composer.attachment_rejected.connect(messages.append)

    composer._on_attach_clicked()

    assert messages, "clicking attach with no agent must explain itself"
    assert "agent" in messages[0].lower()


def test_unreadable_file_does_not_raise(composer, tmp_path):
    composer.set_capabilities(_info(), "")

    missing = tmp_path / "gone.txt"

    assert composer.add_attachment(missing) is False

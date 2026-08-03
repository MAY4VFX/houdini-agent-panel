"""NoticeStrip/BlockingNotice tests: rendering strictly from the data sent."""

from __future__ import annotations

from PySide6 import QtWidgets

from houdini_agent_panel.announcements import Announcement, Button
from houdini_agent_panel.updates import Update
from houdini_agent_panel.ui.announcement import BlockingNotice, NoticeStrip


def test_notice_strip_shows_title_and_buttons(qapp):
    strip = NoticeStrip()
    ann = Announcement(
        id="ann-1",
        severity="info",
        title="A new panel version",
        buttons=(Button(label="Details", url="https://example.test"),),
    )
    strip.show_notice(ann)

    assert strip.isVisible()
    assert "A new panel version" in strip._label.text()
    buttons = strip.findChildren(QtWidgets.QPushButton)
    assert [b.text() for b in buttons] == ["Details"]


def test_notice_strip_button_click_emits_action_with_url(qapp):
    strip = NoticeStrip()
    ann = Announcement(
        id="ann-1",
        severity="info",
        title="t",
        buttons=(Button(label="Open", url="https://example.test"),),
    )
    strip.show_notice(ann)

    received = []
    strip.action_clicked.connect(lambda ann_id, url: received.append((ann_id, url)))
    strip.findChild(QtWidgets.QPushButton).click()
    assert received == [("ann-1", "https://example.test")]


def test_notice_strip_close_emits_dismissed_and_hides(qapp):
    strip = NoticeStrip()
    ann = Announcement(id="ann-2", severity="info", title="t")
    strip.show_notice(ann)

    received = []
    strip.dismissed.connect(received.append)
    strip._on_close()

    assert received == ["ann-2"]
    assert not strip.isVisible()


def test_notice_strip_show_update_offers_single_update_button(qapp):
    strip = NoticeStrip()
    update = Update(kind="agent", target="claude-acp", label="Claude Agent 1.2.0", current="1.0.0", latest="1.2.0")
    strip.show_update(update)

    assert strip.isVisible()
    assert "1.0.0" in strip._label.text() and "1.2.0" in strip._label.text()
    buttons = strip.findChildren(QtWidgets.QPushButton)
    assert [b.text() for b in buttons] == ["Update"]

    received = []
    strip.action_clicked.connect(lambda target, url: received.append((target, url)))
    buttons[0].click()
    assert received == [("claude-acp", "")]


def test_notice_strip_switching_notice_replaces_buttons_not_accumulates(qapp):
    strip = NoticeStrip()
    strip.show_notice(Announcement(id="a", severity="info", title="t1", buttons=(Button("B1"),)))
    strip.show_notice(Announcement(id="b", severity="info", title="t2", buttons=(Button("B2"),)))
    buttons = strip.findChildren(QtWidgets.QPushButton)
    assert [b.text() for b in buttons] == ["B2"]


# --- BlockingNotice --------------------------------------------------------------


def test_blocking_notice_renders_title_body_and_buttons_from_announcement(qapp):
    notice = BlockingNotice()
    ann = Announcement(
        id="block-1",
        severity="blocking",
        title="A required update",
        body="Please update the panel.",
        buttons=(Button(label="Got it", url=""), Button(label="Download", url="https://example.test")),
    )
    notice.show_notice(ann)

    assert notice.isVisible()
    assert notice._title.text() == "A required update"
    assert notice._body.text() == "Please update the panel."
    buttons = notice.findChildren(QtWidgets.QPushButton)
    assert [b.text() for b in buttons] == ["Got it", "Download"]


def test_blocking_notice_button_click_emits_action_clicked(qapp):
    notice = BlockingNotice()
    ann = Announcement(id="block-2", severity="blocking", title="t", buttons=(Button("Ok", "https://x"),))
    notice.show_notice(ann)

    received = []
    notice.action_clicked.connect(lambda ann_id, url: received.append((ann_id, url)))
    notice.findChild(QtWidgets.QPushButton).click()
    assert received == [("block-2", "https://x")]


def test_blocking_notice_does_not_invent_its_own_buttons(qapp):
    notice = BlockingNotice()
    ann = Announcement(id="block-3", severity="blocking", title="t")  # no buttons
    notice.show_notice(ann)
    assert notice.findChildren(QtWidgets.QPushButton) == []


def test_blocking_notice_hide_notice(qapp):
    notice = BlockingNotice()
    notice.show_notice(Announcement(id="x", severity="blocking", title="t"))
    assert notice.isVisible()
    notice.hide_notice()
    assert not notice.isVisible()

"""Conversation drawer stays inside the panel and owns session navigation."""

from __future__ import annotations

from houdini_agent_panel.sessions import SessionState
from houdini_agent_panel.ui import conversations as conversations_mod
from houdini_agent_panel.ui import theme
from houdini_agent_panel.ui.conversations import (
    ConversationDrawer,
    empty_scope_text,
    scope_label_text,
    summarize_title,
)
from houdini_agent_panel.ui.qt import QtGui, QtWidgets


def _state(session_id: str, title: str, created_at: float) -> SessionState:
    return SessionState(session_id, title, "/tmp/shot", created_at)


def test_drawer_lists_newest_conversation_first_and_marks_current(qapp):
    host = ConversationDrawer()
    states = [_state("old", "Old chat", 1.0), _state("new", "New chat", 2.0)]
    host.set_sessions(states, "new")

    assert list(host._buttons) == ["new", "old"]
    assert host._buttons["new"].property("currentConversation") is True
    assert host._buttons["old"].property("currentConversation") is False


# --- busy / unread markers -------------------------------------------------


def test_busy_session_shows_the_busy_dot(qapp):
    host = ConversationDrawer()
    busy = _state("s1", "Chat", 1.0)
    busy.busy = True
    idle = _state("s2", "Other chat", 2.0)
    host.set_sessions([busy, idle], "s2")

    assert host._busy_dots["s1"].isHidden() is False
    assert host._busy_dots["s2"].isHidden() is True


def test_unread_session_shows_the_unread_dot(qapp):
    host = ConversationDrawer()
    unread = _state("s1", "Chat", 1.0)
    unread.unread = True
    read = _state("s2", "Other chat", 2.0)
    host.set_sessions([unread, read], "s2")

    assert host._unread_dots["s1"].isHidden() is False
    assert host._unread_dots["s2"].isHidden() is True


def test_busy_and_unread_markers_are_independent(qapp):
    """A session can be both, or either, or neither — they track different things."""
    host = ConversationDrawer()
    both = _state("s1", "Chat", 1.0)
    both.busy = True
    both.unread = True
    host.set_sessions([both], "s1")

    assert host._busy_dots["s1"].isHidden() is False
    assert host._unread_dots["s1"].isHidden() is False


def _sampled_dot_color(label) -> QtGui.QColor:
    pixmap = label.pixmap()
    image = pixmap.toImage()
    center = pixmap.width() // 2
    return image.pixelColor(center, center)


def test_busy_and_unread_dots_follow_the_theme_accent(qapp, monkeypatch):
    monkeypatch.setattr(theme, "accent_color", lambda: QtGui.QColor("#ff33aa"))
    host = ConversationDrawer()
    state = _state("s1", "Chat", 1.0)
    state.busy = True
    state.unread = True
    host.set_sessions([state], "s2")

    assert _sampled_dot_color(host._busy_dots["s1"]) == QtGui.QColor("#ff33aa")
    assert _sampled_dot_color(host._unread_dots["s1"]) == QtGui.QColor("#ff33aa")


def test_pin_icon_color_follows_the_theme_accent(qapp):
    palette = QtGui.QPalette()
    palette.setColor(QtGui.QPalette.Highlight, QtGui.QColor("#ff33aa"))
    qapp.setPalette(palette)

    host = ConversationDrawer()

    assert theme.to_hex(QtGui.QColor("#ff33aa")) in host.styleSheet()


def test_selecting_a_conversation_leaves_the_drawer_open(qapp):
    """It used to close, on the reasoning that the artist asked for this one
    conversation and now wants to read it. Wrong model of the thing: this is
    a panel, not a menu, and reading one conversation is very often followed
    by reading another. The toggle in the header is there for whoever wants
    it gone."""
    from PySide6 import QtWidgets

    parent = QtWidgets.QWidget()
    parent.resize(900, 700)
    drawer = ConversationDrawer(parent)
    drawer.set_sessions([_state("s1", "Chat", 1.0)], "s1")
    selected = []
    drawer.session_selected.connect(selected.append)
    drawer.open_drawer()
    drawer._animation.stop()
    drawer.move(0, 0)

    drawer._buttons["s1"].click()

    assert selected == ["s1"]
    assert drawer._closing is False


def test_new_chat_leaves_the_drawer_open(qapp):
    """Picking an existing conversation closes the drawer — that one was
    asked for and is about to be read. Asking for a NEW chat is the action
    most likely to be repeated, and closing the list means reopening it to
    do the same thing again. The new chat appears in the list the artist is
    still looking at."""
    drawer = ConversationDrawer()
    drawer.open_drawer()
    qapp.processEvents()

    seen: list[int] = []
    drawer.new_session_clicked.connect(lambda: seen.append(1))
    drawer._on_new_session()
    qapp.processEvents()

    assert seen == [1]
    assert not drawer._closing, "the drawer started closing on a new chat"


def test_drawer_is_child_overlay_not_native_window(qapp):
    from PySide6 import QtWidgets

    parent = QtWidgets.QWidget()
    drawer = ConversationDrawer(parent)

    assert drawer.parentWidget() is parent
    assert not drawer.isWindow()


# --- naming --------------------------------------------------------------


def test_summarize_title_empty_message_reads_as_new_conversation():
    assert summarize_title("") == "New chat"
    assert summarize_title("   \n  ") == "New chat"


def test_summarize_title_keeps_a_short_first_line_verbatim():
    assert summarize_title("Fix the wet rock material") == "Fix the wet rock material"
    # Only the first line counts — the rest of the message isn't the name.
    assert summarize_title("Fix the material\nand check the shader too") == "Fix the material"


def test_summarize_title_cuts_at_a_word_boundary_not_mid_word():
    long_text = "Set up a full pyro simulation with a custom velocity field chain"
    title = summarize_title(long_text, limit=20)

    assert title.endswith("…")
    assert len(title) <= 21  # 20 chars + the ellipsis marker
    # Every word before the cut is a whole word straight from the source —
    # a bare `text[:20]` would slice "velocity" into "veloc".
    words = title[:-1].rstrip().split(" ")
    assert long_text.split(" ")[: len(words)] == words


# --- pin / rename / delete -------------------------------------------------


def test_pinning_moves_a_conversation_to_the_top_and_survives_a_rebuild(qapp):
    host = ConversationDrawer()
    states = [_state("old", "Old chat", 1.0), _state("new", "New chat", 2.0)]
    host.set_sessions(states, "new")
    assert list(host._buttons) == ["new", "old"]

    host._pin_buttons["old"].click()

    assert list(host._buttons) == ["old", "new"]
    assert host._pin_buttons["old"].property("pinned") is True

    # A later refresh (e.g. a new message arriving) must not forget the pin.
    host.set_sessions(states, "new")
    assert list(host._buttons) == ["old", "new"]


def test_rebuilding_the_row_list_hides_the_stale_widget_immediately(qapp):
    """`deleteLater` only runs on a later event-loop pass — the outgoing row
    must not stay visible in the meantime. It used to bleed through the
    freshly built layout whenever two rebuilds happened back to back (e.g.
    pinning right after the initial `set_sessions`)."""
    host = ConversationDrawer()
    host.show()
    qapp.processEvents()
    states = [_state("old", "Old chat", 1.0), _state("new", "New chat", 2.0)]
    host.set_sessions(states, "new")
    qapp.processEvents()
    stale_row = host._buttons["old"].parentWidget()
    assert stale_row.isVisible() is True

    host._toggle_pin("old")

    assert stale_row.isVisible() is False


def test_deleting_the_last_conversation_disconnects_its_row_buttons(qapp):
    """A real, reproducible native crash lived here: rebuilding the list
    down to EMPTY (the shape "delete the last conversation" makes) could
    later segfault in unrelated code, the first time anything else pumped
    the event loop hard. Confirmed with lldb, not guessed:
    `deleteChildren()`, cascading from a row's own deferred delete,
    crashed inside `QObjectPrivate::setParent_helper` while tearing down a
    `QToolButton` (the row's pin/more button) that still had a live
    `clicked` connection to a lambda closing over the drawer. Isolated by
    elimination: shrinking to a SMALLER NON-EMPTY list never crashed;
    deferring the `deleteLater()` call itself changed nothing (it already
    posts its event asynchronously regardless of when it's called); the
    one change that closed it, reliably, was severing the connection
    BEFORE deletion. This test can't reproduce the segfault itself (it
    needed real concurrent QThread activity elsewhere in the full suite to
    manifest) — it verifies the actual fix mechanism directly: the
    outgoing row's buttons have no live connections left by the time
    `deleteLater` is even called.
    """
    # `receivers()` wants Qt's own normalized signal signature, not the
    # bound-signal object — `QAbstractButton.clicked` is `clicked(bool)`
    # for both QPushButton and QToolButton; the leading "2" is Qt's old
    # SIGNAL()-macro normalization prefix, still what `receivers()` wants
    # (measured directly: `receivers("clicked(bool)")` — no prefix —
    # always answers 0, connected or not).
    clicked_signature = "2clicked(bool)"

    host = ConversationDrawer()
    host.set_sessions([_state("s1", "Chat", 1.0)], "s1")
    buttons = list(host._buttons["s1"].parentWidget().findChildren(QtWidgets.QAbstractButton))
    assert buttons, "the row has no buttons to check — test itself is broken"
    assert any(b.receivers(clicked_signature) > 0 for b in buttons), (
        "sanity check: the row's buttons should start out connected"
    )

    host.set_sessions([], None)

    assert all(b.receivers(clicked_signature) == 0 for b in buttons), (
        "a row's button still has a live connection after being removed — "
        "the exact state that crashed deleteChildren() during a later "
        "deferred delete"
    )


def test_renaming_emits_session_renamed_with_the_typed_text(qapp, monkeypatch):
    host = ConversationDrawer()
    host.set_sessions([_state("s1", "Old name", 1.0)], "s1")
    monkeypatch.setattr(
        conversations_mod.QtWidgets.QInputDialog,
        "getText",
        staticmethod(lambda *a, **k: ("New name", True)),
    )
    renamed = []
    host.session_renamed.connect(lambda sid, title: renamed.append((sid, title)))

    host._start_rename("s1", "Old name")

    assert renamed == [("s1", "New name")]


def test_cancelling_rename_emits_nothing(qapp, monkeypatch):
    host = ConversationDrawer()
    monkeypatch.setattr(
        conversations_mod.QtWidgets.QInputDialog,
        "getText",
        staticmethod(lambda *a, **k: ("whatever", False)),
    )
    renamed = []
    host.session_renamed.connect(lambda sid, title: renamed.append((sid, title)))

    host._start_rename("s1", "Old name")

    assert renamed == []


def test_deleting_a_conversation_requires_confirmation_first(qapp, monkeypatch):
    host = ConversationDrawer()
    monkeypatch.setattr(
        conversations_mod.QtWidgets.QMessageBox,
        "question",
        staticmethod(lambda *a, **k: conversations_mod.QtWidgets.QMessageBox.Cancel),
    )
    removed = []
    host.session_removed.connect(removed.append)

    host._confirm_delete("s1", "Old name")

    assert removed == []


def test_confirming_delete_emits_session_removed(qapp, monkeypatch):
    host = ConversationDrawer()
    monkeypatch.setattr(
        conversations_mod.QtWidgets.QMessageBox,
        "question",
        staticmethod(lambda *a, **k: conversations_mod.QtWidgets.QMessageBox.Yes),
    )
    removed = []
    host.session_removed.connect(removed.append)

    host._confirm_delete("s1", "Old name")

    assert removed == ["s1"]


def test_long_title_is_elided_not_left_to_overflow_the_drawer(qapp):
    """A `QPushButton` never elides overflowing text on its own — without
    this the pin/overflow icons used to get pushed clean off the (non-
    scrolling) drawer by a long first message."""
    host = ConversationDrawer()
    long_title = (
        "Set up a full pyro simulation with a custom velocity field and volume shader chain"
    )
    host.set_sessions([_state("s1", long_title, 1.0)], "s1")

    button = host._buttons["s1"]
    assert button.text() != long_title
    assert button.text().endswith("…")
    assert button.toolTip() == long_title


def test_drawer_starts_below_the_header_it_is_toggled_from(qapp):
    """The drawer must never cover the control that closes it.

    The only toggle is the header's sidebar button; a drawer spanning the
    full height sat right on top of it, so an open drawer could not be
    closed from the panel at all.
    """
    from PySide6 import QtWidgets

    parent = QtWidgets.QWidget()
    parent.resize(900, 700)
    drawer = ConversationDrawer(parent)
    drawer.set_top_inset(38)
    drawer.open_drawer()
    drawer._animation.setCurrentTime(drawer._animation.duration())

    assert drawer.y() == 38
    assert drawer.height() == 700 - 38


def test_drawer_reports_its_open_state(qapp):
    from PySide6 import QtWidgets

    parent = QtWidgets.QWidget()
    parent.resize(900, 700)
    drawer = ConversationDrawer(parent)
    states: list[bool] = []
    drawer.open_state_changed.connect(states.append)

    drawer.open_drawer()
    assert drawer.is_open() is True
    drawer.close_drawer()
    assert drawer.is_open() is False

    assert states == [True, False]


def test_the_compose_icon_is_drawn_and_not_blank(qapp):
    """The header's "+" and the drawer's button share one glyph. Drawn from
    the palette rather than shipped as an image, so it survives a theme
    change — and so it needs a test that it actually drew something."""
    from houdini_agent_panel.ui.conversations import compose_icon

    icon = compose_icon()
    assert not icon.isNull()
    pixmap = icon.pixmap(16, 16)
    image = pixmap.toImage()
    painted = sum(
        1
        for x in range(image.width())
        for y in range(image.height())
        if image.pixelColor(x, y).alpha() > 0
    )
    assert painted > 20, f"the glyph is effectively empty: {painted} pixels drawn"


# --- scope label -------------------------------------------------------
#
# The drawer used to show no indication at all of how many conversations
# were being displayed, or that "displayed" meant "for this scene" — an
# artist who opened a folder with real history on disk had no way to tell
# a drawer that legitimately held one conversation apart from one that was
# supposed to hold four and only found one (the restore bug
# `scene.watch_hip_dir_changes` fixed). Deliberately just a count, not a
# repeat of the scene path (`HeaderBar.set_cwd` already shows that above
# the drawer) and deliberately not a "N more elsewhere" figure — measured
# (see `conversations.py`'s own comment): a full, unfiltered
# `conversations_store.load()` costs ~25-30ms at the store's worst-case
# size, cheap once but not something to re-pay on every `set_sessions()`.


def test_scope_label_text_for_zero_one_and_many():
    assert scope_label_text(0) == "No conversations here yet"
    assert scope_label_text(1) == "1 conversation here"
    assert scope_label_text(2) == "2 conversations here"
    assert scope_label_text(11) == "11 conversations here"


# --- empty_scope_text -------------------------------------------------
#
# The measured incident this exists for: the owner opened a scene, saw
# one empty drawer, and reported his conversations missing — twice, both
# times against a CORRECT drawer. Dumping the store: 41 conversations
# scoped to that folder all belonged to a different agent; the 2 that
# belonged to this one lived in a different folder entirely. "No
# conversations here yet" was true and said nothing about why.


def test_empty_scope_text_names_both_filters_with_no_hints():
    assert (
        empty_scope_text("Claude Agent")
        == "No conversations for Claude Agent in this scene folder"
    )


def test_empty_scope_text_falls_back_without_an_agent():
    """Before an agent is chosen there's nothing to name — same wording
    `scope_label_text(0)` already uses, not a second phrase to keep in
    sync."""
    assert empty_scope_text("") == scope_label_text(0)


def test_empty_scope_text_adds_the_other_agents_hint():
    assert empty_scope_text("Claude Agent", other_agents_here=41) == (
        "No conversations for Claude Agent in this scene folder — "
        "41 here for other agents"
    )


def test_empty_scope_text_singular_other_agent():
    assert empty_scope_text("Claude Agent", other_agents_here=1) == (
        "No conversations for Claude Agent in this scene folder — "
        "1 here for another agent"
    )


def test_empty_scope_text_adds_the_elsewhere_hint():
    assert empty_scope_text("Claude Agent", this_agent_elsewhere=2) == (
        "No conversations for Claude Agent in this scene folder — "
        "2 for Claude Agent in other folders"
    )


def test_empty_scope_text_singular_elsewhere():
    assert empty_scope_text("Claude Agent", this_agent_elsewhere=1) == (
        "No conversations for Claude Agent in this scene folder — "
        "1 for Claude Agent in another folder"
    )


def test_empty_scope_text_both_hints_together():
    """The owner's own measured numbers, verbatim."""
    assert empty_scope_text(
        "Claude Agent", other_agents_here=41, this_agent_elsewhere=2
    ) == (
        "No conversations for Claude Agent in this scene folder — "
        "41 here for other agents, 2 for Claude Agent in other folders"
    )


def test_empty_scope_text_zero_hints_are_silent():
    """A genuinely first-ever conversation must not show a suspicious
    "0 elsewhere" — that invites exactly the doubt this whole feature
    exists to prevent."""
    text = empty_scope_text("Claude Agent", other_agents_here=0, this_agent_elsewhere=0)
    assert text == "No conversations for Claude Agent in this scene folder"
    assert "0" not in text


def test_drawer_shows_nothing_here_yet_before_any_sessions(qapp):
    host = ConversationDrawer()
    assert host._scope_label.text() == "No conversations here yet"


def test_drawer_uses_the_supplied_empty_scope_text(qapp):
    host = ConversationDrawer()
    host.set_empty_scope_text("No conversations for Claude Agent in this scene folder")
    host.set_sessions([], None)
    assert host._scope_label.text() == "No conversations for Claude Agent in this scene folder"


def test_drawer_empty_scope_text_does_not_leak_into_a_populated_list(qapp):
    """The rich empty-state text must never bleed into the ordinary,
    populated case — that stays exactly `scope_label_text(len(states))`,
    computed from the drawer's own truth."""
    host = ConversationDrawer()
    host.set_empty_scope_text("No conversations for Claude Agent in this scene folder")
    host.set_sessions([_state("s1", "Chat", 1.0)], "s1")
    assert host._scope_label.text() == "1 conversation here"


def test_drawer_scope_label_tracks_the_shown_session_count(qapp):
    host = ConversationDrawer()
    host.set_sessions([_state("s1", "Chat", 1.0)], "s1")
    assert host._scope_label.text() == "1 conversation here"

    host.set_sessions(
        [_state("s1", "Chat", 1.0), _state("s2", "Other", 2.0)], "s2"
    )
    assert host._scope_label.text() == "2 conversations here"

    # This exact next call — a nonempty drawer going to empty, the same
    # shape as deleting the last conversation — used to reproducibly
    # corrupt native (PySide6/Qt) state here and segfault later, in
    # unrelated code, before `set_sessions` was fixed to disconnect the
    # outgoing rows' buttons rather than leaving Qt's own destructor to
    # walk a live `clicked` connection during `deleteChildren()` (see
    # `set_sessions`'s own comment, and
    # `test_deleting_the_last_conversation_disconnects_its_row_buttons`
    # below for the fix verified directly).
    host.set_sessions([], None)
    assert host._scope_label.text() == "No conversations here yet"

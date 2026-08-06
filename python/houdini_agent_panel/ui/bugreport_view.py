"""The in-panel bug reporter — reachable from the header's "⋯" menu and
from Settings (`AgentPanel._open_bug_report`), never buried.

The feature exists because the owner asked for a button he can press,
type a comment into, and send, landing as a GitHub issue. What he did
NOT choose is a screenshot; what he DID choose, explicitly, is three
attachments: versions/system, the tail of the panel's own log, and the
last few messages of the current conversation.

The part that matters more than the feature: the conversation is the
content of the artist's own work, and this goes to a PUBLIC issue
tracker. So the promise this screen exists to keep is not "reachable" or
"convenient" — it is **nothing is sent that the artist has not had the
chance to read**. Concretely:

- Every piece — title, description, and each attachment — is a real,
  editable text field showing the ACTUAL text, not a summary and not a
  checkbox list of categories. `bugreport.py` gathers and redacts each
  attachment before this screen ever shows it; nothing here decides
  content on the artist's behalf, only lays out what was gathered.
- Each attachment can be removed individually (`_AttachmentSection`'s own
  toggle) — not an all-or-nothing "attach system info" switch.
- The choice is remembered (`Settings.bugreport_attachments`) so someone
  working under NDA who removes the conversation once does not have to
  fight the same control on every report.
- Immediately before sending, EVERYTHING currently in the fields is
  redacted again (`_redact_before_sending`) — catching anything the
  artist typed or pasted themselves, not just what was gathered
  automatically. If that pass changes anything, sending is held: the
  fields are updated with what changed and the artist has to press Send
  again, having now read the exact text that would leave the machine.
- On failure the typed text is never lost, and a "Copy report" button
  offers the exact same text that would have been sent, so it can be
  pasted into GitHub by hand.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .. import bugreport
from . import theme
from .qt import QtCore, QtGui, QtWidgets, Signal
from .worker import release

if TYPE_CHECKING:
    from .bugreport_worker import BugReportWorker

#: Same centred column as the feed, composer, settings, and sign-in screen.
_RAIL_WIDTH = 736
_MIN_RAIL_WIDTH = 180


class _AttachmentSection(QtWidgets.QWidget):
    """One removable attachment: a header (name, a note if it was redacted
    on the way in, a Remove/Restore toggle) and the actual editable text
    underneath — never a checkbox standing in for content the artist
    hasn't seen.
    """

    #: Fired whenever the included/excluded state changes by the artist's
    #: own click — `BugReportView` persists it into Settings right away,
    #: not only when the report is actually sent, so navigating away
    #: without sending still remembers the choice.
    toggled = Signal()

    def __init__(self, name: str, key: str, parent=None) -> None:
        super().__init__(parent)
        self.key = key
        self._included = True

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        header = QtWidgets.QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(6)
        name_label = QtWidgets.QLabel(name, self)
        font = name_label.font()
        font.setBold(True)
        name_label.setFont(font)
        header.addWidget(name_label)
        self._redacted_label = QtWidgets.QLabel("", self)
        self._redacted_label.setStyleSheet("color: palette(disabled, text);")
        header.addWidget(self._redacted_label)
        header.addStretch(1)
        self._toggle_button = QtWidgets.QPushButton("Remove", self)
        self._toggle_button.clicked.connect(self._on_toggle_clicked)
        header.addWidget(self._toggle_button)
        outer.addLayout(header)

        self._text_edit = QtWidgets.QTextEdit(self)
        self._text_edit.setAcceptRichText(False)
        self._text_edit.setFont(theme.monospace_font())
        # `WrapAnywhere`, not the default word-boundary wrap: a log line or
        # a raw `PATH=` dump can be one unbroken run of non-space
        # characters far wider than this column — measured live, that
        # forced the WHOLE screen to scroll sideways to fit one line of
        # one attachment. Wrapping mid-word here is what keeps the
        # overflow inside this box instead.
        self._text_edit.setWordWrapMode(QtGui.QTextOption.WrapAnywhere)
        self._text_edit.setMinimumHeight(80)
        self._text_edit.setMaximumHeight(160)
        outer.addWidget(self._text_edit)

    # --- public ----------------------------------------------------------

    def set_text(self, text: str, *, redacted: bool = False) -> None:
        self._text_edit.setPlainText(text)
        self._redacted_label.setText("redacted before showing" if redacted else "")

    def text(self) -> str:
        return self._text_edit.toPlainText()

    def is_included(self) -> bool:
        return self._included

    def set_included(self, included: bool) -> None:
        self._included = included
        self._text_edit.setVisible(included)
        self._redacted_label.setVisible(included)
        self._toggle_button.setText("Restore" if not included else "Remove")

    def flag_redacted_now(self) -> None:
        """Called when the SECOND, pre-send redaction pass changes this
        section's own text — the note has to say so even if the artist
        never saw a redaction at gather time."""
        self._redacted_label.setText("redacted just now — review below")

    def _on_toggle_clicked(self) -> None:
        self.set_included(not self._included)
        self.toggled.emit()


class BugReportView(QtWidgets.QWidget):
    closed = Signal()
    #: `{key: included}` for the three attachments, fired on every toggle
    #: — not only on Send — so navigating away without sending still
    #: remembers the choice. `AgentPanel` is the one that knows this
    #: belongs in `Settings.bugreport_attachments`; this view only reports
    #: what changed (same one-way layering as `SettingsView.changed`).
    attachments_changed = Signal(dict)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._worker: "BugReportWorker | None" = None
        self._endpoint = bugreport.DEFAULT_ENDPOINT

        outer = QtWidgets.QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 16)
        outer.setSpacing(0)

        header_row = QtWidgets.QHBoxLayout()
        back_button = QtWidgets.QToolButton(self)
        back_button.setText("←")
        back_button.setToolTip("Back")
        back_button.clicked.connect(self.closed.emit)
        header_row.addWidget(back_button)
        title_label = QtWidgets.QLabel("Report a bug", self)
        title_label.setStyleSheet("font-weight: bold; font-size: 14px;")
        header_row.addWidget(title_label)
        header_row.addStretch(1)
        outer.addLayout(header_row)

        self._scroll = QtWidgets.QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        self._scroll.setStyleSheet(theme.scrollbar_stylesheet())
        outer.addWidget(self._scroll, 1)

        content = QtWidgets.QWidget(self._scroll)
        content_layout = QtWidgets.QVBoxLayout(content)
        content_layout.setContentsMargins(0, 8, 0, 8)
        content_layout.setSpacing(theme.SPACING * 2)
        self._scroll.setWidget(content)

        rail = QtWidgets.QWidget(content)
        rail.setMaximumWidth(_RAIL_WIDTH)
        rail_layout = QtWidgets.QVBoxLayout(rail)
        rail_layout.setContentsMargins(0, 0, 0, 0)
        rail_layout.setSpacing(theme.SPACING * 2)
        # `AlignLeft` plus an explicit margin in `resizeEvent`, NOT
        # `AlignHCenter` — `SettingsView.resizeEvent` already has the exact
        # note for why: `AlignHCenter` centers within the SCROLL AREA's
        # viewport, which is a few pixels narrower than `self.width()`
        # the moment a vertical scrollbar is showing (any attachment with
        # more than a couple of lines). Measured live: that few-pixel gap
        # was enough to force the whole screen to scroll sideways to show
        # a "Remove" button that was otherwise fully on screen.
        content_layout.addWidget(rail, 0, QtCore.Qt.AlignLeft)
        self._content_layout = content_layout
        self._rail = rail

        intro = QtWidgets.QLabel(
            "Everything below is exactly what will be sent, as plain text you can "
            "edit or remove — nothing leaves this machine until you press Send.",
            rail,
        )
        intro.setWordWrap(True)
        intro.setStyleSheet("color: palette(disabled, text);")
        rail_layout.addWidget(intro)

        title_field_label = QtWidgets.QLabel("Title", rail)
        rail_layout.addWidget(title_field_label)
        self._title_edit = QtWidgets.QLineEdit(rail)
        self._title_edit.setPlaceholderText("A short summary of what went wrong")
        rail_layout.addWidget(self._title_edit)

        description_label = QtWidgets.QLabel("What happened", rail)
        rail_layout.addWidget(description_label)
        self._description_edit = QtWidgets.QTextEdit(rail)
        self._description_edit.setAcceptRichText(False)
        self._description_edit.setPlaceholderText("What were you doing, what did you expect, what happened instead?")
        self._description_edit.setMinimumHeight(100)
        # Same reasoning as `_AttachmentSection`'s own text edit: a pasted
        # URL or path with no spaces must wrap inside this box, not push
        # the whole screen wider.
        self._description_edit.setWordWrapMode(QtGui.QTextOption.WrapAnywhere)
        rail_layout.addWidget(self._description_edit)

        self._system_section = _AttachmentSection("System info", "system", rail)
        self._system_section.toggled.connect(self._on_attachment_toggled)
        rail_layout.addWidget(self._system_section)

        self._log_section = _AttachmentSection("Panel log (tail)", "log", rail)
        self._log_section.toggled.connect(self._on_attachment_toggled)
        rail_layout.addWidget(self._log_section)

        self._conversation_section = _AttachmentSection("Conversation (last few messages)", "conversation", rail)
        self._conversation_section.toggled.connect(self._on_attachment_toggled)
        rail_layout.addWidget(self._conversation_section)

        self._status_label = QtWidgets.QLabel("", rail)
        self._status_label.setWordWrap(True)
        self._status_label.setTextInteractionFlags(QtCore.Qt.TextBrowserInteraction)
        self._status_label.setOpenExternalLinks(True)
        self._status_label.setVisible(False)
        rail_layout.addWidget(self._status_label)

        action_row = QtWidgets.QHBoxLayout()
        self._copy_button = QtWidgets.QPushButton("Copy report to clipboard", rail)
        self._copy_button.clicked.connect(self._on_copy_clicked)
        self._copy_button.setVisible(False)
        action_row.addWidget(self._copy_button)
        action_row.addStretch(1)
        self._send_button = QtWidgets.QPushButton("Send", rail)
        self._send_button.clicked.connect(self._on_send_clicked)
        action_row.addWidget(self._send_button)
        rail_layout.addLayout(action_row)

        rail_layout.addStretch(1)

    def minimumSizeHint(self) -> QtCore.QSize:  # noqa: N802 - Qt override
        hint = super().minimumSizeHint()
        return QtCore.QSize(min(hint.width(), _MIN_RAIL_WIDTH), hint.height())

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        # Reference `self._scroll.width()`, NOT `self.width()`. Unlike
        # `SettingsView` (whose outer layout has zero margins, so the two
        # are the same number), this view's own outer layout has a 16px
        # margin on every side — `self.width()` runs 32px ahead of the
        # scroll area's actual width. Using `self.width()` here left the
        # rail + margin 32px too wide for the viewport, which is exactly
        # what forced a horizontal scrollbar and clipped "Remove".
        scroll_width = self._scroll.width()
        width = max(_MIN_RAIL_WIDTH, min(_RAIL_WIDTH, scroll_width - 32))
        self._rail.setFixedWidth(width)
        margin = max(0, (scroll_width - width) // 2)
        self._content_layout.setContentsMargins(margin, 0, 0, 0)

    # --- public ------------------------------------------------------------

    def open_for(
        self,
        *,
        system_fields: "bugreport.SystemFields",
        log_tail: str,
        log_redacted: bool,
        conversation_tail: str,
        conversation_redacted: bool,
        attachment_prefs: dict[str, bool],
        endpoint: str,
    ) -> None:
        """Called right before this page is shown — every gathered piece
        is fresh (a report about "right now", not whenever this widget
        happened to be built), and the title/description are cleared: a
        second report starts blank, it does not carry the last one's
        words forward.
        """
        self._endpoint = endpoint
        self._title_edit.clear()
        self._description_edit.clear()
        self._status_label.setVisible(False)
        self._copy_button.setVisible(False)
        self._send_button.setEnabled(True)
        self._send_button.setText("Send")

        self._system_section.set_text(system_fields.as_text(), redacted=False)
        self._log_section.set_text(log_tail, redacted=log_redacted)
        self._conversation_section.set_text(conversation_tail, redacted=conversation_redacted)

        for section in (self._system_section, self._log_section, self._conversation_section):
            section.set_included(attachment_prefs.get(section.key, True))

        self._title_edit.setFocus()

    def shutdown(self) -> None:
        """Same reasoning as `SettingsView.shutdown()`/`Composer.shutdown()`
        — a `BugReportWorker` still running when this widget tree comes
        down is the same hazard as any other (docs/facts/houdini.md §14).
        """
        worker = self._worker
        if worker is not None:
            release(worker)
            self._worker = None

    # --- internal ------------------------------------------------------

    def _on_attachment_toggled(self) -> None:
        prefs = {
            section.key: section.is_included()
            for section in (self._system_section, self._log_section, self._conversation_section)
        }
        self.attachments_changed.emit(prefs)

    def _compose_full_report(self) -> tuple[str, str, dict]:
        """`(title, body, structured_fields)` — the body is everything the
        artist chose to keep, joined in reading order; structured fields
        are only the ones an INCLUDED system-info section still carries
        (blank string omits a field server-side the same as not sending
        it at all — `bugreport.SystemFields`'s own fields are already
        strings, never `None`, so an intentionally-cleared one line reads
        as "not provided", not as this screen inventing a value)."""
        title = self._title_edit.text().strip()
        parts = [self._description_edit.toPlainText().strip()]
        if self._log_section.is_included() and self._log_section.text().strip():
            parts.append("## Panel log (tail)\n```\n" + self._log_section.text().strip() + "\n```")
        if self._conversation_section.is_included() and self._conversation_section.text().strip():
            parts.append(
                "## Conversation (last few messages)\n```\n" + self._conversation_section.text().strip() + "\n```"
            )
        body = "\n\n".join(p for p in parts if p)

        fields: dict[str, str] = {}
        if self._system_section.is_included():
            for line in self._system_section.text().splitlines():
                if ":" not in line:
                    continue
                key, _, value = line.partition(":")
                value = value.strip()
                if not value:
                    continue
                mapped = {
                    "Panel version": "panel_version",
                    "Houdini version": "houdini_version",
                    "OS": "os",
                    "Agent": "agent_id",
                }.get(key.strip())
                if mapped:
                    fields[mapped] = value
        return title, body, fields

    def _redact_before_sending(self) -> bool:
        """Runs `bugreport.redact_secrets` over EVERYTHING currently in
        the fields — including whatever the artist typed or pasted
        themselves, which the gather-time pass never saw. Returns True if
        anything changed, in which case the fields are updated in place
        and sending is held: the artist has to read the redacted text and
        press Send again, never a silent rewrite between "I read this"
        and "this left the machine".
        """
        changed_any = False

        title = self._title_edit.text()
        redacted_title, changed = bugreport.redact_secrets(title)
        if changed:
            self._title_edit.setText(redacted_title)
            changed_any = True

        description = self._description_edit.toPlainText()
        redacted_description, changed = bugreport.redact_secrets(description)
        if changed:
            self._description_edit.setPlainText(redacted_description)
            changed_any = True

        for section in (self._system_section, self._log_section, self._conversation_section):
            if not section.is_included():
                continue
            redacted_text, changed = bugreport.redact_secrets(section.text())
            if changed:
                section.set_text(redacted_text)
                section.flag_redacted_now()
                changed_any = True

        return changed_any

    def _on_send_clicked(self) -> None:
        if self._redact_before_sending():
            self._show_status(
                "Redacted something that looked like a credential — review the "
                "highlighted section above, then press Send again.",
                is_error=True,
            )
            return

        title, body, system_fields = self._compose_full_report()
        if len(title) < bugreport.TITLE_MIN:
            self._show_status(f"Title needs at least {bugreport.TITLE_MIN} characters.", is_error=True)
            return
        if len(body) < bugreport.BODY_MIN:
            self._show_status(
                f"The report needs at least {bugreport.BODY_MIN} characters — "
                "say a bit more about what happened.",
                is_error=True,
            )
            return

        payload = {"project": bugreport.PROJECT_KEY, "title": title, "body": body}
        payload.update(system_fields)

        from .bugreport_worker import BugReportWorker

        self._worker = BugReportWorker(self._endpoint, payload, parent=self)
        self._worker.succeeded.connect(self._on_send_succeeded)
        self._worker.failed.connect(self._on_send_failed)
        self._send_button.setEnabled(False)
        self._send_button.setText("Sending…")
        self._copy_button.setVisible(False)
        self._show_status("Sending…", is_error=False)
        self._worker.start()

    def _on_send_succeeded(self, issue_url: str) -> None:
        self._worker = None
        self._send_button.setEnabled(True)
        self._send_button.setText("Send")
        self._show_status(f'Filed: <a href="{issue_url}">{issue_url}</a>', is_error=False)

    def _on_send_failed(self, message: str) -> None:
        self._worker = None
        self._send_button.setEnabled(True)
        self._send_button.setText("Send")
        self._copy_button.setVisible(True)
        self._show_status(message, is_error=True)

    def _on_copy_clicked(self) -> None:
        title, body, system_fields = self._compose_full_report()
        text = f"{title}\n\n{body}"
        if system_fields:
            text += "\n\n---\n" + "\n".join(f"{k}: {v}" for k, v in system_fields.items())
        clipboard = QtWidgets.QApplication.clipboard()
        if clipboard is not None:
            clipboard.setText(text)
            self._show_status("Copied — paste it into a new issue at github.com/MAY4VFX/houdini-agent-panel.", is_error=False)

    def _show_status(self, text: str, *, is_error: bool) -> None:
        # Shape, not colour, marks failure — the same rule every status in
        # the feed already follows (`theme.status_glyph`'s own docstring:
        # "the only signal that survives any theme"). `QPalette` has no
        # semantic error role, and painting this red would mean hardcoding
        # a colour this module's own rules forbid.
        glyph = theme.status_glyph("failed") if is_error else ""
        self._status_label.setText(f"{glyph} {text}" if glyph else text)
        self._status_label.setVisible(True)


__all__ = ["BugReportView"]

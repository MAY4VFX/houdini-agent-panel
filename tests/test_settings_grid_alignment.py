"""Numeric proof that the settings screen's grid actually lines up.

The owner's report was "a staircase" — four different left edges (section
headers, agent rows, "Custom agent", field labels/checkboxes) and no shared
value column. `ui/settings_view.py`'s `_Section` was rewritten from a
per-instance `QFormLayout` to one shared grid; these tests compare mapped
X/Y coordinates between widgets in DIFFERENT sections, not a screenshot
eyeballed for "looks right" — the whole point of the rewrite was that
"looks right" is exactly what let the staircase happen in the first place.
"""

from __future__ import annotations

from houdini_agent_panel.ui.qt import QtCore
from houdini_agent_panel.ui.settings_view import SettingsView


def _left_x(view: SettingsView, widget) -> int:
    return widget.mapTo(view, QtCore.QPoint(0, 0)).x()


def _right_x(view: SettingsView, widget) -> int:
    return widget.mapTo(view, QtCore.QPoint(widget.width(), 0)).x()


def _build(qapp) -> SettingsView:
    view = SettingsView()
    # Privacy and Data start collapsed (design.md) — force every section
    # open so its body actually gets laid out before we measure it.
    for section in (
        view._agents_section,
        view._behaviour_section,
        view._updates_section,
        view._voice_section,
        view._privacy_section,
        view._network_section,
        view._data_section,
    ):
        section._toggle.setChecked(True)
        section._on_toggled(True)
    view.resize(1000, 900)
    view.show()
    qapp.processEvents()
    return view


def test_all_row_labels_share_one_left_edge(qapp):
    """"Default agent", "Whisper endpoint" and "Data folder" used to start
    at three different X positions — one per section's own `QFormLayout`.
    They now share one grid column across sections."""
    view = _build(qapp)

    default_agent_label = view._behaviour_section.widget_at(0, 0)
    whisper_label = view._voice_section.widget_at(0, 0)
    proxy_label = view._network_section.widget_at(0, 0)
    data_folder_label = view._data_section.widget_at(0, 0)
    assert None not in (default_agent_label, whisper_label, proxy_label, data_folder_label)

    xs = {
        _left_x(view, w)
        for w in (default_agent_label, whisper_label, proxy_label, data_folder_label)
    }
    assert len(xs) == 1, f"labels do not share one X: {xs}"


def test_all_row_values_share_one_left_edge(qapp):
    """The combo, the whisper edit, the proxy field and the data-folder row
    all start at the same X: the label column's fixed width plus the gap —
    not wherever each section's own longest label happened to end."""
    view = _build(qapp)

    xs = {
        _left_x(view, view._default_agent_combo),
        _left_x(view, view._whisper_edit),
        _left_x(view, view._proxy_edit),
        _left_x(view, view._data_dir_label),
    }
    assert len(xs) == 1, f"values do not share one X: {xs}"


def test_network_sections_own_three_rows_share_the_grid(qapp):
    """"Proxy", "No proxy" and "CA bundle" (issue #26) are three rows of the
    SAME section, so this is a narrower claim than the cross-section tests
    above: they must line up with each other too, not just with "Whisper
    endpoint" elsewhere on the page."""
    view = _build(qapp)
    section = view._network_section

    label_xs = {
        _left_x(view, section.widget_at(0, 0)),
        _left_x(view, section.widget_at(1, 0)),
        _left_x(view, section.widget_at(2, 0)),
    }
    assert label_xs == {_left_x(view, view._behaviour_section.widget_at(0, 0))}

    value_xs = {
        _left_x(view, view._proxy_edit),
        _left_x(view, view._no_proxy_edit),
        _left_x(view, view._ca_bundle_edit),
    }
    assert len(value_xs) == 1, f"Network's own values do not share one X: {value_xs}"


def test_checkboxes_start_at_the_label_column_x(qapp):
    """A lone checkbox has no separate label, but it must start at the SAME
    X the label column does — not further right, not at its own margin."""
    view = _build(qapp)

    label_x = _left_x(view, view._behaviour_section.widget_at(0, 0))
    checkbox_xs = {
        _left_x(view, view._autostart_checkbox),
        _left_x(view, view._check_updates_checkbox),
        _left_x(view, view._show_announcements_checkbox),
        _left_x(view, view._telemetry_checkbox),
    }
    assert checkbox_xs == {label_x}


def test_action_buttons_share_one_right_edge(qapp):
    """"Open" (beside "Data folder") and "Copy diagnostics" (its own,
    label-less row) land on the same right edge regardless of their text
    width — not wherever a spanning form row happened to place a lone
    button."""
    view = _build(qapp)

    assert _right_x(view, view._open_data_dir_button) == _right_x(
        view, view._copy_diagnostics_button
    )


def test_section_body_is_clearly_indented_from_its_own_header(qapp):
    """The header owns its content visually: the body's left edge sits
    clearly right of the toggle's own text, not flush with it (which is
    what made a section's rows read as the same rank as its title)."""
    view = _build(qapp)

    section = view._behaviour_section
    header_x = _left_x(view, section._toggle)
    body_x = _left_x(view, section.widget_at(0, 0))

    # A visible step, not a rounding artefact of the toggle's own padding.
    assert body_x - header_x >= 10


def test_section_gap_is_the_same_between_every_pair(qapp):
    """One number for the space between sections, not whatever a form's
    bottom margin plus the next header's own top padding used to add up
    to — checked between every consecutive pair, not just one."""
    view = _build(qapp)

    sections = [
        view._agents_section,
        view._behaviour_section,
        view._updates_section,
        view._voice_section,
        view._privacy_section,
        view._network_section,
        view._data_section,
    ]
    gaps = set()
    for a, b in zip(sections, sections[1:]):
        bottom = a.mapTo(view, QtCore.QPoint(0, a.height())).y()
        top = b.mapTo(view, QtCore.QPoint(0, 0)).y()
        gaps.add(top - bottom)
    assert len(gaps) == 1, f"section gaps are not uniform: {gaps}"

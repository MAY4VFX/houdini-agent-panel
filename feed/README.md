# `announcements.json` — how to edit the feed

Every installed panel fetches this file once a day (cached, see
`python/houdini_agent_panel/announcements.py`). Edit it directly in this
repository and push to `main` — the panel's next daily check-in will pick
up the changes on its own, nothing needs to be deployed.

The format — this, in full:

```json
{
    "version": 1,
    "announcements": [
        {
            "id": "2026-08-node-eol",
            "severity": "info",
            "title": "Portable Node is getting updated",
            "body": "The next panel version will raise the minimum Node version to 22 LTS. No action needed — the panel will update it on its own the next time an agent is installed.",
            "buttons": [
                { "label": "Learn more", "url": "https://github.com/MAY4VFX/houdini-agent-panel/releases" }
            ],
            "panel_versions": "",
            "expires": "2026-09-01T00:00:00Z"
        },
        {
            "id": "2026-08-critical-fx-bridge",
            "severity": "blocking",
            "title": "Critical bug in the scene bridge",
            "body": "In versions 0.2.x, under certain scene conditions the panel could corrupt an unsaved file. Update to 0.3.0 or later before continuing to work.",
            "buttons": [
                { "label": "How to update", "url": "https://github.com/MAY4VFX/houdini-agent-panel#update" }
            ],
            "panel_versions": ">=0.2,<0.3",
            "expires": ""
        }
    ]
}
```

## Record fields

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | A unique string. The panel uses it to remember "already shown" (`settings.seen_announcements`) — **never reuse an id** for a new message, or people who already saw the old one won't see the new one. |
| `severity` | no | `"info"` (a quiet banner that dismisses itself) or `"blocking"` (a popup over the input field — the feed is still readable, but the artist can't message the agent until the button is pressed). Missing or an unfamiliar value is treated as `"info"`: an unknown future severity level must not accidentally block an artist's input. |
| `title` | yes | The headline. |
| `body` | no | Text under the headline. |
| `buttons` | no | A list of `{ "label": "...", "url": "..." }`. A `blocking` message needs at least one button — without one there's nothing to unblock input with. |
| `panel_versions` | no | A panel version specifier like `">=0.2,<0.4"` (comma-separated conditions, all must hold). Empty means shown to every version. |
| `expires` | no | ISO 8601 (`"2026-09-01T00:00:00Z"`). The record stops being shown after this point. Empty or an unreadable date means it never expires. |

## Targeting rules

- `panel_versions` is compared against the panel's version using the same
  PEP 440-like comparison from `updates.py` — no epochs or local versions,
  just plain numeric releases with an optional pre-release/`.postN`/`.devN`.
- A specifier with a typo (an unknown operator, an unreadable version) is
  **shown to nobody** — this is a deliberate choice: a targeting error in
  JSON that's external to the code shouldn't accidentally reach users it
  wasn't meant for.
- A record that's broken as a whole (missing `id` or `title`, a field of
  the wrong type) is silently skipped by the panel — the rest of the feed's
  records aren't lost because of it.

## A limitation worth understanding

**Whether someone actually clicked a button's link can't be verified** —
the feed is static, with no server on the receiving end of clicks. The
panel only records the fact that the button itself was pressed in the UI
(which opens the link in the system browser), not that the person actually
read it. For `blocking` messages this means: input unblocks when the
button is pressed, not when reading is confirmed.

## What not to do

- Don't remove or change the `id` of an already-published record — that's
  the same as showing it to everyone again from scratch.
- Don't rely on record order — the panel doesn't guarantee it will show
  them in the order they appear in the file.
- Houdini is never blocked, not even by a `blocking` announcement — only
  the panel's input field gets blocked. If you need to stop a person's work
  entirely, that's not what this feed is for.

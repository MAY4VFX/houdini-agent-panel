# Telemetry — policy

Houdini Agent Panel can send anonymous usage statistics. This is off by
default, and no setting will ask you to log in to turn it on — only the
toggle in the panel's settings.

## What is collected

Only with telemetry turned on, and only if your panel install has a
receiver address configured (`HAP_TELEMETRY_URL` — a studio/distribution
setting, not something an artist edits):

- the panel's version, the `fxhoudinimcp` version, the version of the ACP
  agent in use;
- the Houdini version and operating system;
- the event name (e.g. "panel opened", "agent connected") and, on a
  crash, the exception type (e.g. `ConnectionError`), with no message text
  and no stack trace.

## What is never collected

- File and folder paths — not `$HIP`, not the scene path, not the panel's path.
- The contents of the Houdini scene: node names, geometry, parameters.
- The text of prompts and the agent's replies, or any conversation content.
- Agent session ids, user identifiers, the studio's name.

The list of what's even allowed into an event is hardcoded
(`telemetry.build_payload`) as a strict allowlist — an event with an
unfamiliar field isn't "written more or less safely," the unrecognized
field is dropped entirely. This is checked by an automated test that
verifies every collected event for the absence of paths and forbidden
keys.

## Where it's turned on and off

Panel Settings → "Telemetry" toggle. Off by default. The change takes
effect immediately, no Houdini restart needed.

## Where it goes

To the address set by the `HAP_TELEMETRY_URL` environment variable. If it
isn't set, a turned-on toggle sends nothing: the panel behaves exactly as
if telemetry were off — there are no network requests at all, not
"requests that go nowhere."

Any network error while sending is silently ignored: telemetry is not
allowed to slow down the panel or show an artist an error.

## How to delete what's already been sent

The data is anonymous and carries no identifier that could single out a
specific panel install among others — there's nothing to delete on a
per-install basis. To stop further collection, turn off the "Telemetry"
toggle in the panel's settings.

# History missing until the next prompt

The report: opening an old conversation omits its latest messages, even at the
bottom of the feed; submitting another prompt makes missing replies appear.

## Reproduced causes

- The drawer selected the local model without requesting `session/load`.
  Resuming happened on submit, so replies available only in the agent's history
  were not fetched while browsing.
- Restored models were populated at startup. Selecting a conversation did not
  reread a newer on-disk copy written by another Houdini process.
- Persistence compared the in-memory record against disk, treating any difference
  as a local edit. An untouched stale model could overwrite newer user and agent
  messages from another process.
- Replay arriving after `session_loaded` has no `turn_finished` event of its own.
  Such replies need a save trigger even when the user does not send anything.

## Changes

Selecting a restored conversation refreshes its disk copy when there are no local
edits, then loads the agent session if the connected agent supports it. Offline
history stays readable. A failed browse retains the saved transcript without
creating another agent session. Browsing does not submit a saved message queue.

The last loaded/saved snapshot is shared with each agent's models. Unchanged
local copies are skipped by persistence, preserving newer on-disk records. This
covers sequential writes from different processes, not simultaneous edits to the
same conversation: the JSON store still has no cross-process transaction lock.

Loads are serialized per tab and shared when two tabs select the same session.
Completion does not take the user away from another conversation. Pending prompts
are routed to the session they were typed into. Replay reaches the shared model
even when another tab is the signal writer. Late idle reply chunks trigger the
existing coalesced save.

## Verification and limits

Final validation: `.venv/bin/python -m pytest -q` — 1536 passed, 19 Qt signal
disconnect warnings, 97.42 seconds. `git diff --check` also passes.

`tests/test_history_open.py` exercises the actual drawer signal, transcript model,
client signals, and JSON store without a real agent or API call. The first three
regressions failed before the fix. Loading-on-open was also isolated in an initial
probe: zero loads and no latest reply before submit; the same reply appeared after
submit. Triggering load on open made that probe pass.

Existing history already absent from every local copy is not reconstructed for
user messages: the ACP client still ignores `user_message_chunk` echoes. Reliably
merging those with locally generated user entry IDs is separate work. Agent
replay can recover missing replies. No claim of live Houdini validation or of
having repaired the owner's stored data is made by these tests.

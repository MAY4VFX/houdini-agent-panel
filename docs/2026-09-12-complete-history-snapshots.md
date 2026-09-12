# Complete history tails and external CLI authentication

The screenshots distinguished a data problem from the previously fixed paint
jitter: the first screen held replies, then old reasoning and tool groups were
appended beneath them. `TranscriptModel.to_records` discarded those kinds, so
the local cache could never display the same tail as the loaded agent session.

## History

- Cache format 2 preserves readable thought, tool, plan and activity rows, in
  addition to messages. Permissions remain live-only. Binary payloads are not
  saved; long tool output is capped. The existing 400-entry cache limit remains.
- `session/load` replay is buffered on the worker and delivered with the loaded
  state as one snapshot. It is reconstructed in protocol order, instead of
  appending historical tools after a cached final reply. Local-only messages,
  queued text and annotations are retained; repeated prompts are matched from
  the end to account for a tail-only cache.
- Awaiting the RPC response is not a notification barrier in ACP SDK 0.12.1:
  incoming notifications are queued, while responses resolve immediately. A
  stream observer records received counts, and the worker waits until those
  specific notifications have been handled before publishing the snapshot.
  This is an event barrier, not a sleep or quiet-period heuristic.
- Existing text-only caches show Loading conversation on their first connected
  visit, then reveal the completed tail at the bottom. They are upgraded lazily.
  Full caches show the saved tail immediately while the agent resumes in the
  background. Offline or failed loads keep saved history available.
- The first revealed frame is held until bottom geometry settles. Full-cache
  refreshes retain the existing reading anchor and selection.

## Authentication

The owner's settings already listed claude-acp in signed_in_agents, while its
ACP auth methods were absent and the panel owned no token. The Settings helper
incorrectly required non-empty auth methods to accept a completed turn as login
evidence. The row also offered Sign in whenever logout was unavailable.

Known authentication now shows Signed in when the panel cannot perform logout.
Agents with supported logout, including the existing Codex case, retain Sign out.
No CLI credentials are changed, and no external account is logged out.

## Verification

1554 local tests passed, including a real fake-ACP subprocess with delayed
notification dispatch, cache round trips, legacy-to-full migration and the
actual Settings row. Native Houdini 21.0.792 and 22.0.368 smoke checks cover warm,
cold and reading modes, recording every painted frame with isolated data and no
real model request. The first visible cold frame and all warm-tail frames are
at the bottom; reading mode keeps its anchor. The source checks are followed by
the same checks on the GitHub-built release wheel.

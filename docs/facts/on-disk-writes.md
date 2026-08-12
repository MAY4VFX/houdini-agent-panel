# On-disk writes — the shared `.tmp` name corruption, measured

Every JSON file this panel owns (`registry.json`, `conversations.json`,
`settings.json`, `orphans.json`, the updates cache) is written the same
way: build the new content in a temp file, then `os.replace()` it into
place. `os.replace` is genuinely atomic — the rename itself can never
leave a reader looking at a half-written file. What five separate
implementations of this pattern got wrong, independently, in five files,
is what happens BEFORE that rename.

## 1. The incident

Reported by the owner: the Settings screen's "Agents" section rendered
completely empty — header expanded, nothing under it. `panel.log` was
clean: no exceptions, agent connected fine, session started fine. The
symptom and the log disagreed.

Found by reading the owner's own `~/Library/Application Support/
HoudiniAgentPanel/cache/registry.json` directly: 35729 bytes, written that
morning at the exact moment the panel started. `json.loads` on it raised:

```
json.decoder.JSONDecodeError: Extra data: line 1 column 35729
```

Read byte by byte: the first 35728 bytes were a complete, valid JSON
document on their own — not truncated, not garbage in the middle, no
missing closing brace. The 35729th byte was one extra `}`, alone, with
nothing after it. Not a crash mid-write (that leaves a document cut off
partway through, usually inside a string or a nested object) and not disk
corruption (that doesn't reliably reproduce as "the document is complete,
plus exactly one trailing byte of what looks like more of the same kind of
document"). The only explanation that fits ALL of these facts at once — a
complete valid document, plus a leftover fragment from something LONGER
that was also valid JSON of the same shape — is two writes landing in the
same file.

## 2. The mechanism — read directly, not inferred from the symptom alone

`registry.py::_write_cache` (before the fix in this same change):

```python
def _write_cache(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    wrapper = {"fetched_at": time.time(), "payload": payload}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(wrapper), "utf-8")
    os.replace(tmp, path)
```

`path.with_suffix(path.suffix + ".tmp")` is `registry.json.tmp` — the SAME
name, every single call, for every writer. `Path.write_text` opens in
truncate mode: `open(path, "w")`. Two processes calling this at close to
the same time (two Houdini sessions on the same machine, or the panel
racing something else that also touches the registry) can genuinely
interleave:

1. Process A opens `registry.json.tmp` (truncating it) and starts writing
   its own, longer payload.
2. Process B opens the SAME `registry.json.tmp` (truncating A's
   still-in-progress write) and writes its own, shorter payload, then
   finishes and calls `os.replace`.
3. Process A, still mid-write on what is now the SAME underlying file
   (its own file handle, unaware B truncated it), keeps writing from
   wherever its own byte offset was — landing bytes past the end of what
   B just wrote, since A's payload is longer than B's.
4. A finishes and calls `os.replace` too. Whichever `os.replace` runs
   LAST wins the rename outright — but by then the file's own CONTENT is
   already a hybrid: it's not "one write or the other", it's leftover
   bytes from whichever write's tail extended past the other's.

This reproduces exactly the shape found on disk: a complete, valid
document (the SHORTER write, B's, landing at the front, still coherent on
its own — `write_text` doesn't fsync per line, but a single `write()`
syscall for a already-buffered short string typically lands as one
contiguous block) followed by leftover bytes from the LONGER write (A's)
that happened to be, at that exact tail position, one more `}` — the
harmless, coincidental case, not the common one. A worse timing overlap
would have produced a document that doesn't even LOOK complete at the
front — this was already close to the best-case version of this bug.

Reproduced for real, live, not just reasoned about: two real subprocesses,
one writing a short payload and one writing a much longer one to the same
shared `.tmp` name, raced repeatedly — corruption reproduces (see
`tests/test_paths.py`'s own note on this; the corrupting repro isn't kept
as a permanent test, since it's timing-dependent and would be flaky, but
was run and confirmed before writing the fix below).

## 3. Why the failure was invisible

`registry.py::_read_cache` (before this fix): `except (OSError, ValueError):
return None` — a `json.JSONDecodeError` (a `ValueError` subclass) is
swallowed silently. `fetch_registry`'s own docstring says a stale cache is
accepted "at ANY age" when the network is unavailable — but a cache that
doesn't PARSE at all isn't stale, it's gone, and no age tolerance can
recover it. Every subsequent launch re-reads the SAME corrupt file and
gets the SAME `None` back, forever, until something removes it — nothing
did.

Separately, and just as important: even a LOUD failure here would not have
reached `panel.log`. `ui/panel.py::_on_refresh_done`'s own "Couldn't fetch
the agent list" message goes through `self._note(text, error=True)` —
`AgentPanel._note`'s own implementation writes directly into the
TRANSCRIPT MODEL (the chat feed), never into `logbook`'s file. A developer
reading `panel.log` to rule out a registry problem was checking a source
that structurally cannot answer that question, whether or not the
corruption existed. This is the same blind spot `c76d1ce` fixed for a
different pair of messages (panel-generated notes indistinguishable from
genuine errors) — the note/log split is real and intentional
(`design.md`), but it means "the log is clean" is never proof that
nothing user-facing went wrong; it only proves nothing was logged.

## 4. The fix

`paths.py::atomic_write_text` — one shared implementation, used by all
five call sites (`registry.py`, `conversations_store.py`, `settings.py`,
`orphans.py`, `updates.py`) instead of five hand-rolled copies of the same
gap. `tempfile.mkstemp(dir=path.parent, prefix=path.name + ".",
suffix=".tmp")` gives every INDIVIDUAL CALL its own unique file, in the
same directory as the target (required — `os.replace` across a filesystem
boundary is a copy, not a rename, and stops being atomic). Two concurrent
writers now hold two entirely separate files; neither can ever observe or
overwrite the other's bytes, and whichever `os.replace` happens to run
last simply wins outright, which is what "last writer wins" was always
supposed to mean here.

Verified live, not just by construction: `tests/test_paths.py::
test_two_concurrent_real_processes_never_produce_a_corrupt_file` spawns
two real subprocesses (a large and a small payload, the same asymmetry
that produced the original incident) racing the same target file, 20
times per run. A standalone measurement run (not kept as a permanent test
— too slow) repeated the same race 200 times: 0 corrupted files. The OLD
mechanism, exercised the same way as a control, reproduced corruption on
essentially the first attempt.

`registry.py::_read_cache` additionally now logs the corruption (`_log.
warning`, `houdini_agent_panel.registry`) AND deletes the unparseable
file, so a corrupt cache — from before this fix, or from any future cause
this fix doesn't happen to cover — heals itself on the very next launch
instead of failing identically forever. `settings.py::load` already had
this exact self-healing shape (renaming a broken settings file to
`.broken` rather than looping on it forever) before this incident; this
brings `registry.py` in line with a pattern that already existed
elsewhere in the codebase, not a new idea.

## Not established

- Whether `conversations_store.py`, `orphans.py`, or `updates.py` have
  ever actually produced a corrupted file this way in practice — only
  `registry.json` was caught with a live, corrupted example. All five
  shared the identical vulnerable pattern, which is why all five were
  fixed together rather than only the one caught red-handed; the OTHER
  four are a preventive fix for a proven mechanism, not a confirmed
  separate incident each.
- Whether a similar corruption could happen from a genuinely killed
  process (SIGKILL, not two processes racing) leaving a `.tmp` file
  behind with nothing to ever clean it up — `atomic_write_text`'s own
  `except BaseException: tmp.unlink(...)` covers an in-process failure
  (an exception raised while writing), not a process that stops existing
  entirely mid-write. A leftover uniquely-named `.tmp` file from that case
  is harmless on its own (it is never read by anything, and a fresh write
  gets its own new unique name) — just not actively swept.

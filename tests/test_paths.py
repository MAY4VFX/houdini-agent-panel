"""`paths.atomic_write_text` — the shared write-without-corruption helper
five modules used to hand-roll separately (`registry.py`,
`conversations_store.py`, `settings.py`, `orphans.py`, `updates.py`), each
with the exact same gap: a FIXED, shared temp filename
(`path.with_suffix(path.suffix + ".tmp")`) is not actually atomic across
two concurrent writers — only the final `os.replace` is. Found for real:
an owner's `registry.json` cache was a complete, valid JSON document plus
exactly one leftover trailing byte from a longer write a shorter one raced
and lost to (docs/facts/on-disk-writes.md has the full byte-level account).
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

from houdini_agent_panel import paths


def test_writes_and_reads_back(tmp_path):
    target = tmp_path / "sub" / "file.json"
    paths.atomic_write_text(target, '{"a": 1}')
    assert json.loads(target.read_text("utf-8")) == {"a": 1}


def test_creates_parent_directories(tmp_path):
    target = tmp_path / "does" / "not" / "exist" / "yet" / "file.json"
    paths.atomic_write_text(target, "content")
    assert target.read_text("utf-8") == "content"


def test_leaves_no_tmp_file_behind_on_success(tmp_path):
    target = tmp_path / "file.json"
    paths.atomic_write_text(target, "content")
    leftovers = list(tmp_path.glob("*.tmp"))
    assert leftovers == [], f"a temp file survived a successful write: {leftovers}"


def test_a_second_write_replaces_the_first_cleanly(tmp_path):
    target = tmp_path / "file.json"
    paths.atomic_write_text(target, "first")
    paths.atomic_write_text(target, "second, and longer than the first")
    assert target.read_text("utf-8") == "second, and longer than the first"
    assert list(tmp_path.glob("*.tmp")) == []


def test_a_failed_write_cleans_up_its_own_tmp_file(tmp_path, monkeypatch):
    """The one thing worse than a `.tmp` file surviving a crash is a whole
    directory of them accumulating from every failed write forever. The
    write itself succeeds here; it's the final `os.replace` that fails
    (permission denied, the target directory vanishing underneath it) —
    a more realistic late failure than a mid-write one, and it doesn't
    require faking the low-level file object `os.fdopen` hands back."""
    target = tmp_path / "file.json"
    monkeypatch.setattr(
        paths.os, "replace", lambda *a, **k: (_ for _ in ()).throw(OSError("boom"))
    )

    try:
        paths.atomic_write_text(target, "content")
    except OSError:
        pass
    else:
        raise AssertionError("expected the injected OSError to propagate")

    assert not target.exists()
    assert list(tmp_path.glob("*.tmp")) == []


# --- the actual regression: two real, concurrent processes ------------------

_RACING_WRITER = textwrap.dedent(
    """
    import sys
    sys.path.insert(0, {source!r})
    import json
    from pathlib import Path
    from houdini_agent_panel import paths

    size = int(sys.argv[1])
    payload = json.dumps({{"payload": "x" * size}})
    paths.atomic_write_text(Path({target!r}), payload)
    """
)


def test_two_concurrent_real_processes_never_produce_a_corrupt_file(tmp_path):
    """The exact shape of the incident this was written for: two writers,
    one with more bytes to write than the other, racing the SAME target.
    Under the old fixed-`.tmp`-name scheme this reliably corrupts (proven
    separately, live, against the actual old code — not reproduced here to
    keep this test's runtime sane); under `atomic_write_text` each writer
    gets its own temp file, so there is nothing left for them to race.

    Repeated, not run once: a race that only sometimes reproduces would
    make a single green run worthless as a regression guard.
    """
    target = tmp_path / "registry.json"
    source = str(Path(__file__).resolve().parents[1] / "python")
    script = tmp_path / "writer.py"
    script.write_text(_RACING_WRITER.format(source=source, target=str(target)))

    for _ in range(20):
        if target.exists():
            target.unlink()
        p1 = subprocess.Popen([sys.executable, str(script), "20000"])
        p2 = subprocess.Popen([sys.executable, str(script), "500"])
        p1.wait(timeout=10)
        p2.wait(timeout=10)
        assert target.exists(), "one of the two writes must have landed"
        # Must parse cleanly — a corrupt file is exactly what this guards.
        json.loads(target.read_text("utf-8"))

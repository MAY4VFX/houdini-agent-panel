"""`SelfUpdateWorker`: runs `uvx --refresh --from <target>==<version> python
-m houdini_agent_panel install` off the main thread and classifies what
happened — the automated path behind the notice strip's "Update" button
for the panel/fxhoudinimcp, replacing "type this yourself" (issue: the
owner asking why the panel can't do this itself).

No real `uv` is invoked: a small stand-in script stands in for `uvx`
(found via `which`, monkeypatched), and its behaviour is chosen by an
environment variable the test sets before `worker.start()` — the same
"read a stand-in shape controlled by the test" pattern
`test_terminal_login_worker.py` already uses for a spawned process.
"""

from __future__ import annotations

import sys
import textwrap

from houdini_agent_panel.ui import self_update as self_update_module
from houdini_agent_panel.ui.self_update import SelfUpdateWorker

_FAKE_UVX = textwrap.dedent(
    """
    import os, sys, time
    behavior = os.environ.get("HAP_TEST_UVX_BEHAVIOR", "success")
    if behavior == "success":
        print("Downloading houdini_agent_panel-0.5.0-py3-none-any.whl")
        print("Successfully installed houdini-agent-panel-0.5.0")
        sys.exit(0)
    elif behavior == "write_failure":
        print(
            "ERROR: Could not install packages due to an OSError: [WinError 32] "
            "The process cannot access the file because it is being used by "
            "another process"
        )
        sys.exit(1)
    elif behavior == "download_failure":
        print(
            "ConnectionError: HTTPSConnectionPool(host='pypi.org', port=443): "
            "Max retries exceeded"
        )
        sys.exit(1)
    elif behavior == "generic_failure":
        print("line one of an unrelated pip error")
        print("line two of an unrelated pip error")
        sys.exit(3)
    elif behavior == "hang":
        time.sleep(30)
        sys.exit(0)
    elif behavior == "no_output_failure":
        sys.exit(7)
    elif behavior == "echo_argv_and_env":
        print("ARGV:" + " ".join(sys.argv[1:]))
        for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
            print(f"{name}:" + os.environ.get(name, "<unset>"))
        sys.exit(0)
    """
)


def _wait_until(app, condition, *, timeout_ms: int = 5000) -> None:
    from PySide6 import QtTest

    elapsed = 0
    step = 20
    while not condition() and elapsed < timeout_ms:
        app.processEvents()
        QtTest.QTest.qWait(step)
        elapsed += step
    assert condition(), "condition did not become true in time"


def _fake_uvx(tmp_path, monkeypatch, behavior: str):
    """Points `self_update.which("uvx", ...)` at a real, runnable script —
    `sys.executable` itself can't be `argv[0]` here (the worker's own argv
    after it — `--refresh --from ...` — would be handed to that
    interpreter directly, not to `-c` script text), so this writes an
    actual file with a `sys.executable` shebang, matching what `which`
    would really find on disk."""
    script = tmp_path / "uvx"
    script.write_text(f"#!{sys.executable}\n{_FAKE_UVX}")
    script.chmod(0o755)
    monkeypatch.setattr(self_update_module, "which", lambda name, path=None: str(script))
    monkeypatch.setenv("HAP_TEST_UVX_BEHAVIOR", behavior)
    # `build_env` widens the OS environment with the artist's login shell
    # (`shellenv.merged`) — stubbed to empty for the same determinism
    # reason `test_terminal_login_worker.py`'s own `_no_shell` exists: a
    # real `subprocess.run` per test would be slow and could pick up
    # whatever the test machine's own shell profile happens to export.
    from houdini_agent_panel import shellenv as shellenv_module

    monkeypatch.setattr(shellenv_module, "capture", lambda **_: {})


def test_a_successful_update_streams_progress_and_succeeds(qapp, tmp_path, monkeypatch):
    _fake_uvx(tmp_path, monkeypatch, "success")
    worker = SelfUpdateWorker("houdini-agent-panel", "0.5.0")

    progress: list[str] = []
    succeeded = []
    failed = []
    worker.progressed.connect(progress.append)
    worker.succeeded.connect(lambda: succeeded.append(True))
    worker.failed.connect(failed.append)
    worker.start()

    _wait_until(qapp, lambda: succeeded or failed)
    worker.wait(3000)

    assert failed == []
    assert succeeded == [True]
    assert any("Downloading" in line for line in progress)
    assert any("Successfully installed" in line for line in progress)


def test_a_windows_style_sharing_violation_is_named_as_a_write_failure(qapp, tmp_path, monkeypatch):
    """The one case this project could not test directly (no Windows
    machine) — an open .pyd/.dll can't be overwritten there, and pip's own
    error text for that shape (WinError 32) must produce "close Houdini
    and run it again", not a generic failure message that reads the same
    as a dead network."""
    _fake_uvx(tmp_path, monkeypatch, "write_failure")
    worker = SelfUpdateWorker("houdini-agent-panel", "0.5.0")

    failed: list[str] = []
    worker.failed.connect(failed.append)
    worker.start()

    _wait_until(qapp, lambda: bool(failed))
    worker.wait(3000)

    assert "close houdini" in failed[0].lower()
    assert "write" in failed[0].lower()


def test_a_dropped_connection_is_named_as_a_download_failure_not_a_write_failure(qapp, tmp_path, monkeypatch):
    """The failure this notice's own advice has to distinguish from the
    write-failure case above — a network problem is fixed by exporting a
    proxy or waiting, not by closing Houdini, and telling them apart
    wrong sends someone chasing the wrong fix."""
    _fake_uvx(tmp_path, monkeypatch, "download_failure")
    worker = SelfUpdateWorker("houdini-agent-panel", "0.5.0")

    failed: list[str] = []
    worker.failed.connect(failed.append)
    worker.start()

    _wait_until(qapp, lambda: bool(failed))
    worker.wait(3000)

    assert "download" in failed[0].lower() or "network" in failed[0].lower()
    assert "close houdini" not in failed[0].lower()


def test_an_unrecognised_failure_reports_the_exit_code_and_the_actual_output(qapp, tmp_path, monkeypatch):
    """Not a summary — the ask this whole feature exists to satisfy for
    the manual fallback too: enough to act on, not "something went
    wrong"."""
    _fake_uvx(tmp_path, monkeypatch, "generic_failure")
    worker = SelfUpdateWorker("houdini-agent-panel", "0.5.0")

    failed: list[str] = []
    worker.failed.connect(failed.append)
    worker.start()

    _wait_until(qapp, lambda: bool(failed))
    worker.wait(3000)

    assert "exit code 3" in failed[0]
    assert "line one of an unrelated pip error" in failed[0]


def test_a_failure_with_no_captured_output_still_names_the_exit_code(qapp, tmp_path, monkeypatch):
    _fake_uvx(tmp_path, monkeypatch, "no_output_failure")
    worker = SelfUpdateWorker("houdini-agent-panel", "0.5.0")

    failed: list[str] = []
    worker.failed.connect(failed.append)
    worker.start()

    _wait_until(qapp, lambda: bool(failed))
    worker.wait(3000)

    assert "exit code 7" in failed[0]


def test_uvx_not_found_names_the_manual_command_as_the_reason(qapp, monkeypatch):
    """`_start_update`'s whole point is to stop leaving the artist with
    only the manual command — but if `uv` genuinely isn't on this
    machine, the manual command IS the honest answer, spelled out in
    full rather than "uv not found"."""
    monkeypatch.setattr(self_update_module, "which", lambda name, path=None: None)
    from houdini_agent_panel import shellenv as shellenv_module

    monkeypatch.setattr(shellenv_module, "capture", lambda **_: {})
    worker = SelfUpdateWorker("houdini-agent-panel", "0.5.0")

    failed: list[str] = []
    worker.failed.connect(failed.append)
    worker.start()

    _wait_until(qapp, lambda: bool(failed))
    worker.wait(3000)

    assert "uvx --refresh --from houdini-agent-panel==0.5.0 python -m houdini_agent_panel install" in failed[0]


def test_a_hung_child_is_killed_and_reported_after_the_timeout(qapp, tmp_path, monkeypatch):
    """A child that goes silent (network stalled, proxy dropped) must not
    hang this worker forever — `for line in process.stdout` alone has no
    timeout, exactly the "curl with no --connect-timeout" shape already
    found and fixed in install.sh's own fetches; this is the same defect
    in a different process. `_UPDATE_TIMEOUT` is lowered for the test —
    the real 600s default would make this test itself hang for ten
    minutes."""
    monkeypatch.setattr(self_update_module, "_UPDATE_TIMEOUT", 0.5)
    _fake_uvx(tmp_path, monkeypatch, "hang")
    worker = SelfUpdateWorker("houdini-agent-panel", "0.5.0")

    failed: list[str] = []
    worker.failed.connect(failed.append)
    worker.start()

    _wait_until(qapp, lambda: bool(failed), timeout_ms=5000)
    worker.wait(3000)

    assert "timed out" in failed[0].lower()


def test_target_names_fxhoudinimcp_specifically_in_every_message(qapp, tmp_path, monkeypatch):
    """`update.kind == "fx"` updates `fxhoudinimcp`, not the panel itself
    — the same worker, a different target, and the messages have to say
    which one."""
    _fake_uvx(tmp_path, monkeypatch, "write_failure")
    worker = SelfUpdateWorker("fxhoudinimcp", "1.2.3")

    failed: list[str] = []
    worker.failed.connect(failed.append)
    worker.start()

    _wait_until(qapp, lambda: bool(failed))
    worker.wait(3000)

    assert "fxhoudinimcp" in failed[0]


def test_the_version_is_pinned_into_the_from_spec_not_left_for_uvx_to_resolve(
    qapp, tmp_path, monkeypatch
):
    """The bug this exists for: an owner on 0.7.1 pressed Update with 0.7.2
    available, and the panel reinstalled 0.7.1 over itself — an update that
    silently undid itself. `--from houdini-agent-panel` (no pin) asks uvx
    to resolve "latest" itself; `Update.latest` is already known, on the
    `Update` record the notice is showing, so there is nothing to ask uvx
    to figure out."""
    _fake_uvx(tmp_path, monkeypatch, "echo_argv_and_env")
    worker = SelfUpdateWorker("houdini-agent-panel", "0.7.2")

    progress: list[str] = []
    succeeded = []
    worker.progressed.connect(progress.append)
    worker.succeeded.connect(lambda: succeeded.append(True))
    worker.start()

    _wait_until(qapp, lambda: succeeded)
    worker.wait(3000)

    argv_line = next(line for line in progress if line.startswith("ARGV:"))
    assert "--from houdini-agent-panel==0.7.2" in argv_line


def test_pythonpath_is_stripped_before_spawning_the_child(qapp, tmp_path, monkeypatch):
    """Confirmed by direct reproduction (not just reasoning): Houdini's own
    package json prepends its deps tree to `PYTHONPATH`
    (`houdini_package.py`), and a child spawned with that inherited wins
    over whatever `uvx` resolves into its own venv — pinning the version
    alone (the test above) does NOT fix this by itself, planting a fake
    package on `PYTHONPATH` and pinning `--from` to a real, different
    version still imported the fake one. `PYTHONHOME`/`PYTHONSTARTUP` are
    the same class of leak (`mcp_runtime.SHADOWING_VARS`, shared with the
    fx server subprocess's own identical fix)."""
    monkeypatch.setenv("PYTHONPATH", str(tmp_path / "old-deps-tree"))
    monkeypatch.setenv("PYTHONHOME", str(tmp_path / "somewhere"))
    monkeypatch.setenv("PYTHONSTARTUP", str(tmp_path / "startup.py"))
    _fake_uvx(tmp_path, monkeypatch, "echo_argv_and_env")
    worker = SelfUpdateWorker("houdini-agent-panel", "0.7.2")

    progress: list[str] = []
    succeeded = []
    worker.progressed.connect(progress.append)
    worker.succeeded.connect(lambda: succeeded.append(True))
    worker.start()

    _wait_until(qapp, lambda: succeeded)
    worker.wait(3000)

    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
        line = next(l for l in progress if l.startswith(f"{name}:"))
        assert line == f"{name}:<unset>", f"{name} leaked into the child: {line}"

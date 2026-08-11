"""The artist's own environment reaches the agent.

Houdini is launched by the window server, not by a shell, so it never sees
`~/.zprofile`; and the ACP SDK hands a subprocess six variables by design.
Between them, an agent launched from the panel had no key, no proxy and no
cloud project — while the same agent run from the artist's terminal had all
three. That is the whole of the "Gemini won't log in from the panel" report.
"""

from __future__ import annotations

import subprocess

import pytest

from houdini_agent_panel import shellenv


@pytest.fixture(autouse=True)
def fresh():
    shellenv.reset_cache_for_tests()
    yield
    shellenv.reset_cache_for_tests()


def _fake_run(stdout: bytes, returncode: int = 0):
    def _run(*_args, **_kwargs):
        return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=b"")

    return _run


def test_values_may_contain_newlines(monkeypatch):
    """NUL-separated for a reason: a proxy exclusion list or a PEM-shaped
    value spans lines, and splitting on newlines would truncate it into
    something that looks valid and is not."""
    monkeypatch.setattr(
        subprocess, "run", _fake_run(b"KEY=line one\nline two\0OTHER=plain\0")
    )
    env = shellenv.capture()
    assert env["KEY"] == "line one\nline two"
    assert env["OTHER"] == "plain"


def test_a_shell_that_fails_costs_nothing(monkeypatch):
    """An environment we could not read is a missing convenience, never a
    reason to refuse to launch the agent."""
    def _boom(*_args, **_kwargs):
        raise OSError("no shell here")

    monkeypatch.setattr(subprocess, "run", _boom)
    assert shellenv.capture() == {}


def test_a_shell_that_hangs_is_given_up_on(monkeypatch):
    def _timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="shell", timeout=5.0)

    monkeypatch.setattr(subprocess, "run", _timeout)
    assert shellenv.capture() == {}


def test_the_shells_own_session_variables_are_not_carried_over(monkeypatch):
    """`PWD` and `SHLVL` describe the shell we spawned to ask the question.
    They say nothing true about the agent."""
    monkeypatch.setattr(
        subprocess, "run", _fake_run(b"PWD=/somewhere\0SHLVL=3\0GEMINI_API_KEY=k\0")
    )
    env = shellenv.capture()
    assert "PWD" not in env and "SHLVL" not in env
    assert env["GEMINI_API_KEY"] == "k"


def test_path_from_the_profile_is_kept(monkeypatch):
    """The opposite case, and the reason `PATH` is not in that exclusion
    list: the profile's `PATH` is where `node`, `npx` and the agent binaries
    are. Dropping it would reintroduce the bug this module exists for."""
    monkeypatch.setattr(subprocess, "run", _fake_run(b"PATH=/opt/homebrew/bin:/usr/bin\0"))
    assert shellenv.capture()["PATH"] == "/opt/homebrew/bin:/usr/bin"


def test_settings_win_over_the_shell_and_the_shell_over_the_sdk(monkeypatch):
    """Weakest first: the SDK's minimum, then the artist's profile, then
    what they typed into the panel — the only one of the three they can see
    and edit from here."""
    monkeypatch.setattr(subprocess, "run", _fake_run(b"TOKEN=from-profile\0EXTRA=shell\0"))
    merged = shellenv.merged({"TOKEN": "from-sdk", "BASE": "kept"}, {"TOKEN": "typed"})
    assert merged["TOKEN"] == "typed"
    assert merged["EXTRA"] == "shell"
    assert merged["BASE"] == "kept"


def test_the_shell_is_only_asked_once(monkeypatch):
    """It is a subprocess on the way to launching an agent; asking per
    launch would add its cost to every restart."""
    calls: list[int] = []

    def _counting(*_args, **_kwargs):
        calls.append(1)
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=b"A=1\0", stderr=b"")

    monkeypatch.setattr(subprocess, "run", _counting)
    shellenv.capture()
    shellenv.capture()
    shellenv.capture()
    assert len(calls) == 1


def test_the_shell_does_not_inherit_houdinis_own_environment(monkeypatch):
    """The regression this module now guards against: Houdini's package json
    writes `PYTHONPATH`/`HAP_DEPS`/`HAP_PYTHON` into Houdini's own process
    environment for the panel's own benefit (`houdini_package.py`). Spawning
    the "login shell" from that environment relays those straight through —
    `env -0` cannot tell "the profile set this" from "this was already
    there" — and a deps tree built for one Python's ABI ended up on
    `PYTHONPATH` for a completely different interpreter the fx MCP server
    was launched with, breaking `pydantic_core` and killing the connection
    in under a second. `subprocess.run` must be given an explicit `env=`
    that does NOT include these, no matter what the real `os.environ` has.
    """
    monkeypatch.setenv("PYTHONPATH", "/Users/artist/Library/Application Support/HoudiniAgentPanel/deps/py3.13")
    monkeypatch.setenv("HAP_DEPS", "/Users/artist/Library/Application Support/HoudiniAgentPanel/deps/py3.13")
    monkeypatch.setenv("HAP_PYTHON", "/some/interpreter")
    seen_env: list[dict] = []

    def _record(*_args, **kwargs):
        seen_env.append(kwargs.get("env"))
        return subprocess.CompletedProcess(args=[], returncode=0, stdout=b"A=1\0", stderr=b"")

    monkeypatch.setattr(subprocess, "run", _record)
    shellenv.capture()

    assert seen_env, "subprocess.run was never called"
    spawn_env = seen_env[0]
    assert spawn_env is not None, "env= was not passed — the shell would inherit the full parent environment"
    assert "PYTHONPATH" not in spawn_env
    assert "HAP_DEPS" not in spawn_env
    assert "HAP_PYTHON" not in spawn_env


def test_hap_prefixed_variables_never_survive_even_if_the_shell_prints_them(monkeypatch):
    """Second line of defense, independent of the spawn-env fix above: a
    variable the shell itself echoes back (e.g. because some future code
    path widens the spawn env again) is still not "the artist's
    environment" if it is one of ours."""
    monkeypatch.setattr(
        subprocess,
        "run",
        _fake_run(b"HAP_DEPS=/leaked\0HAP_PYTHON=/leaked\0REAL_KEY=kept\0"),
    )
    env = shellenv.capture()
    assert "HAP_DEPS" not in env
    assert "HAP_PYTHON" not in env
    assert env["REAL_KEY"] == "kept"


def test_the_shell_is_asked_interactively(monkeypatch):
    """Not a style choice, and got wrong here first.

    zsh — macOS's default shell since Catalina — reads `.zshenv` and
    `.zprofile` for a login shell and `.zshrc` only for an interactive one.
    Exports live in `.zshrc` on most real machines. Measured from a clean
    environment on the machine that reported this: `zsh -lc` yields 14
    variables and no `GEMINI_API_KEY`; `zsh -ilc` yields 17 and finds it.
    Reasoning from "credentials belong in the profile" produces the elegant
    flag and keeps the bug.
    """
    seen: list[list[str]] = []

    def _record(argv, **_kwargs):
        seen.append(list(argv))
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"A=1\0", stderr=b"")

    monkeypatch.setattr(subprocess, "run", _record)
    shellenv.capture()

    assert seen, "the shell was never asked"
    flags = seen[0][1]
    assert "i" in flags, f"a non-interactive shell misses ~/.zshrc entirely: {flags!r}"
    assert "l" in flags, f"a non-login shell misses ~/.zprofile: {flags!r}"

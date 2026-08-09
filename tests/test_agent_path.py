"""The PATH an agent process actually runs with.

Its own file because the two halves come from opposite ends of the codebase
— `runtime` knows where our Node is, `shellenv` knows what the artist's
shell says, and only `client._agent_path` sees both. Getting it wrong is
invisible in every unit test of either half: the agent starts, and then
cannot find `git`.
"""

from __future__ import annotations

import os

from houdini_agent_panel import client as client_mod
from houdini_agent_panel.runtime import LaunchSpec


def test_binary_agent_keeps_the_merged_path_untouched():
    """No directories of ours to put first — a binary agent (opencode,
    kimi) gets exactly what `shellenv.merged` produced."""
    spec = LaunchSpec(command="/agents/opencode", args=["acp"], env={})

    assert client_mod._agent_path(spec, {"PATH": "/shell/bin:/usr/bin"}) == "/shell/bin:/usr/bin"


def test_npx_agent_leads_with_our_node_and_keeps_the_shell_path(monkeypatch):
    """The regression this exists for: `runtime._npx_launch_spec` can only
    build `env["PATH"]` from Houdini's own PATH, and merging that in the
    ordinary way threw away the login shell's — so an npx agent lost every
    tool (`git`, a version manager's python) that a binary agent kept.
    """
    monkeypatch.setattr(
        client_mod.shellenv, "capture", lambda **_: {"PATH": "/opt/homebrew/bin:/usr/bin"}
    )
    spec = LaunchSpec(
        command="/data/node/bin/node",
        args=["npx-cli.js"],
        env={"PATH": os.pathsep.join(["/data/node/bin", "/houdini/bin"])},
        path_prepend=("/data/node/bin",),
    )

    result = client_mod._agent_path(spec, {"PATH": spec.env["PATH"]})

    assert result.split(os.pathsep) == ["/data/node/bin", "/opt/homebrew/bin", "/usr/bin"]


def test_npx_agent_falls_back_to_the_merged_path_without_a_shell(monkeypatch):
    """Windows has no login shell to read (`shellenv.capture` returns an
    empty dict there) — our Node must still come first. Written with POSIX
    paths because `os.pathsep` is whatever the test host uses, and the
    question here is the ORDER, not the spelling."""
    monkeypatch.setattr(client_mod.shellenv, "capture", lambda **_: {})
    spec = LaunchSpec(command="node", args=[], env={}, path_prepend=("/hap/node",))

    result = client_mod._agent_path(spec, {"PATH": "/system/bin"})

    assert result.split(os.pathsep) == ["/hap/node", "/system/bin"]


def test_a_spec_without_the_field_is_not_a_crash():
    """`client` accepts anything spec-shaped (the tests' own stand-ins do
    not carry `path_prepend`), and a missing field means "nothing to put
    first", never a failed launch."""

    class MinimalSpec:
        command = "/bin/agent"
        args: list[str] = []
        env: dict[str, str] = {}

    assert client_mod._agent_path(MinimalSpec(), {"PATH": "/usr/bin"}) == "/usr/bin"

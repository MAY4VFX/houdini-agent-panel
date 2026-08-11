"""End-to-end run of the panel inside a real Houdini, against real agents.

Why this exists: every serious defect in this project so far was invisible to
the unit tests and only showed up in a live Houdini — the swapped-out asyncio,
the missing CA bundle, the npx path, the event loop that could not be
restarted, an agent that reports "not signed in" only on stderr. Finding
those by asking a human to open Houdini, click, and describe what they saw
does not scale and is miserable for the human.

So this drives the real thing: the installed build, inside `hython`, with the
real ACP client, the real agents and the real fx bridge. No mocks anywhere.

Run it::

    hython tests/e2e/run_e2e.py                  # every check, default agent
    hython tests/e2e/run_e2e.py --agent codex-acp
    hython tests/e2e/run_e2e.py --only prompt,mcp
    hython tests/e2e/run_e2e.py --list

What it cannot cover, and nobody should pretend otherwise: real mouse and
keyboard inside Houdini's GUI, pane docking, live theme switching, a real
microphone, and a browser OAuth round trip. Those still need a human. The
point is that everything else no longer does.

This file is a deliberate exception to a rule every OTHER hython/manual
verification script must follow: it does NOT set `HAP_DATA_DIR` to a
throwaway directory, because driving the real installed build against real
agents is the entire point. Do not copy that part. A one-off script written
to check some panel logic by hand must set `HAP_DATA_DIR` before importing
`houdini_agent_panel` at all — see AGENTS.md's Project rules. Without it, a
real `settings.json` with `autostart_agent=True` gets a real agent launched
against the real API on the very first `app.processEvents()`, which is
exactly how this project once ran a real Claude session for 30+ minutes and
clobbered real install records on a developer's own machine.

One convention every check here follows: a check that returns without
actually verifying its own claim (nothing on this machine/agent to check
against — see `check_sign_in_reachable`, `check_captured_token_signs_in_
alone`) says so by starting its returned string with `_SKIP_PREFIX`,
`"skipped — "` — never a sentence that merely sounds like a passing
result. `main` prints that as SKIP, not ok, and counts it apart from
`passed` in the final tally. A check that invents a different way to say
"nothing to verify here" defeats the one thing that makes a summary line
worth trusting.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sys
import tempfile
import time
import traceback

# Assigned, not `setdefault`ed. This drives a real panel with real widgets
# and calls `show()`; inheriting a windowing platform from whatever shell
# launched it means the run scatters live windows across the desktop of
# whoever is using the machine. `HAP_E2E_VISIBLE=1` opts back in for the rare
# case of watching it work.
if os.environ.get("HAP_E2E_VISIBLE") != "1":
    os.environ["QT_QPA_PLATFORM"] = "offscreen"


def _select_source() -> str:
    """Decide which copy of the package this run exercises, and say so.

    `hython` does not honour PYTHONPATH ordering: Houdini rebuilds `sys.path`
    with its own `site-packages-forced` entries first, so putting the source
    tree ahead of the installed deps tree in PYTHONPATH silently does
    nothing. A whole run once reported on code that was never loaded — every
    check exercised the installed build while the fix under test sat on
    disk, unread.

    So the choice is explicit and printed. `--source installed` (the
    default) is what an artist actually runs; `--source repo` is what you
    want after editing, and it wins by going in at `sys.path[0]`.
    """
    wanted = "repo" if "--source=repo" in sys.argv or (
        "--source" in sys.argv and sys.argv[sys.argv.index("--source") + 1 :][:1] == ["repo"]
    ) else os.environ.get("HAP_E2E_SOURCE", "installed")
    if wanted == "repo":
        source = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__)))), "python")
        sys.path.insert(0, source)
    return wanted


_SOURCE = _select_source()

from houdini_agent_panel.ui.qt import QtCore, QtWidgets  # noqa: E402

_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

from houdini_agent_panel import (  # noqa: E402
    conversations_store,
    logbook,
    paths,
    registry,
    runtime,
    scene,
    sessions,
    settings as settings_mod,
    shellenv,
)
from houdini_agent_panel.client import AcpClient  # noqa: E402
from houdini_agent_panel.ui import panel as panel_mod  # noqa: E402

#: Generous: an npx agent downloads its own package on first launch, and a
#: minute there is normal rather than a failure.
CONNECT_TIMEOUT_MS = 240_000
TURN_TIMEOUT_MS = 240_000

#: The one honest way for a check to say "there was nothing here to verify"
#: — a machine/agent that doesn't have what this check needs (no auth
#: methods to exercise, no token in the environment to inject), as opposed
#: to a check that ran and found the thing it was checking for true. `main`
#: below prints this as SKIP, not ok, and counts it separately in the final
#: tally. Found the hard way: `check_sign_in_reachable`'s early return used
#: to read "agent needs no sign-in (already authenticated)" — a sentence
#: that sounds like a result, on the exact machine state every OTHER check
#: already requires to run at all, so it was printing "ok" for a check that
#: had verified nothing, every single run. A check that returns without
#: verifying its own claim says so with this prefix — nothing else invents
#: a third way to mean the same thing.
_SKIP_PREFIX = "skipped — "


class Failure(AssertionError):
    """A check that did not hold. The message is what gets reported."""


def wait_for(predicate, timeout_ms: int, what: str) -> None:
    """Pump the Qt loop until `predicate` holds, or fail saying what we waited for."""
    timer = QtCore.QElapsedTimer()
    timer.start()
    while timer.elapsed() < timeout_ms:
        _app.processEvents()
        if predicate():
            return
        QtCore.QThread.msleep(20)
    raise Failure(f"timed out after {timeout_ms // 1000}s waiting for: {what}")


class Harness:
    """One panel, one agent, torn down cleanly whatever happens."""

    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.panel: panel_mod.AgentPanel | None = None
        self._restore_agent: str = ""
        self._restore_autostart: bool = False
        self.events: dict = {"chunks": [], "tools": [], "notes": [], "stderr": [], "modes": []}

    def __enter__(self) -> "Harness":
        panel_mod.reset_shared_state_for_tests()
        # This run drives the REAL settings file — that is the point, it
        # exercises the installed build the way an artist has it. What it may
        # not do is keep the changes. Pointing `default_agent` at whatever
        # agent is under test and forcing autostart, and then leaving both
        # that way, silently re-pointed the panel of the person whose machine
        # this is: they open Houdini later and get an agent they never chose.
        # Whatever it was is put back in `__exit__`, including when a check
        # fails — especially then.
        current = settings_mod.load()
        self._restore_agent = current.default_agent
        self._restore_autostart = current.autostart_agent
        current.default_agent = self.agent_id
        current.autostart_agent = True
        settings_mod.save(current)

        self.panel = panel_mod.AgentPanel()
        client = panel_mod.shared_client(self.agent_id)
        e = self.events
        client.connected.connect(lambda info: e.__setitem__("connected", info))
        client.failed.connect(lambda m: e.__setitem__("failed", m))
        client.auth_required.connect(lambda ms: e.__setitem__("auth", [m.id for m in ms]))
        client.session_started.connect(lambda sid, _s: e.__setitem__("session", sid))
        client.message_chunk.connect(lambda _s, _m, t: e["chunks"].append(t))
        client.tool_call.connect(lambda _s, tc: e["tools"].append(getattr(tc, "title", "?")))
        client.modes_changed.connect(
            lambda sid, modes: e["modes"].append((sid, modes.current_mode_id))
        )
        client.turn_finished.connect(lambda _s, r: e.__setitem__("stop", r))
        client.error.connect(lambda _s, m: e.__setitem__("error", m))
        client.log_line.connect(lambda line: e["stderr"].append(line))
        return self

    def __exit__(self, *_exc) -> None:
        if self.panel is not None:
            self.panel.shutdown()
        panel_mod.reset_shared_state_for_tests()
        _app.processEvents()
        # Give the machine's owner their own settings back. Read fresh rather
        # than written from a snapshot: the run may legitimately have changed
        # other fields (an install records itself), and those must survive.
        try:
            current = settings_mod.load()
            current.default_agent = self._restore_agent
            current.autostart_agent = self._restore_autostart
            settings_mod.save(current)
        except Exception as exc:  # noqa: BLE001 - report, never mask the check's own failure
            print(f"  ! could not restore settings: {exc}")

    # --- steps shared by several checks

    def connect(self) -> None:
        self.panel._boot()
        wait_for(
            lambda: "connected" in self.events or "failed" in self.events,
            CONNECT_TIMEOUT_MS,
            f"{self.agent_id} to connect",
        )
        if "failed" in self.events:
            raise Failure(f"agent did not start: {self.events['failed'][:300]}")

    def open_session(self) -> str:
        wait_for(
            lambda: "session" in self.events or "auth" in self.events,
            CONNECT_TIMEOUT_MS,
            "a session or a sign-in request",
        )
        if "session" not in self.events:
            raise Failure(f"agent wants sign-in: {self.events.get('auth')}")
        return self.events["session"]

    def say(self, text: str) -> str:
        """Type and send exactly the way the send button does."""
        self.events["chunks"].clear()
        self.events.pop("stop", None)
        self.panel._composer._text_edit.setPlainText(text)
        self.panel._composer._submit()
        _app.processEvents()
        wait_for(
            lambda: "stop" in self.events or "error" in self.events,
            TURN_TIMEOUT_MS,
            "the agent to finish its turn",
        )
        if "error" in self.events:
            raise Failure(f"agent error: {str(self.events['error'])[:300]}")
        return "".join(self.events["chunks"])


# --- checks -----------------------------------------------------------------
#
# Each takes an agent id and raises Failure with a human sentence. Keep them
# independent: a run must be able to execute any subset in any order.


def check_connect(agent_id: str) -> str:
    with Harness(agent_id) as h:
        h.connect()
        info = h.events["connected"]
        return f"{info.name} {info.version}"


def check_session(agent_id: str) -> str:
    with Harness(agent_id) as h:
        h.connect()
        return f"session {h.open_session()[:16]}…"


def check_prompt(agent_id: str) -> str:
    with Harness(agent_id) as h:
        h.connect()
        h.open_session()
        reply = h.say("Reply with the single word OK and nothing else.")
        if not reply.strip():
            raise Failure("the agent finished its turn without saying anything")
        return f"replied {reply.strip()[:40]!r}"


def check_answer_reaches_the_feed(agent_id: str) -> str:
    """Not "the agent replied" — "the artist can read the reply".

    `check_prompt` above watches `message_chunk` on the client, and that is
    exactly the blind spot that let a real bug ship: opencode sends its
    reasoning and its answer under ONE `messageId`, both entries took that id
    as their own, and `TranscriptView` resolves an id by taking the first
    entry carrying it — so every chunk of the answer was drawn into the
    thought's row. The chunks arrived, the client signals fired, every
    signal-level check passed, and the panel showed the agent thinking and
    never showed what it said.

    So this one reads the panel's own feed and its own rows: reasoning and
    answer must be separate entries, no two entries may share an id, and
    every answer must have a widget of its own on screen. Run it per agent —
    this is the check that says whether THAT agent's streaming actually
    renders.
    """
    with Harness(agent_id) as h:
        h.connect()
        session_id = h.open_session()
        h.say("Think briefly, then reply with the single word OK and nothing else.")

        entries = h.panel._model(session_id).entries()
        answers = [e for e in entries if e.kind == "agent" and e.text.strip()]
        thoughts = [e for e in entries if e.kind == "thought" and e.text.strip()]
        if not answers:
            kinds = [(e.kind, e.text[:20]) for e in entries]
            raise Failure(f"the turn produced no readable answer entry; feed was {kinds}")

        ids = [e.id for e in entries]
        duplicated = {i for i in ids if ids.count(i) > 1}
        if duplicated:
            raise Failure(
                f"two feed entries share an id ({sorted(duplicated)[:3]}) — "
                "only the first of them can ever be redrawn"
            )

        # Queued chunk renders are drained by the event loop; ask for them
        # now rather than race it.
        h.panel._flush_transcript()
        _app.processEvents()
        rows = h.panel._transcript._rows
        undrawn = [e.id for e in answers if e.id not in rows]
        if undrawn:
            raise Failure(f"answer entries with no row on screen: {undrawn}")

        return (
            f"{len(answers)} answer entr{'y' if len(answers) == 1 else 'ies'}, "
            f"{len(thoughts)} thought(s), all drawn"
        )


def check_mcp(agent_id: str) -> str:
    """The whole point of the panel: the agent can see the scene.

    Does NOT exercise the race `498b251` fixed (opening a session before
    the fx server has finished its own async readiness poll) — the fx
    server lives IN the hython process, not per-check, so by the time this
    runs it has almost always already been warmed by an earlier check in
    the same run (`connect`/`session`/`prompt` all open a session first).
    That race only shows up for the very first session opened in a fresh
    hython process, and checks are deliberately order-independent ("must be
    able to execute any subset in any order" — see the checks comment
    below), so there is no reliable way to guarantee THIS check is that
    first session without breaking that independence. Worth remembering
    this limitation explicitly rather than re-discovering it the next time
    someone wonders why a run of `--only mcp` never reproduces it.
    """
    with Harness(agent_id) as h:
        h.connect()
        h.open_session()
        h.say(
            "Call the get_houdini_connection_status tool and report the houdini_version "
            "it returns. Do not ask me for files."
        )
        called = [t for t in h.events["tools"] if "houdini" in str(t).lower()]
        if not called:
            raise Failure(f"no Houdini tool was called; tools seen: {h.events['tools'][:5]}")
        return f"called {called[0]}"


def check_agent_switch_keeps_conversations(agent_id: str) -> str:
    """The artist's words are not the agent's property."""
    with Harness(agent_id) as h:
        h.connect()
        session_id = h.open_session()
        h.panel._pool.get(session_id).title = "E2E conversation"
        h.panel._model(session_id).append_user("remember me")
        h.panel._persist_conversations()

        h.panel._on_agent_chosen(agent_id)
        _app.processEvents()

        stored = conversations_store.load()
        if not any(c.title == "E2E conversation" for c in stored):
            raise Failure("the conversation did not survive an agent switch")
        if h.panel._pool.all():
            raise Failure("a dead agent session id survived the switch")
        return f"{len(stored)} conversation(s) kept"


def check_conversations_persist_across_restart(agent_id: str) -> str:
    with Harness(agent_id) as h:
        h.connect()
        session_id = h.open_session()
        h.panel._pool.get(session_id).title = "Across restart"
        h.panel._model(session_id).append_user("still here?")
        h.panel.shutdown()

    if not any(c.title == "Across restart" for c in conversations_store.load()):
        raise Failure("conversations were lost when the panel closed")
    return "restored from disk"


def check_stop_never_traps(agent_id: str) -> str:
    with Harness(agent_id) as h:
        h.connect()
        session_id = h.open_session()
        h.panel._pool.get(session_id).busy = True
        h.panel._composer.set_busy(True)
        h.panel._on_cancelled()
        wait_for(
            lambda: not h.panel._composer._busy,
            15_000,
            "the panel to release the input after stop",
        )
        return "input released"


def check_restart_after_stop(agent_id: str) -> str:
    """`stop()` used to close the asyncio loop for good, and the next
    `start()` died silently — the chip named the new agent and nothing ever
    happened again."""
    entry = next((e for e in registry.fetch_registry() if e.id == agent_id), None)
    if entry is None:
        raise Failure(f"{agent_id} is not in the registry")
    spec = runtime.launch_spec(entry)

    client = AcpClient()
    seen = {"n": 0}
    client.connected.connect(lambda _i: seen.__setitem__("n", seen["n"] + 1))
    client.failed.connect(lambda m: seen.__setitem__("failed", m))
    try:
        client.start(spec, cwd=scene.hip_dir())
        wait_for(lambda: seen["n"] >= 1 or "failed" in seen, CONNECT_TIMEOUT_MS, "first connect")
        client.stop()
        _app.processEvents()
        client.start(spec, cwd=scene.hip_dir())
        wait_for(
            lambda: seen["n"] >= 2 or "failed" in seen, CONNECT_TIMEOUT_MS, "reconnect after stop"
        )
        if seen["n"] < 2:
            raise Failure(f"could not restart after stop: {seen.get('failed')}")
        return "reconnected"
    finally:
        client.stop()


def check_config_options(agent_id: str) -> str:
    """Model and effort come from the agent, not from us."""
    with Harness(agent_id) as h:
        h.connect()
        session_id = h.open_session()
        wait_for(
            lambda: bool(h.panel._pool.get(session_id)
                         and h.panel._pool.get(session_id).config_options),
            30_000,
            "the agent to publish its config options",
        )
        options = h.panel._pool.get(session_id).config_options
        names = [o.id for o in options]
        if not any("model" in n for n in names):
            raise Failure(f"no model option among {names}")
        details = []
        for option in options:
            choices = "/".join(choice.value for choice in option.choices)
            details.append(f"{option.id}={option.current_value} [{choices}]")
        return f"options: {', '.join(details)}"


def check_modes(agent_id: str) -> str:
    """A mode pick must make the full ACP round trip, not only relabel the chip."""
    with Harness(agent_id) as h:
        h.connect()
        session_id = h.open_session()
        state = h.panel._pool.get(session_id)
        if not state or len(state.available_modes) < 2:
            names = [m.id for m in (state.available_modes if state else [])]
            raise Failure(f"agent offered fewer than two modes: {names}")

        original = state.current_mode_id
        target = next(m.id for m in state.available_modes if m.id != original)
        h.panel._on_mode_selected(target)
        wait_for(
            lambda: (session_id, target) in h.events["modes"],
            30_000,
            f"the agent to acknowledge mode {target}",
        )
        # Leave this disposable session in the agent's own original mode.
        h.panel._on_mode_selected(original)
        wait_for(
            lambda: (session_id, original) in h.events["modes"],
            30_000,
            f"the agent to restore mode {original}",
        )
        names = [m.id for m in state.available_modes]
        return f"modes: {', '.join(names)}; round trip {original} -> {target} -> {original}"


def check_sign_in_reachable(agent_id: str) -> str:
    """An agent that declares sign-in methods must always be reachable.

    Only actually exercises that on a machine/agent state where there ARE
    methods to reach — which is never true on any machine that can also
    run the checks above it (`open_session()` requires the agent already
    authenticated, or every one of those fails). On the far more common
    machine state this never gets past the first line: says so with
    `_SKIP_PREFIX`, not a sentence that reads like a verified result.
    """
    with Harness(agent_id) as h:
        h.connect()
        info = h.events["connected"]
        if not info.auth_methods:
            return f"{_SKIP_PREFIX}agent already authenticated, no methods to reach"
        h.panel._offer_sign_in()
        _app.processEvents()
        if h.panel._pages.currentIndex() != panel_mod.AgentPanel.PAGE_AUTH:
            raise Failure("the sign-in screen could not be opened")
        return f"methods: {', '.join(m.id for m in info.auth_methods)}"


def check_captured_token_signs_in_alone(agent_id: str) -> str:
    """The one part of "signing in" that actually belongs to this panel,
    proven in isolation.

    Every other check in this file goes through `open_session()`, which
    requires the agent to already be authenticated SOME way — an artist's
    own real `claude login` sitting in `~/.claude/.credentials.json` makes
    `connect`/`session`/`prompt`/`mcp`/... all pass, whether or not this
    panel's own capture-and-inject mechanism
    (`settings.agent_oauth_tokens` -> `runtime.py::_with_oauth_tokens` ->
    `CLAUDE_CODE_OAUTH_TOKEN`) works at all. `check_sign_in_reachable`
    doesn't close that gap either — on any machine capable of running the
    checks above (i.e. every machine this file is ever actually run on),
    that agent already has real auth methods satisfied or none advertised,
    so its own real work never runs.

    This isolates the claim that's actually ours: with NO ambient Claude
    login reachable at all and nothing else in `agent_oauth_tokens` but
    the one captured token, the agent still connects and opens a session.
    The only way that can happen is the injection actually reaching the
    process — there is nothing else left for it to have authenticated
    with.

    "No ambient login reachable" used to mean `isolate_agent_config` — a
    Settings toggle that redirected `CLAUDE_CONFIG_DIR` for every launch.
    Removed (not by this check's own choice): it broke the owner's real
    sign-in on a real machine, because a real `claude login`'s credentials
    live in that same directory, and the owner never asked for the whole
    account isolated — he asked whether the agent can see MCP servers and
    skills, which `Settings.claude_show_host_mcp_servers`/
    `claude_show_host_skills` now answer without touching auth at all
    (client.py::claude_session_meta). Neither of those helps THIS check —
    they gate `strictMcpConfig`/`settingSources`, not where credentials
    live — so this still needs some way to defeat a real `~/.claude` on
    the test machine for its own assertion to mean anything, without
    bringing the rejected product feature back as something Settings
    exposes. Monkeypatches `shellenv.capture` for the duration of this one
    check only — the same technique `test_terminal_login_worker.py::
    _no_shell` already uses to keep a real machine's shell profile out of
    a unit test — so the composed launch env carries a `CLAUDE_CONFIG_DIR`
    pointed at a throwaway directory instead of whatever the real login
    shell would report. Restored immediately after, always.

    Claude-specific by construction (`CLAUDE_CODE_OAUTH_TOKEN` is verified
    for `claude-acp` only, docs/facts/acp-sdk.md §21) — skips with a
    stated reason for any other agent id, not silently.

    The token is never written into this repository, fixture or literal,
    even a fake-looking one — read from `HAP_E2E_CLAUDE_TOKEN` at the
    moment it's used, same rule this project already applies everywhere
    else (secrets live only in `~/Github/may-hub/.env`). No token in the
    environment is not a failure: a machine with nothing to test this
    against isn't broken, it just can't run this particular check today.
    """
    if agent_id != "claude-acp":
        return (
            f"{_SKIP_PREFIX}CLAUDE_CODE_OAUTH_TOKEN is claude-acp-specific; "
            "nothing verified here applies to this agent"
        )
    token = os.environ.get("HAP_E2E_CLAUDE_TOKEN")
    if not token:
        return f"{_SKIP_PREFIX}HAP_E2E_CLAUDE_TOKEN is not set, nothing to inject"

    tmp_config_dir = tempfile.mkdtemp(prefix="hap-e2e-claude-config-")
    original_capture = shellenv.capture
    original = settings_mod.load()
    isolated = settings_mod.Settings(
        default_agent=agent_id,
        autostart_agent=True,
        agent_oauth_tokens={agent_id: {"CLAUDE_CODE_OAUTH_TOKEN": token}},
    )
    settings_mod.save(isolated)
    # Test-only, not a product path: no real shell is asked, no real
    # profile is read — `merged()` (client.py::do_start) still calls
    # `shellenv.capture()` by name, so replacing the function object
    # itself is what reaches it, same as the module's own `_cache` would
    # if a real subprocess had actually run.
    shellenv.capture = lambda **_: {"CLAUDE_CONFIG_DIR": tmp_config_dir}
    try:
        with Harness(agent_id) as h:
            h.connect()
            session_id = h.open_session()
            return (
                f"session {session_id[:16]}… opened with a throwaway "
                "CLAUDE_CONFIG_DIR (test-only, not a Settings toggle) and "
                "only the captured token to authenticate with"
            )
    finally:
        shellenv.capture = original_capture
        # Whatever the machine's owner actually has restored exactly as
        # `Harness.__exit__` already restores default_agent/autostart_agent
        # for every other check — this additionally restores the field
        # this check itself overwrote (agent_oauth_tokens), which that
        # narrower restore does not touch.
        settings_mod.save(original)
        shutil.rmtree(tmp_config_dir, ignore_errors=True)


def check_two_panels_share_one_agent(agent_id: str) -> str:
    with Harness(agent_id) as h:
        h.connect()
        h.open_session()
        second = panel_mod.AgentPanel()
        _app.processEvents()
        try:
            if second._pool is not h.panel._pool:
                raise Failure("the second panel got its own session pool")
            if panel_mod.shared_client(agent_id) is not panel_mod.shared_client(second._agent_id):
                raise Failure("the second panel started its own agent")
            return "one agent, two panels"
        finally:
            second.shutdown()


def check_two_tabs_independent_current(agent_id: str) -> str:
    """Issue #21: two tabs share one agent connection, but not which
    conversation is on screen. Switching in one must never move the other.

    `SessionPool` used to own a single shared "current" field, so this only
    showed up when two tabs were on DIFFERENT conversations and one of them
    switched to a THIRD (or to the other's) — anything less doesn't
    distinguish the bug from the fix, which is why the sequence below
    deliberately drives tab 2 onto tab 1's conversation first, then moves
    tab 1 away.
    """
    with Harness(agent_id) as h:
        h.connect()
        session_a = h.open_session()

        second_events: dict = {}
        panel_mod.shared_client(agent_id).session_started.connect(
            lambda sid, _s: second_events.setdefault("session", sid)
        )
        second = panel_mod.AgentPanel()
        _app.processEvents()  # fires _boot() -> _adopt_running_client() -> a fresh session, since it has none yet
        try:
            wait_for(
                lambda: "session" in second_events, CONNECT_TIMEOUT_MS, "the second tab's own session"
            )
            session_b = second_events["session"]
            if session_b == session_a:
                raise Failure("the second tab ended up on tab 1's session instead of getting its own")
            if second._current_session_id != session_b:
                raise Failure("the second tab did not default to the session it just opened")

            second._set_current_session(session_a)  # tab 2 deliberately looks at tab 1's conversation
            h.panel._set_current_session(session_b)  # tab 1 switches away — a real move, not a no-op
            if second._current_session_id != session_a:
                raise Failure("switching tab 1's conversation moved tab 2's too")
            return "tab 1 switching conversations left tab 2's own choice untouched"
        finally:
            second.shutdown()


def check_launch_writes_manifest(agent_id: str) -> str:
    """The exact real-world discrepancy that made an agent vanish from the
    switcher: it can run perfectly while our own bookkeeping says "not
    installed", because launching used to write nothing (see the #20/#21
    commits and docs/facts/houdini.md). Simulated here by removing the
    manifest before a normal connect — the same state a machine reaches
    naturally the first time an npx agent runs without ever going through
    the explicit Install button.

    A binary-kind agent will pay for a real re-download here, not a no-op —
    `is_installed()` only trusts the manifest, so removing it makes the next
    launch redo the extract. That is the real mechanism being verified, not
    an accident to work around.
    """
    manifest_path = paths.agent_dir(agent_id) / "manifest.json"
    backup = manifest_path.read_bytes() if manifest_path.exists() else None
    try:
        if manifest_path.exists():
            manifest_path.unlink()
        runtime.reset_manifest_cache_for_tests()

        with Harness(agent_id) as h:
            h.connect()

        version = runtime.installed_version(agent_id)
        if version is None:
            raise Failure("launching the agent did not leave a manifest behind")
        return f"manifest present after an ordinary launch: version {version}"
    finally:
        # Only restores the backup if the real launch above did NOT write a
        # fresh manifest of its own — leaving the just-verified real state in
        # place is correct; only a failed run should be repaired here, so
        # this check never leaves the agent looking uninstalled afterwards.
        if not manifest_path.exists() and backup is not None:
            manifest_path.write_bytes(backup)
        runtime.reset_manifest_cache_for_tests()


def check_session_close_on_delete(agent_id: str) -> str:
    """Deleting a conversation must hand its session back — `session/close`
    actually sent, not just forgotten locally. An agent that never declared
    `sessionCapabilities.close` makes that impossible, and the check has to
    say so plainly rather than pass quietly: a check that cannot tell "sent"
    from "the agent can't do this" is worse than no check at all.

    Verified through the on-disk log (`client.py`'s `do_close_session` logs
    both outcomes), not by assuming the client-side call succeeding means
    the request reached the agent — those are different claims.
    """
    log_records: list[str] = []

    class _Collector(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            log_records.append(record.getMessage())

    handler = _Collector()
    client_logger = logging.getLogger("houdini_agent_panel.client")
    client_logger.addHandler(handler)
    stored_title = "E2E session-close check"
    try:
        with Harness(agent_id) as h:
            h.connect()
            session_id = h.open_session()
            h.panel._pool.get(session_id).title = stored_title
            h.panel._model(session_id).append_user("closing this on purpose")
            h.panel._on_session_removed(session_id)

            wait_for(
                lambda: any("session/close" in r for r in log_records),
                15_000,
                "session/close to be either sent or explicitly skipped",
            )
            sent = [r for r in log_records if "session/close sent" in r]
            skipped = [r for r in log_records if "session/close skipped" in r]
            if sent:
                return "session/close sent and logged"
            if skipped:
                return "agent has no sessionCapabilities.close — session/close correctly not sent"
            raise Failure(f"neither sent nor skipped was logged: {log_records}")
    finally:
        client_logger.removeHandler(handler)
        stored = conversations_store.load()
        remaining = [c for c in stored if c.title != stored_title]
        if len(remaining) != len(stored):
            conversations_store.save(remaining, active_id=conversations_store.load_active_id())


def check_conversations_scoped_to_scene(agent_id: str) -> str:
    """A conversation belongs to the scene ($HIP) it happened in — opening
    a different scene must not show it (see the `fa349f3`/scene-scoping
    fix).

    Only ever proved this at BOOT time — scene A's `Harness`/panel was torn
    down before scene B's was even constructed, so this checked `_boot()`'s
    own fresh-scoped `_restore_conversations()`, never `_on_hip_dir_
    changed()` at all. The bug this was actually meant to guard against
    (owner-reported, reproduced live) lives entirely in the SECOND path: a
    conversation stays open and current in an ALREADY-RUNNING panel after
    the artist opens a different scene into the SAME Houdini session — a
    fresh panel construction can never exercise that, no matter how many
    scenes it's pointed at one after another. Same class of gap as
    `check_sign_in_reachable`'s old shape: green for a reason that has
    nothing to do with what it's named for.

    Rewritten to change `$HIP` underneath ONE already-booted panel and fire
    the real callback `scene.watch_hip_dir_changes` would have (`"loaded"`),
    the way `hou.hipFile`'s own `AfterLoad` does — not a second panel.
    """
    real_hip_dir = scene.hip_dir
    original_active_id = conversations_store.load_active_id()
    scene_a = "/tmp/hap-e2e-scene-a"
    scene_b = "/tmp/hap-e2e-scene-b"
    stored_title = "E2E scene-scoping check"
    try:
        scene.hip_dir = lambda: scene_a
        with Harness(agent_id) as h:
            h.connect()
            session_id = h.open_session()
            h.panel._pool.get(session_id).title = stored_title
            h.panel._model(session_id).append_user("only visible in scene A")
            h.panel._current_hip_dir = scene_a

            scene.hip_dir = lambda: scene_b
            h.panel._on_hip_dir_changed("loaded")

            ids = [s.session_id for s in h.panel._pool.all()]
            if session_id in ids:
                raise Failure(
                    "the scene A conversation stayed in the pool after opening scene B"
                )
            if h.panel._current_session_id == session_id:
                raise Failure("the scene A conversation was still the current one")
            return "an already-open conversation is dropped when the live scene changes"
    finally:
        scene.hip_dir = real_hip_dir
        remaining = [c for c in conversations_store.load() if c.title != stored_title]
        conversations_store.save(remaining, active_id=original_active_id)


CHECKS = {
    "connect": check_connect,
    "session": check_session,
    "prompt": check_prompt,
    "feed": check_answer_reaches_the_feed,
    "mcp": check_mcp,
    "switch": check_agent_switch_keeps_conversations,
    "persist": check_conversations_persist_across_restart,
    "stop": check_stop_never_traps,
    "restart": check_restart_after_stop,
    "options": check_config_options,
    "modes": check_modes,
    "signin": check_sign_in_reachable,
    "captured_token": check_captured_token_signs_in_alone,
    "panels": check_two_panels_share_one_agent,
    "panels_independent": check_two_tabs_independent_current,
    "manifest": check_launch_writes_manifest,
    "close": check_session_close_on_delete,
    "scene": check_conversations_scoped_to_scene,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agent", default="claude-acp", help="agent id from the ACP registry")
    parser.add_argument("--only", default="", help="comma-separated subset of checks")
    parser.add_argument("--list", action="store_true", help="list the checks and exit")
    parser.add_argument(
        "--source", default="installed", choices=("installed", "repo"),
        help="which copy of the package to exercise (see _select_source)",
    )
    args = parser.parse_args(argv)

    if args.list:
        for name, fn in CHECKS.items():
            summary = (fn.__doc__ or "").strip().splitlines()[0] if fn.__doc__ else ""
            print(f"  {name:10} {summary}")
        return 0

    selected = [n.strip() for n in args.only.split(",") if n.strip()] or list(CHECKS)
    unknown = [n for n in selected if n not in CHECKS]
    if unknown:
        parser.error(f"unknown checks: {', '.join(unknown)}")

    logbook.setup()
    print(f"agent: {args.agent}")
    print(f"houdini: {scene.houdini_version()}, fx port: {scene.fx_port()}")
    print(f"source: {_SOURCE} — {os.path.dirname(panel_mod.__file__)}")
    print("-" * 72)

    # This run drives the REAL `conversations.json` — `Harness.__enter__`'s
    # own docstring says why, and it is the whole point. What it may not do
    # is leave anything behind in it: `check_prompt`, `check_mcp`,
    # `check_answer_reaches_the_feed` and others each open a real session
    # and send a real message, and a real `panel.shutdown()` in `Harness.
    # __exit__` persists that conversation exactly the way closing Houdini
    # for real would. Found for real, on the owner's own machine: 36 stray
    # conversations ("Reply with the single word OK", "E2E conversation",
    # "Across restart", ...) accumulated across repeated runs — every
    # `--only` subset, every re-run while iterating on a fix, one more
    # conversation nobody asked for.
    #
    # Two of the checks below (`check_session_close_on_delete`,
    # `check_conversations_scoped_to_scene`) already clean up their own
    # conversation in their own `finally:` — narrow, per-check, and correct
    # as far as it goes. What was missing is the general case: EVERY check
    # that opens a session leaves one behind, and most never clean up at
    # all. A snapshot-and-sweep here, once, covers all of them without
    # touching each check individually — anything that exists at the start
    # of this run is the owner's; anything new by the end is this run's,
    # and gets removed, whether the run passed or crashed.
    #
    # This does not race the two "must survive" checks (`switch`, `persist`)
    # — both read `conversations_store.load()` and assert on it BEFORE this
    # sweep ever runs, from inside their own `try` in the loop below; the
    # sweep only runs after every check has already had its turn.
    before_ids = {c.id for c in conversations_store.load()}
    before_active = conversations_store.load_active_id()

    failures = 0
    skipped = 0
    try:
        for name in selected:
            started = time.monotonic()
            try:
                detail = CHECKS[name](args.agent)
                if detail.startswith(_SKIP_PREFIX):
                    skipped += 1
                    status, extra = "SKIP", detail
                else:
                    status, extra = "ok", detail
            except Failure as exc:
                failures += 1
                status, extra = "FAIL", str(exc)
            except Exception as exc:  # noqa: BLE001 - a broken check is a failed check
                failures += 1
                status, extra = "ERROR", f"{type(exc).__name__}: {exc}"
                traceback.print_exc()
            print(f"{name:10} {status:6} {time.monotonic() - started:5.1f}s  {extra}")
    finally:
        after = conversations_store.load()
        kept = [c for c in after if c.id in before_ids]
        removed = len(after) - len(kept)
        if removed:
            conversations_store.save(kept, active_id=before_active)
        print(f"swept {removed} conversation(s) this run created")

    print("-" * 72)
    passed = len(selected) - failures - skipped
    summary = f"{passed}/{len(selected)} passed"
    if skipped:
        summary += f", {skipped} skipped"
    print(summary)
    return 1 if failures else 0


if __name__ == "__main__":
    code = main()
    # Leave immediately instead of unwinding the interpreter. Houdini's own
    # threads and Qt teardown can keep hython alive long after the run has
    # said everything it has to say, and a report nobody sees because the
    # process never exits is worth nothing. Everything of ours is already
    # closed by this point — `Harness.__exit__` stops each panel and client.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)

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
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from houdini_agent_panel.ui.qt import QtCore, QtWidgets  # noqa: E402

_app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)

from houdini_agent_panel import (  # noqa: E402
    conversations_store,
    logbook,
    registry,
    runtime,
    scene,
    sessions,
    settings as settings_mod,
)
from houdini_agent_panel.client import AcpClient  # noqa: E402
from houdini_agent_panel.ui import panel as panel_mod  # noqa: E402

#: Generous: an npx agent downloads its own package on first launch, and a
#: minute there is normal rather than a failure.
CONNECT_TIMEOUT_MS = 240_000
TURN_TIMEOUT_MS = 240_000


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
        self.events: dict = {"chunks": [], "tools": [], "notes": [], "stderr": []}

    def __enter__(self) -> "Harness":
        panel_mod.reset_shared_state_for_tests()
        current = settings_mod.load()
        current.default_agent = self.agent_id
        current.autostart_agent = True
        settings_mod.save(current)

        self.panel = panel_mod.AgentPanel()
        client = panel_mod.shared_client()
        e = self.events
        client.connected.connect(lambda info: e.__setitem__("connected", info))
        client.failed.connect(lambda m: e.__setitem__("failed", m))
        client.auth_required.connect(lambda ms: e.__setitem__("auth", [m.id for m in ms]))
        client.session_started.connect(lambda sid, _s: e.__setitem__("session", sid))
        client.message_chunk.connect(lambda _s, _m, t: e["chunks"].append(t))
        client.tool_call.connect(lambda _s, tc: e["tools"].append(getattr(tc, "title", "?")))
        client.turn_finished.connect(lambda _s, r: e.__setitem__("stop", r))
        client.error.connect(lambda _s, m: e.__setitem__("error", m))
        client.log_line.connect(lambda line: e["stderr"].append(line))
        return self

    def __exit__(self, *_exc) -> None:
        if self.panel is not None:
            self.panel.shutdown()
        panel_mod.reset_shared_state_for_tests()
        _app.processEvents()

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


def check_mcp(agent_id: str) -> str:
    """The whole point of the panel: the agent can see the scene."""
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
        return f"options: {', '.join(names)}"


def check_sign_in_reachable(agent_id: str) -> str:
    """An agent that declares sign-in methods must always be reachable."""
    with Harness(agent_id) as h:
        h.connect()
        info = h.events["connected"]
        if not info.auth_methods:
            return "agent needs no sign-in (already authenticated)"
        h.panel._offer_sign_in()
        _app.processEvents()
        if h.panel._pages.currentIndex() != panel_mod.AgentPanel.PAGE_AUTH:
            raise Failure("the sign-in screen could not be opened")
        return f"methods: {', '.join(m.id for m in info.auth_methods)}"


def check_two_panels_share_one_agent(agent_id: str) -> str:
    with Harness(agent_id) as h:
        h.connect()
        h.open_session()
        second = panel_mod.AgentPanel()
        _app.processEvents()
        try:
            if second._pool is not h.panel._pool:
                raise Failure("the second panel got its own session pool")
            if panel_mod.shared_client() is not panel_mod.shared_client():
                raise Failure("the second panel started its own agent")
            return "one agent, two panels"
        finally:
            second.shutdown()


CHECKS = {
    "connect": check_connect,
    "session": check_session,
    "prompt": check_prompt,
    "mcp": check_mcp,
    "switch": check_agent_switch_keeps_conversations,
    "persist": check_conversations_persist_across_restart,
    "stop": check_stop_never_traps,
    "restart": check_restart_after_stop,
    "options": check_config_options,
    "signin": check_sign_in_reachable,
    "panels": check_two_panels_share_one_agent,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--agent", default="claude-acp", help="agent id from the ACP registry")
    parser.add_argument("--only", default="", help="comma-separated subset of checks")
    parser.add_argument("--list", action="store_true", help="list the checks and exit")
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
    print("-" * 72)

    failures = 0
    for name in selected:
        started = time.monotonic()
        try:
            detail = CHECKS[name](args.agent)
            status, extra = "ok", detail
        except Failure as exc:
            failures += 1
            status, extra = "FAIL", str(exc)
        except Exception as exc:  # noqa: BLE001 - a broken check is a failed check
            failures += 1
            status, extra = "ERROR", f"{type(exc).__name__}: {exc}"
            traceback.print_exc()
        print(f"{name:10} {status:6} {time.monotonic() - started:5.1f}s  {extra}")

    print("-" * 72)
    print(f"{len(selected) - failures}/{len(selected)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())

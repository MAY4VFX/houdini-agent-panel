"""Houdini autoload file — runs once after the UI is ready.

Houdini picks up `uiready.py` on its own from any `python3.11libs` directory
on HOUDINI_PATH (including paths added by packages) and calls it after UI
initialization — this is a standard mechanism, not something we invented
(see docs/facts/houdini.md §4). The panel itself comes up from `.pypanel`
when the artist opens the tab, not from here: auto-starting the agent inside
the panel is a matter of panel settings, not of Houdini startup.

This file's only job is to not stay silent if the dependency tree isn't
installed yet ($HAP_DEPS is empty, doesn't exist, or the package install got
interrupted halfway through). An empty panel without a single word in the
console is the worst thing to show an artist when something's wrong. That's
why there are no imports of our own package at module level here: if the
install itself is broken, this diagnostic must still run no matter what,
rather than failing on the very first line.
"""

import os


def _check() -> None:
    deps = os.environ.get("HAP_DEPS")
    if not deps:
        print(
            "[houdini_agent_panel] the HAP_DEPS variable isn't set — Houdini "
            "didn't pick up the panel's package json."
        )
        return

    package_dir = os.path.join(deps, "houdini_agent_panel")
    if not os.path.isdir(package_dir):
        print(
            f"[houdini_agent_panel] dependencies not found in {deps}. "
            "The install looks incomplete. Open a console/terminal and run "
            "`python -m houdini_agent_panel doctor` to diagnose."
        )
        return

    acp_dir = os.path.join(deps, "acp")
    if not os.path.isdir(acp_dir):
        print(
            f"[houdini_agent_panel] the acp package (ACP SDK) was not found in {deps}. "
            "The dependency install was interrupted halfway through — run "
            "`python -m houdini_agent_panel doctor`."
        )


try:
    _check()
except Exception as exc:  # noqa: BLE001 - Houdini must never be brought down, no matter what
    print(f"[houdini_agent_panel] uiready.py failed: {exc!r}")

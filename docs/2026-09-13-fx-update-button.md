# MCP upstream update button

The owner's log showed every click running `uvx --from fxhoudinimcp==2.14.2
python -m houdini_agent_panel install`, then failing with `No module named
houdini_agent_panel`. An isolated import check reproduced the failure, and the
worker argv regression test failed on the same missing installer requirement.

The worker now runs the current panel installer with the requested fx version
added to its environment. `install --fx-version` passes that exact requirement
through to pip in each Houdini dependency tree and checks the resulting metadata.
A failed dependency installation cannot be hidden by success on another tree.
Settings shows progress, an actionable error with retry, or installed/restart
required. A stale version-check result cannot restore the completed Update button.

Validation: worker process/argv, installer pin and postcondition, pip command,
settings lifecycle and existing panel wiring tests. `tests/smoke_fx_update.py`
drives the native Qt Settings button through real uv/pip into disposable prefs
and dependencies using the GitHub-built wheel; it does not launch real agents or
rewrite the owner's Houdini package files.

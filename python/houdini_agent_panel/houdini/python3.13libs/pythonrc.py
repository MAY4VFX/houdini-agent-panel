"""Houdini autoload file — runs once at Python startup, before `uiready.py`.

Its only job is to hand fxhoudinimcp's auto-start a port that is really
free before that auto-start binds anything — see `scene.pin_fx_port_env`.
Never raises: Houdini must start no matter what happens here.
"""

try:
    from houdini_agent_panel.scene import pin_fx_port_env

    pin_fx_port_env()
except Exception as exc:  # noqa: BLE001 - Houdini must never be brought down, no matter what
    print(f"[houdini_agent_panel] pythonrc.py failed: {exc!r}")

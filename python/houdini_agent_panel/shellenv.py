"""The environment the artist actually has, not the one Houdini was given.

A GUI application on macOS is launched by the window server, not by a shell,
so it inherits none of what `~/.zshrc` and `~/.zprofile` set up: no
`GEMINI_API_KEY`, no `GOOGLE_CLOUD_PROJECT`, no `HTTPS_PROXY`, no
`NODE_EXTRA_CA_CERTS`. Houdini is such an application, and everything the
panel launches inherits Houdini's blindness.

On top of that, the ACP SDK deliberately trims what it hands a subprocess to
six variables (`HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM`, `USER` — see
`acp.transports.DEFAULT_INHERITED_ENV_VARS`). Sensible as a default for a
library that cannot know its host; wrong for us, because the artist's agent
needs the artist's own credentials and their studio's proxy settings, and
there is nowhere else for those to come from.

Reported as: "Gemini says the login method is deprecated in the panel, but
my terminal explains it needs `GOOGLE_CLOUD_PROJECT`" — the terminal knew
because the terminal had the environment. Measured on the same machine: a
login shell exposes 52 variables, `GEMINI_API_KEY` among them; the agent the
panel launched saw six.

Zed solves this by spawning a login shell and keeping its environment for
everything it later launches. This is the same idea, with the same
constraints: it costs one subprocess, it is cached, and it never runs on the
thread Houdini paints with.

What this does NOT do is decide anything about the values. Nothing is
logged, nothing is filtered by name, nothing is "recognised" as a secret and
treated specially — the panel is a courier here, not a reader.

One thing IS filtered, and this paragraph is the exception to the one above:
`subprocess.run` inherits the calling process's FULL environment unless told
otherwise, and the calling process is Houdini — carrying `PYTHONPATH`,
`HAP_DEPS`, `HAP_PYTHON` because our OWN installer wrote them into Houdini's
package json for the panel's own benefit (`houdini_package.py`). Spawning the
"login shell" straight from `os.environ` doesn't ask the artist's `.zprofile`
for those — it just relays what was already there before the shell even
started, and a plain `env -0` can't tell the difference. Measured for real:
`fx_python()`'s own interpreter (deliberately NOT on `HAP_MCP_PATH`, chosen
because it carries its own matching `fxhoudinimcp` install — see
`install.py::_mcp_python`) received our `PYTHONPATH=$HAP_DEPS` anyway,
pointed at a deps tree built for a different Python's ABI, and
`pydantic_core`'s compiled extension failed to import — closing the MCP
connection in under a second. Nothing downstream (`merged()`, `client.py`)
had a reason to strip `PYTHONPATH`; it looked exactly like a value the
artist's own profile had set. So `capture()` starts the shell from a bare
minimum instead of the calling process's environment — the same six
variables the ACP SDK itself trims a subprocess to (see above) — and drops
anything named `HAP_*` from the result as a second, cheap line of defense
against this exact class of leak recurring some other way.
"""

from __future__ import annotations

import os
import subprocess
import threading

#: Variables the shell owns for its own session and that would be actively
#: wrong to carry into a child of a different process — a shell's idea of
#: what its terminal is, or which pty it belongs to, has nothing to do with
#: the agent. `PATH` is deliberately NOT here: a login shell's `PATH` is the
#: whole point (that is where `node`, `npx` and the agent binaries live).
_SHELL_OWN = frozenset(
    {
        "_",
        "OLDPWD",
        "PWD",
        "SHLVL",
        "TERM_SESSION_ID",
        "TERM_PROGRAM",
        "TERM_PROGRAM_VERSION",
    }
)

#: The ONLY variables the login-shell subprocess starts with — the same six
#: the ACP SDK itself trims a subprocess to (`acp.transports.
#: DEFAULT_INHERITED_ENV_VARS`, see the module docstring). Deliberately NOT
#: `os.environ` in full: the caller is Houdini, and Houdini's own
#: environment carries `PYTHONPATH`/`HAP_DEPS`/`HAP_PYTHON` — written there
#: by our OWN installer for the panel's own benefit
#: (`houdini_package.py::package_json`), not by anything the artist's
#: `.zprofile`/`.zshrc` ever set. `subprocess.run` inherits the full parent
#: environment unless told otherwise, and `env -0` cannot tell "the shell's
#: profile set this" from "this was already here before the shell started" —
#: so without this, capturing "the artist's real terminal" actually captures
#: "Houdini's own process env, plus whatever the profile adds on top",
#: quietly handing Houdini-internal variables to every agent the panel
#: launches. This is not a style choice: it is exactly how a deps tree built
#: for one Python's ABI ended up on `PYTHONPATH` for a DIFFERENT interpreter
#: the fx MCP server was deliberately launched with instead — the import
#: failed, the connection closed in under a second, and nothing in the
#: panel's own log said why (`docs/facts/acp-sdk.md`, MCP servers).
_SPAWN_BASE_KEYS = ("HOME", "LOGNAME", "PATH", "SHELL", "TERM", "USER")

#: Second, cheap line of defense: whatever the shell prints, a variable
#: named `HAP_*` is ours (the installer's own marker prefix — see
#: `houdini_package.py`) and can never legitimately be "the artist's
#: environment". Catches this exact leak even if some other future code path
#: starts the capture subprocess with a wider base env than
#: `_SPAWN_BASE_KEYS` again.
_OURS_PREFIX = "HAP_"

#: How long to wait for the shell. An interactive login shell is normally
#: well under two seconds (measured: 0.26–1.19s on a machine with a prompt
#: framework and a version manager), but a profile that talks to the network
#: can hang, and an agent that never launches because a shell stalled would
#: be a strictly worse bug than the one this fixes.
_TIMEOUT = 10.0

_cache: dict[str, str] | None = None
_lock = threading.Lock()


def reset_cache_for_tests() -> None:
    global _cache
    with _lock:
        _cache = None


def _shell() -> str:
    return os.environ.get("SHELL") or "/bin/sh"


def capture(*, force: bool = False) -> dict[str, str]:
    """Read a login shell's environment. Cached; never raises.

    Login AND interactive (`-ilc`), which is not the obvious choice and was
    got wrong here first. Reasoning from "keys belong in the profile" gives
    `-lc` — and on zsh, the default shell on macOS since Catalina, `-lc`
    reads `.zshenv` and `.zprofile` and specifically NOT `.zshrc`, because
    that one is interactive-only. In practice `.zshrc` is where people put
    their exports; measured on the machine that reported this, `zsh -lc`
    from a clean environment yields 14 variables and no `GEMINI_API_KEY`,
    while `zsh -ilc` yields 17 and finds it. The elegant-sounding version
    would have quietly kept the bug.

    It costs more: 0.3–1.2s here, against 0.04s, because an interactive
    shell runs prompt frameworks and version managers. Paid once per
    process, off the main thread, for the difference between an agent that
    authenticates and one that does not.

    On any failure — no shell, a profile that errors, a timeout — this
    returns an empty dict and the caller proceeds with what it had. An
    environment we could not read is a missing convenience, never a reason
    to refuse to launch.
    """
    global _cache
    with _lock:
        if _cache is not None and not force:
            return dict(_cache)

    captured: dict[str, str] = {}
    try:
        # NUL-separated: values legitimately contain newlines (a proxy
        # exclusion list, a multi-line key), and splitting on newlines would
        # quietly truncate them into nonsense.
        #
        # `env=` is deliberately a small, fixed dict, not `os.environ` —
        # see `_SPAWN_BASE_KEYS`. Starting the shell from our own full
        # environment would let Houdini's `PYTHONPATH`/`HAP_*` ride along as
        # if the artist's profile had set them.
        spawn_env = {k: os.environ[k] for k in _SPAWN_BASE_KEYS if k in os.environ}
        result = subprocess.run(
            [_shell(), "-ilc", "env -0"],
            capture_output=True,
            timeout=_TIMEOUT,
            check=False,
            env=spawn_env,
        )
        if result.returncode == 0:
            for chunk in result.stdout.split(b"\0"):
                if not chunk:
                    continue
                name, separator, value = chunk.partition(b"=")
                if not separator:
                    continue
                try:
                    key = name.decode("utf-8")
                    captured[key] = value.decode("utf-8")
                except UnicodeDecodeError:
                    continue
    except (OSError, subprocess.SubprocessError, ValueError):
        captured = {}

    for key in _SHELL_OWN:
        captured.pop(key, None)
    for key in [k for k in captured if k.startswith(_OURS_PREFIX)]:
        captured.pop(key, None)

    with _lock:
        _cache = dict(captured)
    return dict(captured)


def merged(base: dict[str, str], overrides: dict[str, str] | None = None) -> dict[str, str]:
    """`base`, widened by the login shell, then by `overrides`.

    Precedence, weakest first: what the SDK hands us, what the artist's
    shell says, what the artist typed into the panel's own settings. The
    last one wins because it is the only one they can see and edit here.
    """
    result = dict(base)
    result.update(capture())
    result.update(overrides or {})
    return result


__all__ = ["capture", "merged", "reset_cache_for_tests"]

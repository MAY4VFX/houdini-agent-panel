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
        result = subprocess.run(
            [_shell(), "-ilc", "env -0"],
            capture_output=True,
            timeout=_TIMEOUT,
            check=False,
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

#!/bin/sh
# Install the agent panel into Houdini — one command.
#
#   curl -fsSL <url>/install.sh | sh
#   curl -fsSL <url>/install.sh | sh -s -- --agents opencode
#
# Deliberately /bin/sh, not bash: minimal Linux images and some studio
# environments may not have bash at all, and "bash: not found" in response
# to "install my panel" is the worst possible first impression.
#
# What it does: finds a way to run the package from PyPI and hands it the
# install command. Doesn't put anything into the system itself, except uv —
# and only if there's nothing else to run it with.
set -eu

PACKAGE="houdini-agent-panel"

say() { printf '%s\n' "$*"; }
die() { printf '%s\n' "$*" >&2; exit 1; }

has() { command -v "$1" >/dev/null 2>&1; }

# uvx and pipx run the package without installing it into the system — for
# an installer that's exactly what's needed: it does its job and leaves,
# without a venv or system Python entries left behind.
if has uvx; then
    say "Installing via uvx…"
    exec uvx --from "$PACKAGE" python -m houdini_agent_panel install "$@"
fi

if has pipx; then
    say "Installing via pipx…"
    exec pipx run --spec "$PACKAGE" python -m houdini_agent_panel install "$@"
fi

# Neither is available. Bring in uv — a single static binary in the user's
# home folder, no root, no system package manager. The same trick Houdini
# itself uses by shipping its own Python.
if has curl; then
    say "Couldn't find uvx or pipx, fetching uv…"
    curl -LsSf https://astral.sh/uv/install.sh | sh
elif has wget; then
    say "Couldn't find uvx or pipx, fetching uv…"
    wget -qO- https://astral.sh/uv/install.sh | sh
else
    die "Need curl or wget to download anything. Install either one and try again."
fi

# uv's installer puts the binary here and asks you to reopen your shell; we
# have nowhere to reopen to, so we find it ourselves.
for candidate in "${XDG_BIN_HOME:-}/uvx" "$HOME/.local/bin/uvx" "$HOME/.cargo/bin/uvx"; do
    if [ -x "$candidate" ]; then
        say "Installing via $candidate…"
        exec "$candidate" --from "$PACKAGE" python -m houdini_agent_panel install "$@"
    fi
done

die "uv was installed, but uvx wasn't found. Open a new terminal and run:
    uvx --from $PACKAGE python -m houdini_agent_panel install"

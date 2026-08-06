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
#
# `--connect-timeout`/`--timeout` on both fetches below: measured for real
# on a studio machine whose firewall silently drops (not refuses) egress to
# some hosts unless a proxy is exported first — plain `curl`/`wget` with no
# timeout of their own then hang with ZERO output for minutes, past any
# patience a "one command to install" is supposed to need. 15s is generous
# for a TLS handshake to a CDN and still fails fast enough to read as "the
# network, not the installer" — which is the one sentence this needs to get
# across if it fails here.
if has curl; then
    say "Couldn't find uvx or pipx, fetching uv…"
    curl --connect-timeout 15 -LsSf https://astral.sh/uv/install.sh | sh
elif has wget; then
    say "Couldn't find uvx or pipx, fetching uv…"
    wget --timeout=15 -qO- https://astral.sh/uv/install.sh | sh
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

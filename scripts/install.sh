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
die() { log "FATAL: $*"; printf '%s\n' "$*" >&2; exit 1; }

has() { command -v "$1" >/dev/null 2>&1; }

# --- diagnostic log ---------------------------------------------------------
# A full transcript of this run, opened before anything that can fail and
# named so a second run never collides with the first — this is the only
# thing we have to go on for a run nobody can watch over the shoulder of
# (a friend testing the Windows one-liner; an artist filing an issue). It
# has to hold the actual commands and their actual output, not a summary —
# "the difference between a proxy failure and an auth failure was invisible
# all day" happened once already from a log that only kept a summary.
#
# Location: the OS-conventional log directory, same family the panel's own
# runtime log already lives under (see paths.py) — easy to find together,
# and to attach both if asked.
#
# `set +e` for this whole block, restored right after: the log is a nice-
# to-have diagnostic aid, and must never be the reason a WORKING install
# fails — a minimal environment missing `date`/`uname`/`id` (found for real
# testing this with an emptied PATH) must degrade the log, never crash the
# install over it.
set +e
case "$(uname -s 2>/dev/null || echo unknown)" in
    Darwin) _log_dir="$HOME/Library/Logs/HoudiniAgentPanel/install" ;;
    *)      _log_dir="${XDG_STATE_HOME:-$HOME/.local/state}/houdini-agent-panel/install-logs" ;;
esac
if ! mkdir -p "$_log_dir" 2>/dev/null; then
    _log_dir="${TMPDIR:-/tmp}"
fi
_log_stamp=$(date +%Y%m%dT%H%M%S 2>/dev/null)
[ -n "$_log_stamp" ] || _log_stamp="run"
LOG_FILE="$_log_dir/install-$_log_stamp-$$.log"
: > "$LOG_FILE" 2>/dev/null || LOG_FILE="${TMPDIR:-/tmp}/houdini-agent-panel-install-$$.log"
: > "$LOG_FILE" 2>/dev/null

log() { printf '%s\n' "$*" >> "$LOG_FILE" 2>/dev/null || true; }

log "=== houdini-agent-panel installer log ==="
log "started (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u 2>/dev/null || echo '?')"
log "invocation: install.sh $*"
log ""
log "--- machine ---"
log "uname -a: $(uname -a 2>/dev/null || echo '?')"
if has sw_vers; then
    log "sw_vers: $(sw_vers 2>/dev/null | tr '\n' ' ')"
elif [ -f /etc/os-release ]; then
    log "os-release: $(tr '\n' ' ' < /etc/os-release)"
fi
log "shell: \$0=$0 SHELL=${SHELL:-?}"
if [ -n "${BASH_VERSION:-}" ]; then log "running under bash $BASH_VERSION (as sh)"; fi
log "user: $(id -un 2>/dev/null || whoami 2>/dev/null || echo '?') uid=$(id -u 2>/dev/null || echo '?')"
if [ "$(id -u 2>/dev/null || echo 1)" = "0" ]; then
    log "elevated: yes (root)"
else
    log "elevated: no"
fi
log ""
log "--- network ---"
# Presence only — see the redaction note below. A proxy URL can carry a
# password, so even here we log whether it's set, never what it is.
for _v in HTTP_PROXY HTTPS_PROXY http_proxy https_proxy NO_PROXY no_proxy; do
    eval "_val=\${${_v}:-}"
    if [ -n "$_val" ]; then log "$_v: set"; else log "$_v: unset"; fi
done
log ""
log "--- environment (redacted: name and whether-set only, except a short safe allowlist) ---"
# `env | cut -d= -f1` for names, one per line, no values — a full `env`
# dump here is exactly how someone's API key ends up pasted into our issue
# tracker. PATH/HFS/HOUDINI_* are the exception: safe, and the first three
# things actually needed to tell "wrong hython" from "no network" apart.
env 2>/dev/null | cut -d= -f1 | sort | while IFS= read -r _name; do
    case "$_name" in
        PATH | HFS | HOUDINI_*)
            eval "_v=\${${_name}:-}"
            log "$_name=$_v"
            ;;
        *)
            log "$_name: set"
            ;;
    esac
done
log ""
set -e

say "Diagnostic log: $LOG_FILE"
say "If anything goes wrong, send that file."
say ""

# Runs a command, showing its output live on the console exactly as before
# while ALSO copying it into the log, and returns the command's OWN exit
# status — not `tee`'s, which is what a bare `cmd | tee file` gives you
# under plain POSIX `sh`: no `pipefail`, no bash `$PIPESTATUS`, both of
# which this script deliberately does without (see the header above). A
# status written to a file from inside the pipe's first stage, read back
# after, is the portable way round that — verified against both `/bin/sh`
# and `/bin/dash` before this shipped, including that `set -e` does NOT
# skip the status write when the wrapped command fails.
run_logged() {
    log "--- running: $* ---"
    _status_file="${TMPDIR:-/tmp}/.hap-install-status-$$"
    # `|| true`: without it, `set -e` aborts INSIDE the group the moment
    # "$@" fails, before `printf` ever writes the status file it depends
    # on — verified against both `/bin/sh` and `/bin/dash` before this
    # shipped; a bare `{ ...; } | tee ...` under `set -e` looked correct in
    # isolation and then silently lost every real exit code once called at
    # the top level instead of inside an `if`.
    { "$@" 2>&1; printf '%s' "$?" > "$_status_file"; } | tee -a "$LOG_FILE" || true
    _status=$(cat "$_status_file" 2>/dev/null || echo 111)
    rm -f "$_status_file" 2>/dev/null || true
    log "--- exit code: $_status ---"
    return "$_status"
}

# Copies every package json this run wrote into the log, by reusing what
# `install.py` already announced ("  package json: <path>") instead of
# re-deriving Houdini's on-disk layout a second time, here, in shell.
dump_package_files() {
    grep '^  package json: ' "$LOG_FILE" 2>/dev/null | while IFS= read -r _line; do
        _path=${_line#  package json: }
        if [ -f "$_path" ]; then
            log ""
            log "--- contents of $_path ---"
            cat "$_path" >> "$LOG_FILE" 2>/dev/null || true
        fi
    done
}

# Always the last thing that happens, success or failure — dies, `exit`s,
# and `set -e` aborts all trigger an EXIT trap the same way. This is why
# nothing below hands off with `exec`: replacing the process image skips
# traps entirely, and then the one line an unattended run absolutely needs
# — where the log is — would silently never print.
finish() {
    _code=$?
    dump_package_files
    log ""
    log "=== finished, exit code $_code ==="
    say ""
    say "Diagnostic log: $LOG_FILE"
    if [ "$_code" -ne 0 ]; then
        say "Something went wrong — send that file if you're asking for help."
    fi
}
trap finish EXIT

# uvx and pipx run the package without installing it into the system — for
# an installer that's exactly what's needed: it does its job and leaves,
# without a venv or system Python entries left behind.
if has uvx; then
    say "Installing via uvx…"
    run_logged uvx --from "$PACKAGE" python -m houdini_agent_panel install "$@"
    exit 0
fi

if has pipx; then
    say "Installing via pipx…"
    run_logged pipx run --spec "$PACKAGE" python -m houdini_agent_panel install "$@"
    exit 0
fi

# Neither is available. Bring in uv — a single static binary in the user's
# home folder, no root, no system package manager. The same trick Houdini
# itself uses by shipping its own Python.
#
# `--connect-timeout`/`--timeout` below: measured for real on a studio
# machine whose firewall silently drops (not refuses) egress to some hosts
# unless a proxy is exported first — plain `curl`/`wget` with no timeout of
# their own then hang with ZERO output for minutes, past any patience a
# "one command to install" is supposed to need. 15s is generous for a TLS
# handshake to a CDN and still fails fast enough to read as "the network,
# not the installer" — which is the one sentence this needs to get across
# if it fails here.
#
# Fetched to a file and run as a second, separate step — not `curl | sh` —
# so `run_logged` sees each half's REAL exit status instead of the "did the
# last command in the pipe succeed" a nested pipe would otherwise give it.
_uv_installer="${TMPDIR:-/tmp}/.hap-uv-install-$$.sh"
if has curl; then
    say "Couldn't find uvx or pipx, fetching uv…"
    run_logged curl --connect-timeout 15 -LsSf https://astral.sh/uv/install.sh -o "$_uv_installer"
elif has wget; then
    say "Couldn't find uvx or pipx, fetching uv…"
    run_logged wget --timeout=15 -qO "$_uv_installer" https://astral.sh/uv/install.sh
else
    die "Need curl or wget to download anything. Install either one and try again."
fi
run_logged sh "$_uv_installer"
rm -f "$_uv_installer" 2>/dev/null || true

# uv's installer puts the binary here and asks you to reopen your shell; we
# have nowhere to reopen to, so we find it ourselves.
for candidate in "${XDG_BIN_HOME:-}/uvx" "$HOME/.local/bin/uvx" "$HOME/.cargo/bin/uvx"; do
    if [ -x "$candidate" ]; then
        # `${candidate}`, not bare `$candidate…` — macOS's system `/bin/sh`
        # is bash 3.2 (stuck there since 2010, GPLv3), and under the UTF-8
        # locale macOS ships by default it misparses a `$var` immediately
        # followed by a multi-byte character (the ellipsis) as part of the
        # variable's own name — "candidate…: unbound variable", a hard
        # crash under `set -u`, in exactly the branch a fresh Mac with no
        # `uv` yet is guaranteed to take. `${candidate}` has an unambiguous
        # closing brace and isn't affected; `/bin/dash` and a modern Linux
        # bash never had this bug, which is why it wasn't caught testing
        # there.
        say "Installing via ${candidate}…"
        run_logged "$candidate" --from "$PACKAGE" python -m houdini_agent_panel install "$@"
        exit 0
    fi
done

die "uv was installed, but uvx wasn't found. Open a new terminal and run:
    uvx --from $PACKAGE python -m houdini_agent_panel install"

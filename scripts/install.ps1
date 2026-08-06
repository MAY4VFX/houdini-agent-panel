# Install the agent panel into Houdini — one command, Windows.
#
#   irm <url>/install.ps1 | iex
#
# With arguments, via an intermediate variable (otherwise PowerShell won't
# let them through the pipeline):
#   $args = '--agents','opencode'; irm <url>/install.ps1 | iex
#
# Same logic as install.sh: find something to run the package from PyPI
# with, and hand it the install command. Doesn't put anything into the
# system except uv, and only if there's nothing else to run it with.
#
# UNVERIFIED: there is no Windows machine in this project to run this
# script on for real. `install.sh` was tested live (mayfx02 and a real Mac,
# with and without a studio proxy, uvx present and absent, both Houdini
# versions) and this file was updated to match what that testing found —
# the same `-TimeoutSec` bound against a silently-dropped connection
# hanging forever, the same diagnostic-log approach, the same "fail with
# one readable line" intent — but none of it has actually been executed by
# PowerShell. Say so if reporting on this file; do not claim it works.

$ErrorActionPreference = 'Stop'
$Package = 'houdini-agent-panel'
$Extra = if ($args) { $args } else { @() }

function Test-Command($name) {
    $null -ne (Get-Command $name -ErrorAction SilentlyContinue)
}

# --- diagnostic log ----------------------------------------------------
# A full transcript of this run, opened before anything that can fail and
# named so a second run never collides with the first — the only thing we
# have to go on for a run nobody can watch over the shoulder of (a friend
# testing this exact one-liner; an artist filing an issue). It has to hold
# the actual commands and their actual output, not a summary — "the
# difference between a proxy failure and an auth failure was invisible all
# day" happened once already from a log that only kept a summary.
#
# `Start-Transcript` gives us the console-output half of that for free
# (native commands' stdout included) — but it knows nothing about the
# structured facts below (machine identity, which env vars are set), so
# those are still written out by hand, and written BEFORE the transcript
# starts: writing to the same file both ways at once, concurrently, is not
# a combination this project can verify is safe without a Windows machine.
$LogDir = Join-Path $env:LOCALAPPDATA 'HoudiniAgentPanel\install-logs'
try {
    New-Item -ItemType Directory -Force -Path $LogDir -ErrorAction Stop | Out-Null
} catch {
    $LogDir = $env:TEMP
}
$LogFile = Join-Path $LogDir "install-$(Get-Date -Format 'yyyyMMddTHHmmss')-$PID.log"

function Write-Diag($text) {
    # The log is a diagnostic aid, never the reason a working install
    # fails — a write that can't land (disk full, odd ACLs) is swallowed,
    # same principle as install.sh's own `log()`.
    try { Add-Content -Path $LogFile -Value $text -ErrorAction Stop } catch { }
}

Write-Diag "=== houdini-agent-panel installer log ==="
Write-Diag "started (UTC): $((Get-Date).ToUniversalTime().ToString('o'))"
Write-Diag "invocation: install.ps1 $($Extra -join ' ')"
Write-Diag ""
Write-Diag "--- machine ---"
Write-Diag "OS: $([System.Environment]::OSVersion.VersionString)"
Write-Diag "Arch: $env:PROCESSOR_ARCHITECTURE (64-bit OS: $([System.Environment]::Is64BitOperatingSystem))"
Write-Diag "PowerShell: $($PSVersionTable.PSVersion) edition=$($PSVersionTable.PSEdition)"
Write-Diag "User: $env:USERNAME"
try {
    # One line, deliberately — not backtick-continued. A trailing space
    # after a PowerShell line-continuation backtick breaks it silently,
    # and this file has no Windows machine to catch that on.
    $elevated = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    Write-Diag "Elevated: $elevated"
} catch {
    Write-Diag "Elevated: unknown ($($_.Exception.Message))"
}
Write-Diag ""
Write-Diag "--- network ---"
# Presence only — see the redaction note below. A proxy URL can carry a
# password, so even here we log whether it's set, never what it is.
foreach ($name in 'HTTP_PROXY', 'HTTPS_PROXY', 'http_proxy', 'https_proxy', 'NO_PROXY', 'no_proxy') {
    $val = [Environment]::GetEnvironmentVariable($name)
    if ($val) { Write-Diag "${name}: set" } else { Write-Diag "${name}: unset" }
}
Write-Diag ""
Write-Diag "--- environment (redacted: name and whether-set only, except a short safe allowlist) ---"
# Names only, no values — a full environment dump here is exactly how
# someone's API key ends up pasted into our issue tracker. PATH/HFS/
# HOUDINI_* are the exception: safe, and the first three things actually
# needed to tell "wrong hython" from "no network" apart.
foreach ($item in (Get-ChildItem Env: | Sort-Object Name)) {
    if ($item.Name -eq 'PATH' -or $item.Name -eq 'HFS' -or $item.Name -like 'HOUDINI_*') {
        Write-Diag "$($item.Name)=$($item.Value)"
    } else {
        Write-Diag "$($item.Name): set"
    }
}
Write-Diag ""

Write-Host "Diagnostic log: $LogFile"
Write-Host "If anything goes wrong, send that file."
Write-Host ""

$TranscriptStarted = $false
try {
    Start-Transcript -Path $LogFile -Append -ErrorAction Stop | Out-Null
    $TranscriptStarted = $true
} catch {
    Write-Host "Warning: could not start a diagnostic transcript ($($_.Exception.Message)); continuing without one."
}

# Always the last thing that happens on any exit path, success or failure —
# stops the transcript FIRST (so nothing races the log file), then copies
# in every package json this run wrote (reusing what install.py already
# announced — "  package json: <path>" — instead of re-deriving Houdini's
# on-disk layout a second time, here, in PowerShell), then prints the one
# line an unattended run absolutely needs: where the log is.
function Complete-Install($code) {
    if ($TranscriptStarted) {
        try { Stop-Transcript | Out-Null } catch { }
    }
    if (Test-Path $LogFile) {
        Select-String -Path $LogFile -Pattern '^  package json: ' -ErrorAction SilentlyContinue |
            ForEach-Object {
                $path = $_.Line -replace '^  package json: ', ''
                if (Test-Path $path) {
                    Write-Diag ""
                    Write-Diag "--- contents of $path ---"
                    Get-Content -Path $path -Raw -ErrorAction SilentlyContinue | ForEach-Object { Write-Diag $_ }
                }
            }
    }
    Write-Diag ""
    Write-Diag "=== finished, exit code $code ==="
    Write-Host ""
    Write-Host "Diagnostic log: $LogFile"
    if ($code -ne 0) {
        Write-Host "Something went wrong -- send that file if you're asking for help."
    }
}

try {
    # uvx and pipx run the package without installing it into the system:
    # the installer does its job and leaves, without a venv or system
    # Python entries left behind.
    if (Test-Command 'uvx') {
        Write-Host 'Installing via uvx…'
        & uvx --from $Package python -m houdini_agent_panel install @Extra
        Complete-Install $LASTEXITCODE
        exit $LASTEXITCODE
    }

    if (Test-Command 'pipx') {
        Write-Host 'Installing via pipx…'
        & pipx run --spec $Package python -m houdini_agent_panel install @Extra
        Complete-Install $LASTEXITCODE
        exit $LASTEXITCODE
    }

    Write-Host "Couldn't find uvx or pipx, fetching uv…"
    # -TimeoutSec: measured on install.sh's Unix counterpart that a network
    # silently dropping the connection (not refusing it — a firewall that
    # requires a proxy to be exported first, which we can't know about
    # here) leaves an un-timed-out request hanging with no output for
    # minutes. A bound turns that into a fast, readable failure instead.
    try {
        Invoke-RestMethod -TimeoutSec 15 https://astral.sh/uv/install.ps1 | Invoke-Expression
    } catch {
        Write-Diag "FATAL: could not download uv's installer: $($_.Exception.Message)"
        Complete-Install 1
        # One line, deliberately — same reasoning as the elevation check
        # above. `` `$env:HTTPS_PROXY `` (backtick-escaped) so the literal
        # text prints as advice, not today's actual value interpolated in.
        Write-Error ("Couldn't download uv's installer from astral.sh: " + $_.Exception.Message + "`nIf you're behind a proxy, set `$env:HTTPS_PROXY first and try again.")
        exit 1
    }

    # uv's installer asks you to reopen your shell; we have nowhere to
    # reopen to, so we look for the binary ourselves.
    $candidates = @(
        (Join-Path $env:USERPROFILE '.local\bin\uvx.exe'),
        (Join-Path $env:USERPROFILE '.cargo\bin\uvx.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            Write-Host "Installing via $candidate…"
            & $candidate --from $Package python -m houdini_agent_panel install @Extra
            Complete-Install $LASTEXITCODE
            exit $LASTEXITCODE
        }
    }

    Complete-Install 1
    Write-Error @"
uv was installed, but uvx wasn't found. Open a new terminal window and run:
    uvx --from $Package python -m houdini_agent_panel install
"@
} catch {
    Write-Diag "FATAL: $($_.Exception.Message)"
    Complete-Install 1
    throw
}

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
# Verified live on a stock Windows 11 (10.0.26200) VM with Houdini 22.0.368,
# Windows PowerShell 5.1, no uv/pipx/node and no real Python — the machine an
# artist actually has. That run is what the execution-policy handling and the
# Write-Host error reporting below are for; both were written blind before it
# and both were wrong. `install.sh` was tested live separately (mayfx02 and a
# real Mac, with and without a studio proxy, uvx present and absent, both
# Houdini versions), and the two files share the `-TimeoutSec` bound against a
# silently-dropped connection hanging forever, the diagnostic-log approach,
# and the "fail with one readable line" intent.

# Comments in this file may hold any character; string literals and code are
# ASCII only, and that is not a style preference. This file has no BOM, and
# Windows PowerShell 5.1 reads a BOM-less file as ANSI, not UTF-8 — so an em
# dash (E2 80 94) inside a string is decoded as three cp1252 characters, the
# last of which is U+201D, a curly closing quote. PowerShell accepts curly
# quotes as string delimiters, so the string ends early, the rest of the line
# is parsed as code, and the file fails with an unbalanced-brace error
# hundreds of lines away from the character that caused it. Cost an hour on
# the Windows 11 VM. The `irm | iex` path in the README decodes as UTF-8 and
# is immune; anyone who saves this file and runs it is not.

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
Write-Diag "--- execution policy ---"
# The single fact that was missing the first time this script met a real
# Windows: a stock client machine resolves to `Restricted`, uv's own
# installer refuses to run under it, and the log said nothing about why.
Write-Diag "effective: $(Get-ExecutionPolicy)"
try {
    foreach ($scope in (Get-ExecutionPolicy -List)) {
        Write-Diag "  $($scope.Scope): $($scope.ExecutionPolicy)"
    }
} catch {
    Write-Diag "  per-scope list unavailable ($($_.Exception.Message))"
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

# Every failure exit goes through here, and none of them through
# `Write-Error`: the `$ErrorActionPreference = 'Stop'` at the top of this
# file makes `Write-Error` a *terminating* error, so it throws instead of
# printing, and PowerShell renders the throw as a bare "System error.".
# Measured on the Windows 11 VM — the whole "if you're behind a proxy, set
# $env:HTTPS_PROXY first" paragraph, written precisely for the person
# reading that screen, was invisible to them.
# uv, pip and npm all write their progress to stderr, and under the
# `$ErrorActionPreference = 'Stop'` this file needs everywhere else,
# PowerShell turns every one of those lines into a *terminating* error the
# moment stderr is redirected instead of going straight to a console:
# `... *> install.txt`, a scheduled task, an SSH session onto a render node.
# Measured on the Windows 11 VM over SSH — the run died on uv's first
# progress line, halfway through installing, and blamed it on nothing in
# particular. An exit code is the only thing that actually reports whether a
# child worked, so the preference is relaxed for exactly as long as one runs.
function Invoke-Native {
    param([string] $Exe, [string[]] $Arguments)

    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Arguments
    } finally {
        $ErrorActionPreference = $previous
    }
}

function Exit-WithMessage($message) {
    Write-Diag "FATAL: $message"
    Complete-Install 1
    Write-Host ""
    Write-Host $message -ForegroundColor Red
    exit 1
}

try {
    # uvx and pipx run the package without installing it into the system:
    # the installer does its job and leaves, without a venv or system
    # Python entries left behind.
    if (Test-Command 'uvx') {
        Write-Host 'Installing via uvx...'
        Invoke-Native 'uvx' (@('--from', $Package, 'python', '-m', 'houdini_agent_panel', 'install') + $Extra)
        Complete-Install $LASTEXITCODE
        exit $LASTEXITCODE
    }

    if (Test-Command 'pipx') {
        Write-Host 'Installing via pipx...'
        Invoke-Native 'pipx' (@('run', '--spec', $Package, 'python', '-m', 'houdini_agent_panel', 'install') + $Extra)
        Complete-Install $LASTEXITCODE
        exit $LASTEXITCODE
    }

    Write-Host "Couldn't find uvx or pipx, fetching uv..."
    # Downloading and running are two separate steps here, and they used to
    # be one. They fail for unrelated reasons — a proxy blocks the download,
    # an execution policy blocks the run — and when one `try` covered both,
    # the run-time failure was reported with the download-time advice: the
    # Windows 11 VM told its user to check `$env:HTTPS_PROXY` about a
    # network request that had already succeeded.
    #
    # -TimeoutSec: measured on install.sh's Unix counterpart that a network
    # silently dropping the connection (not refusing it — a firewall that
    # requires a proxy to be exported first, which we can't know about
    # here) leaves an un-timed-out request hanging with no output for
    # minutes. A bound turns that into a fast, readable failure instead.
    try {
        $uvInstaller = Invoke-RestMethod -TimeoutSec 15 https://astral.sh/uv/install.ps1
    } catch {
        # One line, deliberately — same reasoning as the elevation check
        # above. `` `$env:HTTPS_PROXY `` (backtick-escaped) so the literal
        # text prints as advice, not today's actual value interpolated in.
        Exit-WithMessage ("Couldn't download uv's installer from astral.sh: " + $_.Exception.Message + "`nIf you're behind a proxy, set `$env:HTTPS_PROXY first and try again.")
    }

    # uv's installer checks `Get-ExecutionPolicy` itself and refuses to do
    # anything under `Restricted` — which is what a stock Windows client
    # resolves to, every scope `Undefined`. This script survives it (a
    # string run through `iex` is not a script file and is never policy-
    # checked, which is why `irm | iex` got this far at all), so the
    # failure lands one level down, inside somebody else's installer, and
    # reads as though our download broke. Measured on the Windows 11 VM:
    # that is exactly how the first real install attempt died.
    #
    # `-Scope Process` only — it lives in this PowerShell process and dies
    # with it. Nothing is left behind for the next shell, which keeps the
    # promise at the top of this file: nothing goes into the system but uv.
    $PolicyOk = @('Unrestricted', 'RemoteSigned', 'Bypass')
    if ((Get-ExecutionPolicy) -notin $PolicyOk) {
        Write-Diag "ExecutionPolicy is $(Get-ExecutionPolicy), setting Bypass for this process only"
        try {
            Set-ExecutionPolicy Bypass -Scope Process -Force -ErrorAction Stop
        } catch {
            Write-Diag "Set-ExecutionPolicy failed: $($_.Exception.Message)"
        }
    }
    # Checked again rather than assumed: a Group Policy (`MachinePolicy` /
    # `UserPolicy`) outranks the process scope and the `Set` above silently
    # changes nothing. A studio workstation is where that happens, so it
    # gets its own message — telling an artist to set `$env:HTTPS_PROXY`,
    # or to try again, would waste their afternoon on the wrong thing.
    if ((Get-ExecutionPolicy) -notin $PolicyOk) {
        Exit-WithMessage @"
Windows won't run uv's installer: the PowerShell execution policy is $(Get-ExecutionPolicy), and a policy set by your administrator overrides what this script can change for itself.
Ask whoever manages this machine for 'RemoteSigned', or install uv by hand from https://docs.astral.sh/uv/getting-started/installation/ and run this again.
"@
    }

    try {
        Invoke-Expression $uvInstaller
    } catch {
        Exit-WithMessage ("uv's installer failed: " + $_.Exception.Message)
    }

    # uv's installer asks you to reopen your shell; we have nowhere to
    # reopen to, so we look for the binary ourselves.
    $candidates = @(
        (Join-Path $env:USERPROFILE '.local\bin\uvx.exe'),
        (Join-Path $env:USERPROFILE '.cargo\bin\uvx.exe')
    )
    foreach ($candidate in $candidates) {
        if (Test-Path $candidate) {
            Write-Host "Installing via $candidate..."
            Invoke-Native $candidate (@('--from', $Package, 'python', '-m', 'houdini_agent_panel', 'install') + $Extra)
            Complete-Install $LASTEXITCODE
            exit $LASTEXITCODE
        }
    }

    Exit-WithMessage @"
uv was installed, but uvx wasn't found. Open a new terminal window and run:
    uvx --from $Package python -m houdini_agent_panel install
"@
} catch {
    Write-Diag "FATAL: $($_.Exception.Message)"
    Complete-Install 1
    throw
}

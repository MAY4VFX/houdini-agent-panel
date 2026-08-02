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

$ErrorActionPreference = 'Stop'
$Package = 'houdini-agent-panel'
$Extra = if ($args) { $args } else { @() }

function Test-Command($name) {
    $null -ne (Get-Command $name -ErrorAction SilentlyContinue)
}

# uvx and pipx run the package without installing it into the system: the
# installer does its job and leaves, without a venv or system Python
# entries left behind.
if (Test-Command 'uvx') {
    Write-Host 'Installing via uvx…'
    & uvx --from $Package python -m houdini_agent_panel install @Extra
    exit $LASTEXITCODE
}

if (Test-Command 'pipx') {
    Write-Host 'Installing via pipx…'
    & pipx run --spec $Package python -m houdini_agent_panel install @Extra
    exit $LASTEXITCODE
}

Write-Host "Couldn't find uvx or pipx, fetching uv…"
Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression

# uv's installer asks you to reopen your shell; we have nowhere to reopen
# to, so we look for the binary ourselves.
$candidates = @(
    (Join-Path $env:USERPROFILE '.local\bin\uvx.exe'),
    (Join-Path $env:USERPROFILE '.cargo\bin\uvx.exe')
)
foreach ($candidate in $candidates) {
    if (Test-Path $candidate) {
        Write-Host "Installing via $candidate…"
        & $candidate --from $Package python -m houdini_agent_panel install @Extra
        exit $LASTEXITCODE
    }
}

Write-Error @"
uv was installed, but uvx wasn't found. Open a new terminal window and run:
    uvx --from $Package python -m houdini_agent_panel install
"@

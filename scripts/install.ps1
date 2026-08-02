# Установка панели агента в Houdini — одной командой, Windows.
#
#   irm <адрес>/install.ps1 | iex
#
# С аргументами через промежуточную переменную (иначе PowerShell не даст их
# передать в конвейер):
#   $args = '--agents','opencode'; irm <адрес>/install.ps1 | iex
#
# Логика та же, что в install.sh: находим, чем запустить пакет с PyPI, и
# отдаём установку ему. Ничего в систему не кладём, кроме uv, и то лишь если
# запускать нечем.

$ErrorActionPreference = 'Stop'
$Package = 'houdini-agent-panel'
$Extra = if ($args) { $args } else { @() }

function Test-Command($name) {
    $null -ne (Get-Command $name -ErrorAction SilentlyContinue)
}

# uvx и pipx запускают пакет, не устанавливая его в систему: инсталлятор
# отработал и ушёл, не оставив ни venv, ни записей в системном Python.
if (Test-Command 'uvx') {
    Write-Host 'Ставлю через uvx…'
    & uvx --from $Package python -m houdini_agent_panel install @Extra
    exit $LASTEXITCODE
}

if (Test-Command 'pipx') {
    Write-Host 'Ставлю через pipx…'
    & pipx run --spec $Package python -m houdini_agent_panel install @Extra
    exit $LASTEXITCODE
}

Write-Host 'Не нашёл uvx и pipx, приношу uv…'
Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression

# Установщик uv просит перезайти в шелл; нам перезаходить некуда, поэтому
# ищем бинарь сами.
$candidates = @(
    (Join-Path $env:USERPROFILE '.local\bin\uvx.exe'),
    (Join-Path $env:USERPROFILE '.cargo\bin\uvx.exe')
)
foreach ($candidate in $candidates) {
    if (Test-Path $candidate) {
        Write-Host "Ставлю через $candidate…"
        & $candidate --from $Package python -m houdini_agent_panel install @Extra
        exit $LASTEXITCODE
    }
}

Write-Error @"
uv установился, но uvx не нашёлся. Открой новое окно терминала и выполни:
    uvx --from $Package python -m houdini_agent_panel install
"@

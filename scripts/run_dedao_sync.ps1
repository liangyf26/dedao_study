param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$ConfigPath = "config.yaml"
)

$ErrorActionPreference = "Stop"

Set-Location $ProjectRoot

$Exe = Join-Path $ProjectRoot ".venv\Scripts\dedao-sync.exe"
if (-not (Test-Path $Exe)) {
    $Exe = "py"
    & $Exe -m dedao_sync.cli sync --config $ConfigPath
    exit $LASTEXITCODE
}

& $Exe sync --config $ConfigPath
exit $LASTEXITCODE


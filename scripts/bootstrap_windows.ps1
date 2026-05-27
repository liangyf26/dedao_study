param(
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$Python = "py -3.13",
    [switch]$SkipPlaywrightInstall
)

$ErrorActionPreference = "Stop"

Set-Location $ProjectRoot

if (-not (Test-Path "config.yaml") -or -not (Test-Path ".env")) {
    Write-Host "Initializing config files..."
    py -m dedao_sync.cli init
}

if (-not (Test-Path ".venv")) {
    Write-Host "Creating .venv..."
    Invoke-Expression "$Python -m venv .venv"
}

$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"

Write-Host "Installing project dependencies..."
& $VenvPython -m pip install -e ".[dev]"

if (-not $SkipPlaywrightInstall) {
    Write-Host "Installing Playwright Chromium..."
    & $VenvPython -m playwright install chromium
}

Write-Host "Running doctor..."
& $VenvPython -m dedao_sync.cli doctor --config config.yaml --no-auth


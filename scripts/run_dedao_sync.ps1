[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$ProjectRoot = "",
    [string]$ConfigPath = "config.yaml",
    [ValidateSet("sync", "check", "retry-failed", "resummarize")]
    [string]$Command = "sync",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs = @()
)

$ErrorActionPreference = "Stop"

if (-not $ProjectRoot) {
    $ProjectRoot = Join-Path $PSScriptRoot ".."
}
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$LogsDir = Join-Path $ProjectRoot "logs"
New-Item -ItemType Directory -Force -Path $LogsDir | Out-Null

$TranscriptPath = Join-Path $LogsDir ("scheduled-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))
$TranscriptStarted = $false
try {
    Start-Transcript -Path $TranscriptPath -Append | Out-Null
    $TranscriptStarted = $true
} catch {
    Write-Warning "Unable to start PowerShell transcript: $_"
}

try {
    Set-Location $ProjectRoot

    $Exe = Join-Path $ProjectRoot ".venv\Scripts\dedao-sync.exe"
    if (Test-Path $Exe) {
        $Args = @($Command, "--config", $ConfigPath) + $ExtraArgs
    } else {
        $Exe = "py"
        $Args = @("-m", "dedao_sync.cli", $Command, "--config", $ConfigPath) + $ExtraArgs
    }

    Write-Host "[$(Get-Date -Format o)] Running dedao-sync $Command in $ProjectRoot"
    Write-Host "Config: $ConfigPath"
    if ($ExtraArgs.Count -gt 0) {
        Write-Host "ExtraArgs: $($ExtraArgs -join ' ')"
    }
    & $Exe @Args
    $ExitCode = $LASTEXITCODE
    Write-Host "[$(Get-Date -Format o)] dedao-sync $Command exited with code $ExitCode"
    exit $ExitCode
} finally {
    if ($TranscriptStarted) {
        Stop-Transcript | Out-Null
    }
}

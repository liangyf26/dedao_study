param(
    [string]$TaskName = "DedaoSyncToObsidian",
    [string]$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path,
    [string]$At = "08:00"
)

$ErrorActionPreference = "Stop"

$ScriptPath = Join-Path $ProjectRoot "scripts\run_dedao_sync.ps1"
if (-not (Test-Path $ScriptPath)) {
    throw "Run script not found: $ScriptPath"
}

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`" -ProjectRoot `"$ProjectRoot`""

$Trigger = New-ScheduledTaskTrigger -Daily -At $At
$Settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Settings $Settings `
    -Description "Sync Dedao transcripts into Obsidian and send Feishu report." `
    -Force

Write-Host "Registered task '$TaskName' at $At for $ProjectRoot"

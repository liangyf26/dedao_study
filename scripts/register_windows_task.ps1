[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$TaskName = "DedaoSyncToObsidian",
    [string]$ProjectRoot = "",
    [string]$ConfigPath = "config.yaml",
    [ValidateSet("sync", "check", "retry-failed", "resummarize")]
    [string]$Command = "sync",
    [string]$At = "08:00",
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$ExtraArgs = @()
)

$ErrorActionPreference = "Stop"

if (-not $ProjectRoot) {
    $ProjectRoot = Join-Path $PSScriptRoot ".."
}
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
$ScriptPath = Join-Path $ProjectRoot "scripts\run_dedao_sync.ps1"
if (-not (Test-Path $ScriptPath)) {
    throw "Run script not found: $ScriptPath"
}
$ScriptPath = (Resolve-Path -LiteralPath $ScriptPath).Path

$ExtraArgsArgument = ($ExtraArgs | ForEach-Object { "`"$($_ -replace '"', '\"')`"" }) -join " "
$ExtraArgsSwitch = if ($ExtraArgsArgument) { " $ExtraArgsArgument" } else { "" }

$Action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$ScriptPath`" -ProjectRoot `"$ProjectRoot`" -ConfigPath `"$ConfigPath`" -Command `"$Command`"$ExtraArgsSwitch"

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

Write-Host "Registered task '$TaskName' at $At for $ProjectRoot using $ConfigPath command $Command"

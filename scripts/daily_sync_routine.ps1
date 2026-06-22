<#
.DESCRIPTION
  Dedao daily sync routine:
  1. Run sync repeatedly until no new courses remain
  2. Retry all failed items repeatedly until no progress
  3. Generate a summary report with failure analysis
#>

param(
    [string]$ProjectRoot = "",
    [string]$ConfigPath = "config.yaml",
    [int]$MaxSyncPasses = 3,
    [int]$MaxRetryPasses = 3
)

$ErrorActionPreference = "Continue"
$ProgressPreference = "SilentlyContinue"

# ---- Resolve paths ----
if (-not $ProjectRoot) {
    $ProjectRoot = Join-Path $PSScriptRoot ".."
}
$ProjectRoot = (Resolve-Path -LiteralPath $ProjectRoot).Path
Set-Location $ProjectRoot

$Exe = Join-Path $ProjectRoot ".venv\Scripts\dedao-sync.exe"
$LogDir = Join-Path $ProjectRoot "logs"
$ReportDir = Join-Path $ProjectRoot "reports"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType Directory -Force -Path $ReportDir | Out-Null

$Timestamp = Get-Date -Format "yyyy-MM-dd_HHmmss"
$DateStr = Get-Date -Format "yyyy-MM-dd"
$RoutineLog = Join-Path $LogDir "routine-$DateStr.log"
$ReportPath = Join-Path $ReportDir "report-$Timestamp.md"

# ---- Logging helpers ----
function Write-Log {
    param([string]$Message, [string]$Level = "INFO")
    $line = "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [$Level] $Message"
    Write-Host $line
    Add-Content -Path $RoutineLog -Value $line -Encoding UTF8
}

function Invoke-DedaoCommand {
    param([string[]]$Arguments, [int]$TimeoutSeconds = 1800)
    $cmd = "& `"$Exe`" $($Arguments -join ' ')"
    Write-Log "Running: $cmd (timeout: ${TimeoutSeconds}s)"

    $pinfo = New-Object System.Diagnostics.ProcessStartInfo
    $pinfo.FileName = $Exe
    $pinfo.Arguments = $Arguments -join ' '
    $pinfo.UseShellExecute = $false
    $pinfo.RedirectStandardOutput = $true
    $pinfo.RedirectStandardError = $true
    $pinfo.WorkingDirectory = $ProjectRoot
    $pinfo.StandardOutputEncoding = [System.Text.Encoding]::UTF8
    $pinfo.StandardErrorEncoding = [System.Text.Encoding]::UTF8

    $process = New-Object System.Diagnostics.Process
    $process.StartInfo = $pinfo

    $outBuilder = New-Object System.Text.StringBuilder
    $errBuilder = New-Object System.Text.StringBuilder

    $outEvent = Register-ObjectEvent -InputObject $process -EventName OutputDataReceived -Action {
        param($sender, $e)
        if ($e.Data) {
            $Event.MessageData.AppendLine($e.Data) | Out-Null
        }
    } -MessageData $outBuilder

    $errEvent = Register-ObjectEvent -InputObject $process -EventName ErrorDataReceived -Action {
        param($sender, $e)
        if ($e.Data) {
            $Event.MessageData.AppendLine($e.Data) | Out-Null
        }
    } -MessageData $errBuilder

    try {
        $process.Start() | Out-Null
        $process.BeginOutputReadLine()
        $process.BeginErrorReadLine()

        $finished = $process.WaitForExit($TimeoutSeconds * 1000)
        if (-not $finished) {
            Write-Log "TIMEOUT after ${TimeoutSeconds}s - killing process" "WARN"
            $process.Kill()
            $process.WaitForExit(5000) | Out-Null
            $exitCode = -1
        } else {
            $exitCode = $process.ExitCode
        }
    } finally {
        Unregister-Event -SourceIdentifier $outEvent.Name -ErrorAction SilentlyContinue
        Unregister-Event -SourceIdentifier $errEvent.Name -ErrorAction SilentlyContinue
        $process.Dispose()
    }

    $output = ($outBuilder.ToString() + $errBuilder.ToString()) -split "`r?`n" | Where-Object { $_ }
    if ($output) {
        $output | ForEach-Object { Add-Content -Path $RoutineLog -Value $_ -Encoding UTF8 }
    }
    Write-Log "Exit code: $exitCode"
    return @{ ExitCode = $exitCode; Output = $output }
}

function Get-LatestRunStats {
    $result = Invoke-DedaoCommand -Arguments @("list", "--config", $ConfigPath, "--runs", "--limit", "1")
    $line = ($result.Output | Select-Object -Last 1) -as [string]
    if ($line -match 'discovered=(?<d>\d+).*new=(?<n>\d+).*skipped=(?<s>\d+).*success=(?<ok>\d+).*failed=(?<f>\d+).*missing=(?<m>\d+).*summary_failed=(?<sf>\d+)') {
        return @{
            Discovered = [int]$Matches['d']
            New        = [int]$Matches['n']
            Skipped    = [int]$Matches['s']
            Success    = [int]$Matches['ok']
            Failed     = [int]$Matches['f']
            Missing    = [int]$Matches['m']
            SummaryFailed = [int]$Matches['sf']
        }
    }
    Write-Log "Could not parse run stats from output" "WARN"
    return $null
}

function Get-FailedItems {
    $result = Invoke-DedaoCommand -Arguments @("list", "--config", $ConfigPath, "--failed")
    $items = @()
    foreach ($line in $result.Output) {
        $s = $line -as [string]
        $parts = $s -split "`t", 5
        if ($parts.Count -ge 4 -and $parts[0] -match '^\d+$') {
            $items += @{
                Status   = $parts[1].Trim()
                Column   = $parts[2].Trim()
                Title    = $parts[3].Trim()
                Path     = if ($parts.Count -ge 5) { $parts[4].Trim() } else { "" }
            }
        }
    }
    return $items
}

# ============================================================
# PHASE 1: Sync loop
# ============================================================
Write-Log "========== PHASE 1: SYNC =========="

$syncPass = 0
$totalSynced = 0
do {
    $syncPass++
    Write-Log "Sync pass $syncPass / $MaxSyncPasses"

    $result = Invoke-DedaoCommand -Arguments @("sync", "--config", $ConfigPath) -TimeoutSeconds 3600

    $stats = Get-LatestRunStats
    if ($stats) {
        Write-Log "Run stats: discovered=$($stats.Discovered) new=$($stats.New) skipped=$($stats.Skipped) success=$($stats.Success) failed=$($stats.Failed) missing=$($stats.Missing) summary_failed=$($stats.SummaryFailed)"
        $totalSynced += $stats.Success

        if ($stats.New -eq 0) {
            Write-Log "No new items found. Sync phase complete."
            break
        }
    } else {
        Write-Log "Could not determine if sync found new items. Stopping sync loop." "WARN"
        break
    }
} while ($syncPass -lt $MaxSyncPasses)

# ============================================================
# PHASE 2: Retry-failed loop
# ============================================================
Write-Log "========== PHASE 2: RETRY FAILED =========="

$retryPass = 0
$totalRetried = 0
$failedBefore = (Get-FailedItems).Count
Write-Log "Failed items before retry: $failedBefore"

do {
    $retryPass++
    Write-Log "Retry pass $retryPass / $MaxRetryPasses"

    $result = Invoke-DedaoCommand -Arguments @("retry-failed", "--config", $ConfigPath) -TimeoutSeconds 1800

    $failedAfter = (Get-FailedItems).Count
    $fixed = $failedBefore - $failedAfter
    $totalRetried += [Math]::Max(0, $fixed)
    Write-Log "Failed items after retry: $failedAfter (fixed this pass: $([Math]::Max(0, $fixed)))"

    if ($failedAfter -eq 0) {
        Write-Log "No more failed items. Retry phase complete."
        break
    }

    if ($failedAfter -ge $failedBefore) {
        Write-Log "No progress on retry. Stopping retry loop." "WARN"
        break
    }

    $failedBefore = $failedAfter
} while ($retryPass -lt $MaxRetryPasses)

# ============================================================
# PHASE 3: Analyze & Report
# ============================================================
Write-Log "========== PHASE 3: REPORT =========="

$remainingFailed = Get-FailedItems
$failedByStatus = $remainingFailed | Group-Object -Property Status | ForEach-Object { @{ Status = $_.Name; Count = $_.Count } }
$failedByColumn = $remainingFailed | Group-Object -Property Column | ForEach-Object { @{ Column = $_.Name; Count = $_.Count } }

# Scan today's log for failure reasons
$logPath = Join-Path $LogDir "$DateStr.log"
$logContent = Get-Content $logPath -ErrorAction SilentlyContinue
$warnings = $logContent | Select-String -Pattern "WARNING|ERROR" | Select-Object -Last 50
$reasonCategories = @{}
foreach ($line in $warnings) {
    $s = $line -as [string]
    if ($s -match 'missing transcript.*:\s*(.+)$') {
        $reason = $Matches[1].Trim()
        if (-not $reasonCategories.ContainsKey($reason)) { $reasonCategories[$reason] = 0 }
        $reasonCategories[$reason] += 1
    } elseif ($s -match '(summary failed|summary_failed).*') {
        if (-not $reasonCategories.ContainsKey('summary_api_failure')) { $reasonCategories['summary_api_failure'] = 0 }
        $reasonCategories['summary_api_failure'] += 1
    } elseif ($s -match 'fetch.*fail|download.*fail|request.*fail') {
        if (-not $reasonCategories.ContainsKey('fetch_failure')) { $reasonCategories['fetch_failure'] = 0 }
        $reasonCategories['fetch_failure'] += 1
    } elseif ($s -match '(timeout|retry|connection)') {
        if (-not $reasonCategories.ContainsKey('network_issue')) { $reasonCategories['network_issue'] = 0 }
        $reasonCategories['network_issue'] += 1
    }
}

# Build report with StringBuilder to avoid here-string encoding issues
$sb = New-Object System.Text.StringBuilder
[void]$sb.AppendLine("# dedao sync daily report - $DateStr")
[void]$sb.AppendLine()
[void]$sb.AppendLine("## Summary")
[void]$sb.AppendLine()
[void]$sb.AppendLine("| Metric | Value |")
[void]$sb.AppendLine("|--------|-------|")
[void]$sb.AppendLine("| Sync passes | $syncPass |")
[void]$sb.AppendLine("| Synced today | $totalSynced |")
[void]$sb.AppendLine("| Retry passes | $retryPass |")
[void]$sb.AppendLine("| Retry-fixed | $totalRetried |")
[void]$sb.AppendLine("| Remaining failed | $($remainingFailed.Count) |")
[void]$sb.AppendLine()

[void]$sb.AppendLine("## Failure reason analysis")
[void]$sb.AppendLine()
if ($reasonCategories.Count -gt 0) {
    [void]$sb.AppendLine("| Reason | Count |")
    [void]$sb.AppendLine("|--------|-------|")
    foreach ($key in $reasonCategories.Keys) {
        [void]$sb.AppendLine("| $key | $($reasonCategories[$key]) |")
    }
} else {
    [void]$sb.AppendLine("No clear failure patterns detected in logs.")
}
[void]$sb.AppendLine()

[void]$sb.AppendLine("## Failed by status")
[void]$sb.AppendLine()
[void]$sb.AppendLine("| Status | Count |")
[void]$sb.AppendLine("|--------|-------|")
foreach ($item in $failedByStatus) {
    [void]$sb.AppendLine("| $($item.Status) | $($item.Count) |")
}
[void]$sb.AppendLine()

[void]$sb.AppendLine("## Failed by column")
[void]$sb.AppendLine()
[void]$sb.AppendLine("| Column | Count |")
[void]$sb.AppendLine("|--------|-------|")
foreach ($item in $failedByColumn) {
    [void]$sb.AppendLine("| $($item.Column) | $($item.Count) |")
}
[void]$sb.AppendLine()

if ($remainingFailed.Count -gt 0) {
    [void]$sb.AppendLine("## Remaining failed items")
    [void]$sb.AppendLine()
    [void]$sb.AppendLine("| Status | Column | Title |")
    [void]$sb.AppendLine("|--------|--------|-------|")
    foreach ($item in $remainingFailed) {
        $title = if ($item.Title.Length -gt 50) { $item.Title.Substring(0, 50) + "..." } else { $item.Title }
        [void]$sb.AppendLine("| $($item.Status) | $($item.Column) | $title |")
    }
} else {
    [void]$sb.AppendLine("## All clear - no remaining failed items")
}
[void]$sb.AppendLine()

[void]$sb.AppendLine("---")
[void]$sb.AppendLine("*Report generated: $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')*")
[void]$sb.AppendLine("*Config: $ConfigPath*")

$report = $sb.ToString()

# Write report
$report | Out-File -FilePath $ReportPath -Encoding UTF8
Write-Log "Report saved to: $ReportPath"

# Print report to stdout for cron capture
Write-Host "`n========================================"
Write-Host $report
Write-Host "========================================"

# ============================================================
# Summary exit
# ============================================================
Write-Log "========== ROUTINE COMPLETE =========="
Write-Log "Synced: $totalSynced | Retry-fixed: $totalRetried | Remaining: $($remainingFailed.Count)"

if ($remainingFailed.Count -eq 0) {
    Write-Log "All clear!"
} else {
    Write-Log "Routine finished with $($remainingFailed.Count) unresolved items." "WARN"
}
exit 0

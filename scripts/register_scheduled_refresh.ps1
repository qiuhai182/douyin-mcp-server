# register_scheduled_refresh.ps1
# ---------------------------------------------------------------------------
# Compatibility wrapper. The schedule/time is owned by douyin-mcp-server/
# schedule.json (edited in the WebUI「定时任务」panel); nothing here is
# hard-coded to 03:00 anymore.
#
# The WebUI re-registers the task automatically on save. This wrapper stays
# for command-line use:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\register_scheduled_refresh.ps1
#   powershell ... -Time 04:30            # one-off override
#   powershell ... -Remove                # remove the task
# ---------------------------------------------------------------------------
param(
    [string]$Time = "",
    [string]$TaskName = "DouyinRefreshDaily",
    [switch]$Remove
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$root = Split-Path $PSScriptRoot -Parent
$generic = Join-Path $PSScriptRoot "register_scheduled_task.ps1"

if ($Remove) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File $generic -Action delete -TaskName $TaskName
    exit $LASTEXITCODE
}

$weekdays = ""
$interval = 1
if (-not $Time) {
    $cfgPath = Join-Path $root "schedule.json"
    if (-not (Test-Path -LiteralPath $cfgPath)) {
        Write-Output "ERR|schedule.json not found; cannot read refresh time"
        exit 1
    }
    $cfg = Get-Content -LiteralPath $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $task = $cfg.tasks.refresh
    if (-not $task -or -not $task.time) {
        Write-Output "ERR|schedule.json missing tasks.refresh.time"
        exit 1
    }
    $Time = [string]$task.time
    if ($task.weekdays) { $weekdays = ($task.weekdays -join ",") }
    if ($task.interval_days) { $interval = [int]$task.interval_days }
}

$script = Join-Path $root "scripts\scheduled_refresh.py"
& powershell -NoProfile -ExecutionPolicy Bypass -File $generic -Action register `
    -TaskName $TaskName -ScriptPath $script -Arguments "--once" `
    -Time $Time -Weekdays $weekdays -IntervalDays $interval
exit $LASTEXITCODE

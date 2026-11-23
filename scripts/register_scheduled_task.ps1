# register_scheduled_task.ps1
# ---------------------------------------------------------------------------
# Generic Windows Task Scheduler registration used by the WebUI.
# The schedule (time / weekday range / interval) always comes from
# douyin-mcp-server/schedule.json via scripts/schedule_config.py -- this file
# must not hard-code any time.
#
# Actions:
#   register  create or overwrite the task
#   query     report the task state + next run
#   delete    remove the task (idempotent)
#   list      enumerate every task whose action points into this project
#
# Output (one line, ASCII only so PS5 without BOM is safe):
#   OK|action|TaskName|State|NextRun|LastTaskResult
#   ITEM|TaskName|State|NextRun|LastTaskResult|TaskPath|Execute|Arguments
#   ERR|message
# ---------------------------------------------------------------------------
param(
    [ValidateSet("register", "query", "delete", "list")]
    [string]$Action = "register",
    [string]$Root = "",
    [string]$TaskName = "DouyinRefreshDaily",
    [string]$ScriptPath = "",
    [string]$Arguments = "--once",
    [string]$Time = "03:00",
    [string]$Weekdays = "",
    [int]$IntervalDays = 1,
    [int]$ExecutionHours = 6,
    [string]$Label = ""
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Emit-Ok([string]$act, [string]$name, $task, $info) {
    $state = if ($task) { [string]$task.State } else { "" }
    $next = if ($info -and $info.NextRunTime) { $info.NextRunTime.ToString("yyyy-MM-dd HH:mm") } else { "" }
    $last = if ($info) { [string]$info.LastTaskResult } else { "" }
    Write-Output ("OK|{0}|{1}|{2}|{3}|{4}" -f $act, $name, $state, $next, $last)
}

try {
    if ($Action -eq "query") {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if (-not $task) {
            Write-Output ("ERR|task not registered: " + $TaskName)
            exit 0
        }
        $info = Get-ScheduledTaskInfo -TaskName $TaskName -ErrorAction SilentlyContinue
        Emit-Ok "query" $TaskName $task $info
        exit 0
    }

    if ($Action -eq "delete") {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($task) {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
        }
        Emit-Ok "delete" $TaskName $null $null
        exit 0
    }

    if ($Action -eq "list") {
        $probe = if ($Root) { $Root.TrimEnd("\") } else { (Split-Path $PSScriptRoot -Parent).TrimEnd("\") }
        $probe = $probe.ToLower()
        $tasks = @(Get-ScheduledTask -ErrorAction SilentlyContinue)
        foreach ($t in $tasks) {
            $hit = $false
            foreach ($a in @($t.Actions)) {
                $blob = ""
                if ($a.Execute) { $blob = $blob + " " + $a.Execute }
                if ($a.Arguments) { $blob = $blob + " " + $a.Arguments }
                if ($a.WorkingDirectory) { $blob = $blob + " " + $a.WorkingDirectory }
                if ($blob -and $blob.ToLower().Contains($probe)) { $hit = $true; break }
            }
            if (-not $hit) { continue }
            $info = Get-ScheduledTaskInfo -TaskName $t.TaskName -TaskPath $t.TaskPath -ErrorAction SilentlyContinue
            $next = if ($info -and $info.NextRunTime) { $info.NextRunTime.ToString("yyyy-MM-dd HH:mm") } else { "" }
            $last = if ($info) { [string]$info.LastTaskResult } else { "" }
            $act = @($t.Actions) | Select-Object -First 1
            $exec = if ($act) { [string]$act.Execute } else { "" }
            $args = if ($act) { [string]$act.Arguments } else { "" }
            Write-Output ("ITEM|{0}|{1}|{2}|{3}|{4}|{5}|{6}" -f `
                $t.TaskName, [string]$t.State, $next, $last, $t.TaskPath, $exec, $args)
        }
        Emit-Ok "list" "" $null $null
        exit 0
    }

    # ---- register ----
    if (-not $ScriptPath) { throw "ScriptPath is required" }
    if (-not (Test-Path -LiteralPath $ScriptPath)) { throw "entry script not found: $ScriptPath" }

    $root = if ($Root) { $Root } else { Split-Path $PSScriptRoot -Parent }
    $pyw = Join-Path $root ".venv\Scripts\pythonw.exe"
    if (-not (Test-Path -LiteralPath $pyw)) { $pyw = "pythonw.exe" }

    if ($Weekdays) {
        $parts = $Weekdays.Split(",") | ForEach-Object { $_.Trim() } | Where-Object { $_ -ne "" }
        $days = @($parts | ForEach-Object { [int]$_ } | Where-Object { $_ -ge 1 -and $_ -le 7 })
    } else {
        $days = @()
    }

    $hh = 0; $mm = 0
    $bits = $Time.Split(":")
    if ($bits.Count -ge 2) {
        $hh = [int]$bits[0]
        $mm = [int]$bits[1]
    }
    $base = (Get-Date).Date
    $at = New-Object System.DateTime($base.Year, $base.Month, $base.Day, $hh, $mm, 0)

    $taskAction = New-ScheduledTaskAction -Execute $pyw `
        -Argument ('"{0}" {1}' -f $ScriptPath, $Arguments) -WorkingDirectory $root

    if ($days.Count -gt 0 -and $days.Count -lt 7) {
        $weekNames = @("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
        $names = @($days | ForEach-Object { $weekNames[$_ - 1] })
        $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek $names -At $at
    } else {
        if ($IntervalDays -lt 1) { $IntervalDays = 1 }
        $trigger = New-ScheduledTaskTrigger -Daily -DaysInterval $IntervalDays -At $at
    }

    $principal = New-ScheduledTaskPrincipal -UserId ("{0}\{1}" -f $env:USERDOMAIN, $env:USERNAME) `
        -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun `
        -ExecutionTimeLimit (New-TimeSpan -Hours $ExecutionHours) `
        -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

    Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $trigger `
        -Principal $principal -Settings $settings -Force | Out-Null

    $task = Get-ScheduledTask -TaskName $TaskName
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Emit-Ok "register" $TaskName $task $info
    exit 0
}
catch {
    Write-Output ("ERR|" + $_.Exception.Message)
    exit 1
}

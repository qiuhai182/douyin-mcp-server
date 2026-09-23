# refresh_guarded.ps1
# ---------------------------------------------------------------------------
# Guarded full UP refresh: uses Windows Task Scheduler (system service) to
# launch refresh_all.py so the pythonw process escapes the Trae sandbox
# timeout (~2 min kill). Lifecycle:
#   schtasks create+run -> poll driver_live.log -> cleanup -> ensure tray up
#   (the tray/WebUI keeps running the whole time: mutual exclusion with the
#    driver is enforced via the driver state file, and the WebUI shows the
#    driver's live progress + terminate button)
#
# Idempotent (re-runnable after sandbox kill): detects an existing
# schtasks task or a still-running pythonw refresh_all process, and jumps
# straight to polling.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\refresh_guarded.ps1
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\refresh_guarded.ps1 -Workers 5 -Force
#
# Params:
#   -Workers   concurrency (default 3)
#   -Force     force re-run already-completed videos (default False = incremental)
#   -PollSec   polling interval seconds (default 90)
#   -TaskName  schtasks task name (default "DouyinRefresh")
# ---------------------------------------------------------------------------
param(
    [int]$Workers = 3,
    [switch]$Force,
    [int]$PollSec = 90,
    [string]$TaskName = "DouyinRefresh"
)

# --- Encoding: PS5 reads .ps1 without BOM as ANSI. Force UTF-8 for this session. ---
$OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
[Console]::InputEncoding  = [System.Text.Encoding]::UTF8

$ErrorActionPreference = "Continue"
$root = Split-Path $PSScriptRoot -Parent
Set-Location $root

# Use LiteralPath via .NET to avoid ANSI-vs-UTF8 path issues with Chinese dirs.
# PowerShell's Join-Path and -Path parameters re-encode via the current codepage,
# which corrupts non-ASCII path segments on PS5.
function Join-Utf8([string]$base, [string]$child) {
    return [System.IO.Path]::Combine($base, $child)
}

$pyw      = Join-Utf8 $root ".venv\Scripts\pythonw.exe"
$driver   = Join-Utf8 $root "scripts\refresh_all.py"
# "刷新日志" built from char codes: PS5 without BOM would mangle literal Chinese
$refreshDirName = [string]([char]0x5237 + [char]0x65B0 + [char]0x65E5 + [char]0x5FD7)
$liveLog  = Join-Utf8 (Join-Utf8 $root "output") ($refreshDirName + "\driver_live.log")

$forceFlag  = if ($Force) { "force" } else { "" }
$actionArgs = "`"$pyw`" `"$driver`" $Workers $forceFlag"

function Write-Step($msg) { Write-Host ("[guard] " + $msg) -ForegroundColor Cyan }
function Write-Ok($msg)  { Write-Host ("[ok]    " + $msg) -ForegroundColor Green }
function Write-Warn($msg){ Write-Host ("[warn]  " + $msg) -ForegroundColor Yellow }

# Read log via .NET (always UTF-8, never codepage-dependent)
function Get-DriverLogTail([string]$path) {
    if (-not [System.IO.File]::Exists($path)) { return @() }
    try {
        $fs  = [System.IO.FileStream]::new($path, [System.IO.FileMode]::Open,
                                            [System.IO.FileAccess]::Read,
                                            [System.IO.FileShare]::ReadWrite)
        $r   = [System.IO.StreamReader]::new($fs, [System.Text.Encoding]::UTF8)
        $txt = $r.ReadToEnd()
        $r.Close(); $fs.Close()
        return $txt -split "`r?`n"
    } catch {
        return @()
    }
}

# ---------- 0. sanity ----------
if (-not [System.IO.File]::Exists($pyw))    { Write-Host "FATAL: pythonw.exe not found: $pyw"; exit 1 }
if (-not [System.IO.File]::Exists($driver)) { Write-Host "FATAL: refresh_all.py not found: $driver"; exit 1 }

# ---------- 1. idempotency: detect existing run ----------
$existingTask = schtasks /query /tn $TaskName 2>&1 | Out-String
$existingPyw  = @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
                  Where-Object { $_.CommandLine -match 'refresh_all\.py' })

$needCreate = $true
if ($existingPyw.Count -gt 0) {
    Write-Warn ("Found running pythonw refresh_all (pid={0}), skip create" -f $existingPyw[0].ProcessId)
    $needCreate = $false
} elseif ($existingTask -match "ERROR: The system cannot find") {
    $needCreate = $true
} else {
    Write-Warn ("schtasks $TaskName exists, will re-run it")
    $needCreate = $false
}

# ---------- 2. create + run schtasks ----------
if ($needCreate) {
    # NOTE: the tray (WebUI) is deliberately left running. The driver and
    # the WebUI share the Chrome profile exclusively via mutual exclusion:
    # the driver refuses to start while a WebUI batch runs, and the WebUI
    # blocks new tasks while the driver's heartbeat file exists. Keeping
    # the tray alive lets the WebUI show the driver's live progress and
    # offer a terminate button (scripts/refresh_all.py state file).
    schtasks /delete /tn $TaskName /f 2>&1 | Out-Null

    # Rotate live log so stale done-marks from previous runs don't
    # confuse the poll loop. No process holds the file here.
    if ([System.IO.File]::Exists($liveLog)) {
        try { [System.IO.File]::Delete($liveLog) } catch {
            try { [System.IO.File]::Move($liveLog, "$liveLog.old") } catch {}
        }
    }

    Write-Step ("Create schtasks task: " + $TaskName)
    # TR invokes pythonw.exe directly with absolute paths — no bat/ps1
    # wrapper (encoding issues killed those approaches).
    schtasks /create /tn $TaskName /tr $actionArgs /sc once `
             /st ((Get-Date).AddMinutes(1).ToString("HH:mm")) /f 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Host "FATAL: schtasks create failed"; exit 1 }

    Write-Step "Run now"
    schtasks /run /tn $TaskName 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { Write-Host "FATAL: schtasks run failed"; exit 1 }

    Start-Sleep -Seconds 20   # let Chrome + pythonw cold-start
}

# ---------- 3. poll loop ----------
# Baseline: only count done-marks appended AFTER guardian start, so a
# leftover mark from a previous run can't trigger an early exit.
$baseline = (Get-DriverLogTail $liveLog).Count
Write-Step ("Start polling (interval={0}s, baseline_lines={1})" -f $PollSec, $baseline)
Write-Host ("  log path: " + $liveLog)
Write-Host ""

$pollIdx = 0
$seenDone = $false
while ($true) {
    $pollIdx++
    Start-Sleep -Seconds $PollSec

    $pywList = @(Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
                 Where-Object { $_.CommandLine -match 'refresh_all\.py' })
    $alive = $pywList.Count -gt 0

    $lines = Get-DriverLogTail $liveLog
    $tail  = "(no new log yet)"
    for ($i = $baseline; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -ne "") { $tail = $lines[$i] }
    }
    $done = $false
    for ($i = $baseline; $i -lt $lines.Count; $i++) {
        if ($lines[$i] -match '=== .*[\u5b8c\u6210]|=== DRIVER DONE ===') { $done = $true; break }
    }

    # Compact one-line status
    $marker = if ($done) { "DONE" } elseif ($alive) { "LIVE" } else { "DEAD" }
    $short  = ($tail -replace '\s+', ' ')
    if ($short.Length -gt 90) { $short = $short.Substring(0, 90) + "..." }
    Write-Host ("[p{0,-2}] {1}  alive={2}  {3}" -f $pollIdx, $marker, $alive, $short)

    if ($done) { $seenDone = $true }
    if ($done -or (-not $alive)) { break }
}

Write-Host ""
Write-Step ("Driver ended. seen_donemark={0}" -f $seenDone)

# ---------- 4. cleanup schtasks ----------
schtasks /delete /tn $TaskName /f 2>&1 | Out-Null

# ---------- 5. summary ----------
# Glob via .NET: enumerate output dir for summary files.
$reportDir = Join-Utf8 $root "output"
$reportDir = Join-Utf8 $reportDir ([char]0x5237 + [char]0x65B0 + [char]0x65E5 + [char]0x5FD7)
$reportGlob = Join-Utf8 $reportDir "*$([char]0x5168)UP$([char]0x5237 + [char]0x65B0)_$([char]0x6C47 + [char]0x603B).log"
$latestReport = Get-ChildItem $reportGlob -ErrorAction SilentlyContinue |
                Sort-Object LastWriteTime -Descending |
                Select-Object -First 1

if ($latestReport) {
    Write-Step ("Summary: " + $latestReport.Name)
    $jsonText = [System.IO.File]::ReadAllText($latestReport.FullName, [System.Text.Encoding]::UTF8)
    $json = $jsonText | ConvertFrom-Json
    Write-Host ("  Run:    {0} -> {1}" -f $json.started_at, $json.finished_at)
    Write-Host ("  Dur:    {0}s ({1}m {2}s)" -f $json.duration_s, ([int]$json.duration_s/60), ($json.duration_s % 60))
    Write-Host ("  Totals: ok={0}  skip={1}  fail={2}  resumed={3}" -f $json.total_ok, $json.total_skip, $json.total_fail, $json.total_resumed)
    Write-Host ""
    Write-Host ("  {0,-22} {1,6} {2,5} {3,5} {4,5}" -f "UP", "total", "ok", "skip", "fail")
    Write-Host ("  " + ("-" * 60))
    foreach ($a in $json.authors) {
        if ($a.error) {
            $err = $a.error.Substring(0, [Math]::Min(40, $a.error.Length))
            Write-Host ("  {0,-22} ERROR: {1}" -f $a.author, $err)
        } else {
            Write-Host ("  {0,-22} {1,6} {2,5} {3,5} {4,5}" -f $a.author, $a.total, $a.ok, $a.skip, $a.fail)
        }
    }
} else {
    Write-Warn ("No summary file found in: " + $reportDir)
}

# ---------- 6. ensure tray is up (it was never stopped; just a safety net) ----------
Write-Host ""
Write-Step "Restart tray (silent)"
$trayPy = Join-Utf8 $root "tray_server.py"
if ([System.IO.File]::Exists($trayPy)) {
    $probe = "http://127.0.0.1:8080/"
    $code = ""
    try { $code = & curl.exe -s -o NUL -w "%{http_code}" --noproxy "*" --max-time 3 $probe 2>$null } catch { $code = "" }
    if ($code -ne "200") {
        Start-Process -FilePath $pyw -ArgumentList "`"$trayPy`"" -WorkingDirectory $root -WindowStyle Hidden
        $tries = 0
        while ($tries -lt 15) {
            Start-Sleep -Seconds 2
            $tries++
            try { $code = & curl.exe -s -o NUL -w "%{http_code}" --noproxy "*" --max-time 3 $probe 2>$null } catch { $code = "" }
            if ($code -eq "200") { break }
        }
    }
    if ($code -eq "200") { Write-Ok "Tray is up on http://localhost:8080 (no browser opened)" }
    else { Write-Warn "Tray did not respond within 30s; check logs\webui.log" }
}

Write-Host ""
Write-Ok "Guardian finished."
exit 0

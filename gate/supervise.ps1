<#
  supervise.ps1 — keep `gate.py run` alive across the failures that are not
  the queue's fault, and stop cold at every failure that is.

  Why this exists: a real run once died mid-task because the
  runner was a child of an agent's tool shell, and the harness killed that
  shell. The runner's own guards (task_timeout_s, kill_tree) were never the
  problem — the runner simply stopped existing, orphaning a worker on a dead
  stdout pipe and leaving the task IN_PROGRESS. Launch this via Task
  Scheduler (see launch-detached.ps1) and the runner outlives every agent
  session, terminal, and tool call.

  What it will restart:
    - the runner vanishing without writing a report (crash / external kill)
    - stop reason `no_workers` (every CLI quota-benched) after a cooldown

  What it will NEVER do:
    - take over or de-claim an IN_PROGRESS task. gate.py refuses to start on
      one; this script honours that refusal and exits. A stale claim means a
      human has to look at the tree — a heuristic cannot tell a worker that
      is 40 minutes into `pnpm vitest` (writing no files, by design) from a
      worker that died.
    - hunt and kill orphan CLI processes. The user's own interactive sessions
      are indistinguishable from an orphan by name alone.
    - retry a task the gate rejected (`no_progress`, `not_done`). Those are
      the gate refusing to record work it cannot verify. That is the product.
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Repo,
  [int]$MaxTasks = 7,
  [int]$MaxRestarts = 3,
  [int]$QuotaCooldownMinutes = 45,
  # Empty = the first `python` on PATH. Pass an explicit interpreter when the
  # scheduler's lean PATH does not carry one.
  [string]$Python = '',
  # Empty = gate.py next to this script.
  [string]$GatePy = '',
  # Task Scheduler hands over a lean PATH; the worker CLIs usually live in
  # per-user directories. Missing entries are skipped. Override with
  # -PathPrepend or the GATE_PATH_PREPEND environment variable (';'-separated).
  [string[]]$PathPrepend = @()
)

if (-not $GatePy) { $GatePy = Join-Path $PSScriptRoot 'gate.py' }
if (-not $Python) {
  $cmd = Get-Command python -ErrorAction SilentlyContinue
  $Python = if ($cmd) { $cmd.Source } else { 'python' }
}
if (-not $PathPrepend -or $PathPrepend.Count -eq 0) {
  if ($env:GATE_PATH_PREPEND) {
    $PathPrepend = @($env:GATE_PATH_PREPEND -split ';' | Where-Object { $_ })
  } else {
    $PathPrepend = @(
      (Join-Path $env:USERPROFILE '.local\bin'),
      (Join-Path $env:LOCALAPPDATA 'pnpm'),
      (Join-Path $env:USERPROFILE '.grok\bin'),
      (Join-Path $env:USERPROFILE '.kimi-code\bin')
    )
  }
}

$ErrorActionPreference = 'Stop'
$log = Join-Path $Repo '.gate\supervisor.log'
$report = Join-Path $Repo '.gate\RUN-REPORT.md'

function Say([string]$msg) {
  $line = '{0}  {1}' -f (Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'), $msg
  Add-Content -Path $log -Value $line -Encoding utf8
}

foreach ($p in $PathPrepend) { if (Test-Path $p) { $env:PATH = "$p;$env:PATH" } }

# Count DONE rows so "did this attempt accomplish anything" is a fact, not a guess.
function Get-DoneCount {
  $out = & $Python $GatePy doctor --repo $Repo 2>&1 | Out-String
  if ($out -match "'DONE':\s*(\d+)") { return [int]$Matches[1] }
  return -1
}

# gate.py render_report writes exactly: `- stopped because: **<reason>**`
function Get-StopReason {
  if (-not (Test-Path $report)) { return $null }
  $text = Get-Content $report -Raw
  if ($text -match '(?m)^-\s*stopped because:\s*\*\*([^*]+)\*\*') { return $Matches[1].Trim() }
  return 'unparsed'
}

Say "supervisor start: repo=$Repo max_tasks=$MaxTasks max_restarts=$MaxRestarts pid=$PID"

$restarts = 0
while ($true) {
  $doneBefore = Get-DoneCount
  $stamp = if (Test-Path $report) { (Get-Item $report).LastWriteTimeUtc } else { [datetime]::MinValue }

  Say "launching runner (attempt $($restarts + 1)); DONE before = $doneBefore"
  & $Python $GatePy run --repo $Repo --max-tasks $MaxTasks 2>&1 |
    ForEach-Object { Add-Content -Path $log -Value $_ -Encoding utf8 }
  $exit = $LASTEXITCODE

  $doneAfter = Get-DoneCount
  $progressed = ($doneAfter -gt $doneBefore)
  $freshReport = (Test-Path $report) -and ((Get-Item $report).LastWriteTimeUtc -gt $stamp)
  $stop = if ($freshReport) { Get-StopReason } else { $null }

  Say "runner exited code=$exit DONE=$doneBefore->$doneAfter fresh_report=$freshReport stop=$stop"

  if (-not $freshReport) {
    # No report written = the runner did not decide to stop; it was killed or
    # it crashed. This is the exact 2026-08-17 failure. Restart it — unless a
    # claim was left behind, in which case gate.py itself will refuse and we
    # land in the in_progress branch below on the next pass.
    if ($restarts -ge $MaxRestarts) { Say "STOP: runner died $restarts time(s) without a report; needs a human"; break }
    $restarts++
    Say "runner died without a report; restarting in 30s (restart $restarts/$MaxRestarts)"
    Start-Sleep -Seconds 30
    continue
  }

  # `break`/`continue` inside a PowerShell switch do not reliably control the
  # enclosing loop, so decide with an explicit flag instead.
  $retry = $false
  if ($stop -eq 'queue_empty') {
    Say 'DONE: queue empty — every task closed'
  }
  elseif ($stop -eq 'no_workers') {
    if ($restarts -ge $MaxRestarts) {
      Say 'STOP: no workers and restart budget spent'
    }
    else {
      $restarts++
      Say "no workers (quota); sleeping ${QuotaCooldownMinutes}m then retrying (restart $restarts/$MaxRestarts)"
      Start-Sleep -Seconds ($QuotaCooldownMinutes * 60)
      $retry = $true
    }
  }
  elseif ($stop -like 'blocked:*')     { Say "STOP: $stop — head task needs a human decision" }
  elseif ($stop -like 'in_progress:*') { Say "STOP: $stop — a claim is held. Never auto-recovered by design; check git status" }
  elseif ($stop -like 'no_progress:*') { Say "STOP: $stop — two attempts failed identically; read the task log" }
  elseif ($stop -like 'not_done:*')    { Say "STOP: $stop — worker exited without closing the task; read the task log" }
  elseif ($stop -like 'worker_incomplete:*') { Say "STOP: $stop — worker returned while background work was pending; read the task log" }
  elseif ($stop -like 'worker_left_changes:*') { Say "STOP: $stop — worker left partial changes; no concurrent retry was started" }
  elseif ($stop -like 'timeout:*')     { Say "STOP: $stop — worker exceeded task_timeout_s; check for a half-finished tree" }
  elseif ($stop -like 'judge_escalated:*') { Say "STOP: $stop — task is DONE but not to standard after the revision cap; read the Judge section of RUN-REPORT.md" }
  elseif ($stop -like 'revision_broke_verify:*') { Say "STOP: $stop — a judge revision left acceptance failing or the tree dirty; inspect git log/status, nothing was reset" }
  elseif ($stop -like 'review_loop:*') { Say "STOP: $stop — the repair row drew another high finding on its own file; no second repair row, revise the spec with the user" }
  elseif ($stop -like 'needs_approval:*') { Say "STOP: $stop — head task touches an irreversible path; add its approved: line after a human says yes" }
  elseif ($stop -eq 'budget' -or $stop -eq 'run_timeout') {
    Say "runner hit its own limit ($stop); progressed=$progressed"
  }
  else { Say "STOP: $stop" }

  if (-not $retry) { break }
}

Say 'supervisor exit'

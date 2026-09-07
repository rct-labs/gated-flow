<#
  launch-detached.ps1 — start supervise.ps1 under Task Scheduler, so the run
  belongs to the OS and not to whatever agent session started it.

  Start-Process is deliberately NOT used: an agent's tool shell may live in a
  Windows Job Object with kill-on-close, and a Start-Process child can stay
  inside that job — the parent dies, the job dies, the run dies. A scheduled
  task is owned by the scheduler service, which is the point.

  Usage (`flow home` prints the checkout directory):
    pwsh -NoProfile -File "$(flow home)\gate\launch-detached.ps1" `
         -Repo <project> -MaxTasks 7 -TaskName gate-<slug>
#>
[CmdletBinding()]
param(
  [Parameter(Mandatory = $true)][string]$Repo,
  [int]$MaxTasks = 7,
  [string]$TaskName = 'gate-run',
  # Empty = supervise.ps1 next to this script.
  [string]$Supervisor = '',
  [int]$MaxRestarts = 3
)

$ErrorActionPreference = 'Stop'
if (-not $Supervisor) { $Supervisor = Join-Path $PSScriptRoot 'supervise.ps1' }

$pwshExe = (Get-Command pwsh).Source
$argline = @(
  '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
  '-File', "`"$Supervisor`"",
  '-Repo', "`"$Repo`"",
  '-MaxTasks', $MaxTasks,
  '-MaxRestarts', $MaxRestarts
) -join ' '

$action = New-ScheduledTaskAction -Execute $pwshExe -Argument $argline -WorkingDirectory $Repo
# The trigger is a formality — the task is started explicitly below. It is kept
# far in the future so a reboot does not silently re-run the queue.
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddYears(1)
# Interactive logon type: the worker CLIs authenticate out of the user profile
# and misbehave in the service session.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit (New-TimeSpan -Hours 12) -MultipleInstances IgnoreNew -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
  -Principal $principal -Settings $settings -Force | Out-Null

Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3
$info = Get-ScheduledTaskInfo -TaskName $TaskName
$state = (Get-ScheduledTask -TaskName $TaskName).State

'task      : {0}' -f $TaskName
'state     : {0}' -f $state
'last run  : {0} (result {1})' -f $info.LastRunTime, $info.LastTaskResult
'command   : {0} {1}' -f $pwshExe, $argline
'log       : {0}' -f (Join-Path $Repo '.gate\supervisor.log')
''
'Stop it with:  Stop-ScheduledTask -TaskName {0}' -f $TaskName
'Remove it with: Unregister-ScheduledTask -TaskName {0} -Confirm:$false' -f $TaskName

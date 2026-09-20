<#
.SYNOPSIS
Preview local Windows schedules; only -Register changes Task Scheduler.
.EXAMPLE
powershell -NoProfile -File scripts/register_automation_tasks.ps1
.EXAMPLE
powershell -NoProfile -File scripts/register_automation_tasks.ps1 -Register
#>
[CmdletBinding()]
param(
    [switch]$Register,
    [string]$Config,
    [ValidateSet('nightly-data', 'weekly-matrix', 'weekly-smoke', 'monthly-universe', 'monthly-optimize', 'quarterly-robust')]
    [string]$RunTask
)
$ErrorActionPreference = 'Stop'
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$pythonPath = Join-Path $repoRoot '.venv\Scripts\python.exe'
$runnerPath = Join-Path $PSScriptRoot 'run_automation.py'
if (-not $Config) { $Config = Join-Path $repoRoot 'config\automation.json' }
$configPath = (Resolve-Path -LiteralPath $Config).Path
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'Repository Python environment is missing. Create .venv and install the locked dependencies first.'
}
if ($RunTask) {
    if ($Register) { throw '-RunTask cannot be combined with -Register.' }
    Set-Location -LiteralPath $repoRoot
    & $pythonPath $runnerPath $RunTask '--config' $configPath
    exit $LASTEXITCODE
}

function Escape-Xml([string]$Value) {
    return [System.Security.SecurityElement]::Escape($Value)
}
$allMonths = '<January/><February/><March/><April/><May/><June/><July/><August/><September/><October/><November/><December/>'
$schedules = @(
    @{ Task = 'nightly-data'; Time = '01:37:00'; Calendar = '<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>'; Frequency = 'daily, 01:37 local time' },
    @{ Task = 'weekly-matrix'; Time = '02:47:00'; Calendar = '<ScheduleByWeek><WeeksInterval>1</WeeksInterval><DaysOfWeek><Sunday/></DaysOfWeek></ScheduleByWeek>'; Frequency = 'Sunday, 02:47 local time; refresh before matrix' },
    @{ Task = 'weekly-smoke'; Time = '03:17:00'; Calendar = '<ScheduleByWeek><WeeksInterval>1</WeeksInterval><DaysOfWeek><Monday/></DaysOfWeek></ScheduleByWeek>'; Frequency = 'Monday, 03:17 local time; synthetic only' },
    @{ Task = 'monthly-universe'; Time = '02:17:00'; Calendar = "<ScheduleByMonth><DaysOfMonth><Day>1</Day></DaysOfMonth><Months>$allMonths</Months></ScheduleByMonth>"; Frequency = 'first of each month, 02:17 local time; independent facts required' },
    @{ Task = 'monthly-optimize'; Time = '04:37:00'; Calendar = "<ScheduleByMonth><DaysOfMonth><Day>1</Day></DaysOfMonth><Months>$allMonths</Months></ScheduleByMonth>"; Frequency = 'first of each month, 04:37 local time' },
    @{ Task = 'quarterly-robust'; Time = '09:17:00'; Calendar = '<ScheduleByMonth><DaysOfMonth><Day>2</Day></DaysOfMonth><Months><January/><April/><July/><October/></Months></ScheduleByMonth>'; Frequency = 'second of January/April/July/October, 09:17 local time' }
)
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$powershellPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$plans = foreach ($schedule in $schedules) {
    $taskName = 'QuantTradingV1-' + $schedule.Task
    $arguments = '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -File "' + $PSCommandPath + '" -RunTask ' + $schedule.Task + ' -Config "' + $configPath + '"'
    $start = (Get-Date).ToString('yyyy-MM-dd') + 'T' + $schedule.Time
    $xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>QuantTrading research automation; no live admission or orders.</Description></RegistrationInfo>
  <Triggers><CalendarTrigger><StartBoundary>$start</StartBoundary><Enabled>true</Enabled>$($schedule.Calendar)</CalendarTrigger></Triggers>
  <Principals><Principal id="Author"><UserId>$(Escape-Xml $identity)</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings><MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy><DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries><StartWhenAvailable>true</StartWhenAvailable><Enabled>true</Enabled><Hidden>true</Hidden><ExecutionTimeLimit>PT5H</ExecutionTimeLimit></Settings>
  <Actions Context="Author"><Exec><Command>$(Escape-Xml $powershellPath)</Command><Arguments>$(Escape-Xml $arguments)</Arguments><WorkingDirectory>$(Escape-Xml $repoRoot)</WorkingDirectory></Exec></Actions>
</Task>
"@
    if ($Register) {
        # No -Force: an existing user's task is never silently replaced.
        Register-ScheduledTask -TaskName $taskName -Xml $xml | Out-Null
    }
    [PSCustomObject]@{
        TaskName = $taskName
        Frequency = $schedule.Frequency
        WorkingDirectory = $repoRoot
        Command = $powershellPath
        Arguments = $arguments
        Registered = [bool]$Register
        LoginRequirement = 'Current Windows user must be signed in; no password is stored.'
    }
}
$plans | ConvertTo-Json -Depth 4

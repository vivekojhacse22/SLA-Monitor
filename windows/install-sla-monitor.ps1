[CmdletBinding()]
param(
    [string]$AppRoot = 'C:\SlaMonitor'
)

$ErrorActionPreference = 'Stop'
$Python = Join-Path $AppRoot '.venv\Scripts\python.exe'
$Requirements = Join-Path $AppRoot 'requirements.txt'
$EnvironmentFile = Join-Path $AppRoot '.env'

if (-not (Test-Path $EnvironmentFile)) {
    throw "Create $EnvironmentFile from .env.production.example before installing the service."
}

$EnvironmentContent = Get-Content $EnvironmentFile -Raw
if ($EnvironmentContent -match '<[^>]+>') {
    throw 'Replace every angle-bracket placeholder in .env before installing the service.'
}
if ($EnvironmentContent -notmatch '(?m)^AUTH_MODE=app\s*$') {
    throw 'The production .env must contain AUTH_MODE=app.'
}

& icacls.exe $EnvironmentFile /inheritance:r /grant:r 'SYSTEM:(F)' 'BUILTIN\Administrators:(F)' | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to restrict access to the production .env file.'
}

if (-not (Get-Command py.exe -ErrorAction SilentlyContinue) -and
    -not (Get-Command python.exe -ErrorAction SilentlyContinue)) {
    throw 'Python 3.11 or newer must be installed before running this script.'
}

if (-not (Test-Path $Python)) {
    if (Get-Command py.exe -ErrorAction SilentlyContinue) {
        & py.exe -3.11 -m venv (Join-Path $AppRoot '.venv')
    }
    else {
        & python.exe -m venv (Join-Path $AppRoot '.venv')
    }
}

& $Python -m pip install --upgrade pip
& $Python -m pip install --requirement $Requirements

$StartScript = Join-Path $AppRoot 'windows\start-sla-monitor.ps1'
$BackupScript = Join-Path $AppRoot 'windows\backup-sla-monitor.ps1'
$PowerShell = "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe"

$StartAction = New-ScheduledTaskAction -Execute $PowerShell -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$StartScript`""
$StartTrigger = New-ScheduledTaskTrigger -AtStartup
$StartSettings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable
$SystemPrincipal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName 'SLA Monitor' -Action $StartAction -Trigger $StartTrigger -Settings $StartSettings -Principal $SystemPrincipal -Force | Out-Null

$BackupAction = New-ScheduledTaskAction -Execute $PowerShell -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$BackupScript`""
$BackupTrigger = New-ScheduledTaskTrigger -Daily -At '02:00'
Register-ScheduledTask -TaskName 'SLA Monitor Backup' -Action $BackupAction -Trigger $BackupTrigger -Principal $SystemPrincipal -Force | Out-Null

Start-ScheduledTask -TaskName 'SLA Monitor'
Write-Host 'SLA Monitor installed and started.'
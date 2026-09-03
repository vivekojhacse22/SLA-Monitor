$ErrorActionPreference = 'Stop'

$AppRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $AppRoot '.venv\Scripts\python.exe'
$LogDirectory = Join-Path $AppRoot 'logs'
$LogFile = Join-Path $LogDirectory 'sla-monitor.log'

New-Item -ItemType Directory -Path $LogDirectory -Force | Out-Null
Set-Location $AppRoot

while ($true) {
    try {
        & $Python (Join-Path $AppRoot 'serve.py') *>> $LogFile
    }
    catch {
        "$(Get-Date -Format o) Process failed: $_" | Out-File $LogFile -Append
    }
    "$(Get-Date -Format o) Restarting in 10 seconds." | Out-File $LogFile -Append
    Start-Sleep -Seconds 10
}
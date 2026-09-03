$ErrorActionPreference = 'Stop'

$AppRoot = Split-Path -Parent $PSScriptRoot
$Database = Join-Path $AppRoot 'sla_cache.db'
$BackupDirectory = Join-Path $AppRoot 'backups'

if (-not (Test-Path $Database)) {
    exit 0
}

New-Item -ItemType Directory -Path $BackupDirectory -Force | Out-Null
$Timestamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$Destination = Join-Path $BackupDirectory "sla_cache-$Timestamp.db"

$Python = Join-Path $AppRoot '.venv\Scripts\python.exe'
& $Python -c "import sqlite3; source=sqlite3.connect(r'$Database'); target=sqlite3.connect(r'$Destination'); source.backup(target); target.close(); source.close()"

Get-ChildItem $BackupDirectory -Filter 'sla_cache-*.db' |
    Where-Object LastWriteTime -lt (Get-Date).AddDays(-14) |
    Remove-Item -Force
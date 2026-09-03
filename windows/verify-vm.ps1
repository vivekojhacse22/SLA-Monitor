$ErrorActionPreference = 'Continue'

$EnvironmentFile = 'C:\SlaMonitor\.env'
if (-not (Test-Path $EnvironmentFile)) {
    Write-Output 'ENV_MISSING'
}
else {
    $content = Get-Content $EnvironmentFile -Raw
    if ($content -match '(?m)^PORT=8081\s*$') { Write-Output 'PORT_8081_OK' } else { Write-Output 'PORT_8081_NOT_SET' }
    if ($content -match '(?m)^AUTH_MODE=app\s*$') { Write-Output 'AUTH_MODE_APP_OK' } else { Write-Output 'AUTH_MODE_APP_NOT_SET' }
}

$task = Get-ScheduledTask -TaskName 'SLA Monitor' -ErrorAction SilentlyContinue
if ($task) {
    $info = Get-ScheduledTaskInfo -TaskName 'SLA Monitor'
    Write-Output "TASK_STATE=$($task.State)"
    Write-Output "TASK_LAST_RESULT=$($info.LastTaskResult)"
}
else {
    Write-Output 'TASK_MISSING'
}

$listener = Get-NetTCPConnection -State Listen -LocalPort 8081 -ErrorAction SilentlyContinue
if ($listener) { Write-Output 'PORT_8081_LISTENING' } else { Write-Output 'PORT_8081_NOT_LISTENING' }

try {
    $response = Invoke-WebRequest -Uri 'http://127.0.0.1:8081/health' -UseBasicParsing -TimeoutSec 20
    Write-Output "HEALTH_HTTP=$($response.StatusCode)"
    $health = $response.Content | ConvertFrom-Json
    Write-Output "HEALTH_STATUS=$($health.status)"
    Write-Output "HEALTH_STARTED=$($health.started)"
}
catch {
    Write-Output "HEALTH_ERROR=$($_.Exception.Message)"
}

$ErrorActionPreference = 'Stop'

$AppRoot = 'C:\SlaMonitor'
$EnvironmentFile = Join-Path $AppRoot '.env'
$Python = Join-Path $AppRoot '.venv\Scripts\python.exe'

if (-not (Test-Path $EnvironmentFile)) {
    throw 'C:\SlaMonitor\.env is missing.'
}

$content = Get-Content $EnvironmentFile -Raw
if ($content -match '(?m)^PORT=') {
    $content = [regex]::Replace($content, '(?m)^PORT=.*$', 'PORT=8081')
}
else {
    $content += "`r`nPORT=8081`r`n"
}
if ($content -match '(?m)^HOST=') {
    $content = [regex]::Replace($content, '(?m)^HOST=.*$', 'HOST=127.0.0.1')
}
else {
    $content += "HOST=127.0.0.1`r`n"
}
Set-Content -Path $EnvironmentFile -Value $content -Encoding UTF8

& (Join-Path $AppRoot 'windows\install-sla-monitor.ps1') -AppRoot $AppRoot

Set-Location $AppRoot
$testCode = @'
import config
import requests

if not config.TEAMS_WEBHOOK_URL:
    raise RuntimeError("TEAMS_WEBHOOK_URL is not configured")
payload = {
    "type": "message",
    "attachments": [{
        "contentType": "application/vnd.microsoft.card.adaptive",
        "contentUrl": None,
        "content": {
            "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
            "type": "AdaptiveCard",
            "version": "1.4",
            "body": [{
                "type": "TextBlock",
                "text": "Test message from SLA Monitor VM.",
                "wrap": True
            }]
        }
    }]
}
response = requests.post(config.TEAMS_WEBHOOK_URL, json=payload, timeout=30)
response.raise_for_status()
print(f"TEAMS_TEST_STATUS={response.status_code}")
'@
& $Python -c $testCode
Write-Output 'VM_CONFIGURATION_AND_TEST_COMPLETE'

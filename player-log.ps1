# Poll Palworld REST /v1/api/players every 5 minutes; append online names when any.
# Auth: admin + AdminPassword from PalWorldSettings.ini (same as server-manager.py)

$Api      = 'http://localhost:8212/v1/api/players'
$Log      = Join-Path $PSScriptRoot 'logs\player_presence.txt'
$Settings = Join-Path $PSScriptRoot 'server\Pal\Saved\Config\WindowsServer\PalWorldSettings.ini'

$m = Select-String -Path $Settings -Pattern 'AdminPassword="([^"]*)"' | Select-Object -First 1
if (-not $m) { throw "AdminPassword not found in $Settings" }
$auth = [Convert]::ToBase64String([Text.Encoding]::ASCII.GetBytes("admin:$($m.Matches.Groups[1].Value)"))
New-Item -ItemType Directory -Force -Path (Split-Path $Log) | Out-Null

while ($true) {
    try {
        $data  = Invoke-RestMethod $Api -Headers @{ Authorization = "Basic $auth"; Accept = 'application/json' }
        $names = @($data.players | ForEach-Object { $_.name } | Where-Object { $_ })
        if ($names.Count) {
            "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $($names -join ', ')" |
                Add-Content -Path $Log -Encoding utf8
        }
    } catch {}
    Start-Sleep -Seconds 300
}

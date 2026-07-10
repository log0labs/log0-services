# start-log0.ps1 - ensure the log0 backend (services + Cloudflare tunnel) is up.
# Idempotent: safe to run anytime. `docker compose up -d` is a no-op for already-
# running containers and (re)starts any that are stopped. Meant to run at logon.

$ErrorActionPreference = 'Continue'
$dir = 'C:\Users\ashmi\Documents\log0\log0-services\docker'
$log = Join-Path $dir 'start-log0.log'

function Log($m) { "$([DateTime]::Now.ToString('s'))  $m" | Tee-Object -FilePath $log -Append }

Log '--- wake: ensuring log0 stack is up ---'

# 1) Wait for the Docker engine to be ready (Docker Desktop may still be booting).
$ready = $false
for ($i = 0; $i -lt 60; $i++) {          # up to ~5 min
  docker info *> $null
  if ($LASTEXITCODE -eq 0) { $ready = $true; break }
  Start-Sleep -Seconds 5
}
if (-not $ready) { Log 'docker engine not ready after 5 min - aborting'; exit 1 }
Log 'docker engine ready'

# 2) Bring the stack up WITH the tunnel overlay (idempotent).
Set-Location $dir
docker compose -f docker-compose.yml -f docker-compose.tunnel.yml up -d *>> $log
Log "compose up -d finished (exit $LASTEXITCODE)"

# 3) Optional: log public reachability for the record (does not gate anything).
try {
  $code = (Invoke-WebRequest -Uri 'https://api.log0.in/actuator/health' -TimeoutSec 20 -UseBasicParsing).StatusCode
  Log "api.log0.in health -> $code"
} catch {
  Log "api.log0.in not reachable yet (services may still be starting): $($_.Exception.Message)"
}

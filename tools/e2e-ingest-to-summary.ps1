# Option A E2E: ingest 12 matching ERROR logs -> incident -> AI summary (+ optional Slack).
#
#   $env:API_KEY = "log0_c75281f7e0c7666c5d89581b61e726ac5673f1292eb6fa4112ca6230776bbd89"
#   .\e2e-ingest-to-summary.ps1
#   # or: .\e2e-ingest-to-summary.ps1 -ApiKey "log0_c75281f7e0c7666c5d89581b61e726ac5673f1292eb6fa4112ca6230776bbd89"
#
# Throwaway tenant (not visible in your console login):
#   .\e2e-ingest-to-summary.ps1 -MintApiKey

param(
    [switch]$MintApiKey,
    [string]$ApiKey,
    [string]$IngestUrl = "http://localhost:8080",
    [string]$AuthUrl = "http://localhost:8086",
    [string]$IncidentUrl = "http://localhost:8083",
    [string]$AiUrl = "http://localhost:8085"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent

$AuthEnv = Join-Path $Root "services\auth-service\.env"
$AiEnv = Join-Path $Root "services\ai-service\.env"
$NotifEnv = Join-Path $Root "services\notification-service\.env"

function Read-EnvVar([string]$File, [string]$Key) {
    if (-not (Test-Path $File)) { return $null }
    foreach ($line in Get-Content $File) {
        if ($line -match "^\s*$Key=(.*)$") {
            $v = $Matches[1].Trim()
            if ($v.StartsWith('"') -and $v.EndsWith('"')) { $v = $v.Substring(1, $v.Length - 2) }
            return $v
        }
    }
    return $null
}

function Write-Ok([string]$Msg) { Write-Host $Msg -ForegroundColor Green }
function Write-Warn([string]$Msg) { Write-Host $Msg -ForegroundColor Yellow }
function Write-Err([string]$Msg) { Write-Host $Msg -ForegroundColor Red }

function Test-EnvFiles {
    Write-Host "== .env preflight =="
    $ok = $true

    if (-not (Test-Path $AuthEnv)) {
        Write-Err "MISSING $AuthEnv - copy .env.example, set JWT_SECRET (min 32 chars)"
        $ok = $false
    } else {
        $jwt = Read-EnvVar $AuthEnv "JWT_SECRET"
        if ([string]::IsNullOrWhiteSpace($jwt) -or $jwt -match "your-secret") {
            Write-Err "FIX $AuthEnv - JWT_SECRET must be set"
            $ok = $false
        } else { Write-Ok "OK auth-service/.env (JWT_SECRET set)" }
    }

    if (-not (Test-Path $AiEnv)) {
        Write-Err "MISSING $AiEnv - copy .env.example"
        $ok = $false
    } else {
        $groq = Read-EnvVar $AiEnv "GROQ_API_KEY"
        if ([string]::IsNullOrWhiteSpace($groq)) {
            Write-Warn "FIX $AiEnv - GROQ_API_KEY required for default LLM_* (groq)"
            $ok = $false
        } else { Write-Ok "OK ai-service/.env (GROQ_API_KEY set)" }
    }

    if (-not (Test-Path $NotifEnv)) {
        Write-Warn "SKIP notification-service/.env - Slack optional"
    } else {
        $tok = Read-EnvVar $NotifEnv "SLACK_BOT_TOKEN"
        $ch = Read-EnvVar $NotifEnv "SLACK_CHANNEL_ID"
        if ([string]::IsNullOrWhiteSpace($tok) -or $tok -match "your-bot" -or
            [string]::IsNullOrWhiteSpace($ch) -or $ch -match "C0XXXX") {
            Write-Warn "WARN notification-service/.env - Slack placeholders; rest of E2E still works"
        } else { Write-Ok "OK notification-service/.env (Slack configured)" }
    }

    if (-not $ok) {
        Write-Err "Fix .env issues, restart containers (docker compose up -d), re-run."
        exit 1
    }
    Write-Host ""
}

function Test-ServiceHealth {
    Write-Host "== Health =="
    foreach ($pair in @(
            @("ingestion-gateway", "$IngestUrl/actuator/health"),
            @("auth-service", "$AuthUrl/actuator/health"),
            @("incident-service", "$IncidentUrl/actuator/health"),
            @("ai-service", "$AiUrl/actuator/health")
        )) {
        $up = $false
        for ($i = 1; $i -le 15; $i++) {
            try {
                Invoke-RestMethod -Uri $pair[1] -TimeoutSec 5 | Out-Null
                Write-Ok "UP  $($pair[0]) ($($pair[1]))"
                $up = $true
                break
            } catch {
                if ($i -lt 15) { Start-Sleep -Seconds 2 } else {
                    Write-Err "DOWN $($pair[0]) ($($pair[1]))"
                    exit 1
                }
            }
        }
        if (-not $up) { exit 1 }
    }
    Write-Host ""
}

function New-MintApiKey {
    Write-Warn "Minting throwaway tenant + API key (console login won't see these incidents)"
    $ts = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds()
    $email = "e2e+$ts@log0.test"
    $slug = "e2e-$ts"
    $pass = "E2ePass123!"

    Invoke-RestMethod -Method POST -Uri "$AuthUrl/api/v1/tenants/register" -ContentType "application/json" `
        -Body (@{ tenantName = "E2E $ts"; slug = $slug; adminEmail = $email; adminPassword = $pass } | ConvertTo-Json) | Out-Null

    $login = Invoke-RestMethod -Method POST -Uri "$AuthUrl/api/v1/auth/login" -ContentType "application/json" `
        -Body (@{ email = $email; password = $pass } | ConvertTo-Json)

    $keyResp = Invoke-RestMethod -Method POST -Uri "$AuthUrl/api/v1/api-keys" -ContentType "application/json" `
        -Headers @{ Authorization = "Bearer $($login.accessToken)" } `
        -Body (@{ name = "e2e" } | ConvertTo-Json)

    return $keyResp.rawKey
}

function Get-ApiKey {
    if ($MintApiKey) { return New-MintApiKey }
    if (-not [string]::IsNullOrWhiteSpace($ApiKey)) { return $ApiKey.Trim() }
    $k = $env:API_KEY
    if (-not [string]::IsNullOrWhiteSpace($k)) { return $k.Trim() }
    Write-Err 'Pass -ApiKey "log0_..." or:  $env:API_KEY = "log0_..."; .\e2e-ingest-to-summary.ps1'
    Write-Err 'In PowerShell, API_KEY=... ./script.sh does not work — use Git Bash for that syntax.'
    exit 1
}

function Get-TenantId([string]$Key) {
    try {
        return (Invoke-RestMethod -Method POST -Uri "$AuthUrl/api/v1/auth/validate-key" `
            -Headers @{ "X-Api-Key" = $Key }).tenantId
    } catch {
        Write-Err "Invalid or inactive API key against $AuthUrl"
        Write-Warn "Local E2E uses Docker auth on :8086. Your console key only works if the console talks to that same Postgres (AUTH_API_URL=http://localhost:8086)."
        Write-Warn "If you use the hosted console/tunnel, mint a local key: log in to console on localhost, create Settings -> API Keys, or use -MintApiKey."
        exit 1
    }
}

$RunId = "e2e-$([DateTimeOffset]::UtcNow.ToUnixTimeSeconds())"
$Message = "E2E Option A timeout after 30000ms $RunId"
$Trace = "java.net.SocketTimeoutException: $Message`n`t at com.log0.e2e.TestHandler.run(TestHandler.java:42)"

function Send-IngestBurst([string]$ApiKey) {
    Write-Host "== Ingest 12 logs (threshold 10) =="
    Write-Host "  marker: $RunId"
    $headers = @{
        "Content-Type"   = "application/json"
        "X-SERVICE-NAME" = "payment-service"
        "X-ENVIRONMENT"  = "production"
        "X-API-KEY"      = $ApiKey
    }
    1..12 | ForEach-Object {
        $body = @{
            timestamp = (Get-Date).ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.fffZ")
            level     = "ERROR"
            message   = $Message
            trace     = $Trace
        } | ConvertTo-Json -Compress
        try {
            Invoke-WebRequest -Uri "$IngestUrl/api/v1/logs" -Method POST -Headers $headers -Body $body -UseBasicParsing | Out-Null
        } catch {
            Write-Err "Ingest failed: $($_.Exception.Message) - check API_KEY and auth :8086"
            exit 1
        }
        Start-Sleep -Milliseconds 200
    }
    Write-Ok "12 x 202 Accepted"
    Write-Host ""
}

function Wait-Incident([string]$TenantId) {
    Write-Host "== Wait for incident (tenant $TenantId) =="
    for ($attempt = 1; $attempt -le 45; $attempt++) {
        $page = Invoke-RestMethod -Uri "$IncidentUrl/api/v1/incidents?tenantId=$TenantId&size=50&sort=createdAt,desc"
        foreach ($inc in $page.content) {
            $msgs = @($inc.topMessages)
            if ($msgs | Where-Object { $_ -like "*$RunId*" }) {
                $id = $inc.incidentId
                Write-Ok "Incident: $id (~$($attempt * 2)s max)"
                return $id
            }
        }
        Start-Sleep -Seconds 2
    }
    Write-Err "No incident within 90s - docker logs log0-clustering log0-incident"
    exit 1
}

function Wait-AiSummary([string]$TenantId, [string]$IncidentId) {
    Write-Host "== Wait for AI summary =="
    for ($attempt = 1; $attempt -le 45; $attempt++) {
        $detail = Invoke-RestMethod -Uri "$IncidentUrl/api/v1/incidents/$IncidentId`?tenantId=$TenantId"
        $summary = $detail.aiSummary
        if (-not [string]::IsNullOrWhiteSpace($summary)) {
            Write-Ok "AI summary present ($($summary.Length) chars)"
            if ($summary.Length -gt 400) { $summary = $summary.Substring(0, 400) + "..." }
            Write-Host $summary
            Write-Host ""
            Write-Ok "E2E OK - check console Incidents or Slack if configured."
            return
        }
        Start-Sleep -Seconds 2
    }
    Write-Warn "Incident exists but ai_summary empty after 90s."
    Write-Warn "Check: docker logs log0-ai --tail 30"
    Write-Warn "Check: docker logs log0-incident 2>&1 | Select-String summary"
    Write-Warn "If you see 422 on POST /summaries, rebuild incident-service (empty JSON body bug was fixed in AiSummaryRequest record)."
    exit 1
}

Test-EnvFiles
Test-ServiceHealth
$apiKey = Get-ApiKey
$tenantId = Get-TenantId $apiKey
Send-IngestBurst $apiKey
$incidentId = Wait-Incident $tenantId
Wait-AiSummary $tenantId $incidentId

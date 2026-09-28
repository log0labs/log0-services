#!/usr/bin/env bash
# Option A E2E: ingest 12 matching ERROR logs → incident → AI summary (+ optional Slack).
#
# Prerequisites: docker compose stack up (ingestion through ai-service).
#
#   API_KEY=ak_xxxx  ./e2e-ingest-to-summary.sh
#   ./e2e-ingest-to-summary.sh --mint-api-key   # throwaway tenant (console won't see it)
#
# Override URLs if needed:
#   INGEST_URL=http://localhost:8080 AUTH_URL=http://localhost:8086 ...

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AUTH_ENV="$ROOT/services/auth-service/.env"
AI_ENV="$ROOT/services/ai-service/.env"
NOTIF_ENV="$ROOT/services/notification-service/.env"

INGEST_URL="${INGEST_URL:-http://localhost:8080}"
AUTH_URL="${AUTH_URL:-http://localhost:8086}"
INCIDENT_URL="${INCIDENT_URL:-http://localhost:8083}"
AI_URL="${AI_URL:-http://localhost:8085}"

MINT_KEY=0
for arg in "$@"; do
  case "$arg" in
    --mint-api-key) MINT_KEY=1 ;;
    -h|--help)
      sed -n '1,12p' "$0"
      exit 0
      ;;
    *) echo "Unknown arg: $arg" >&2; exit 1 ;;
  esac
done

red() { printf '\033[31m%s\033[0m\n' "$*" >&2; }
grn() { printf '\033[32m%s\033[0m\n' "$*" >&2; }
ylw() { printf '\033[33m%s\033[0m\n' "$*" >&2; }

env_val() {
  local file="$1" key="$2"
  [[ -f "$file" ]] || return 1
  grep -E "^[[:space:]]*${key}=" "$file" | head -1 | sed -E "s/^[[:space:]]*${key}=//" | tr -d '\r' | sed -E 's/^["'\''](.*)["'\'' ]/\1/'
}

check_env_files() {
  local ok=1
  echo "== .env preflight =="

  if [[ ! -f "$AUTH_ENV" ]]; then
    red "MISSING $AUTH_ENV — copy from services/auth-service/.env.example and set JWT_SECRET (32+ chars)"
    ok=0
  else
    local jwt
    jwt="$(env_val "$AUTH_ENV" JWT_SECRET || true)"
    if [[ -z "${jwt:-}" ]] || [[ "$jwt" == *your-secret* ]]; then
      red "FIX $AUTH_ENV — JWT_SECRET must be set (not placeholder)"
      ok=0
    else
      grn "OK auth-service/.env (JWT_SECRET set)"
    fi
  fi

  if [[ ! -f "$AI_ENV" ]]; then
    red "MISSING $AI_ENV — copy from services/ai-service/.env.example"
    ok=0
  else
    local groq
    groq="$(env_val "$AI_ENV" GROQ_API_KEY || true)"
    if [[ -z "${groq:-}" ]]; then
      ylw "WARN $AI_ENV — GROQ_API_KEY empty (needed if LLM_* use groq, the default)"
      ok=0
    else
      grn "OK ai-service/.env (GROQ_API_KEY set)"
    fi
    local inc_url
    inc_url="$(env_val "$AI_ENV" INCIDENT_SERVICE_URL || true)"
    if [[ -n "${inc_url:-}" ]] && [[ "$inc_url" != *incident-service* ]] && [[ "$inc_url" != *localhost:8083* ]]; then
      ylw "WARN ai-service INCIDENT_SERVICE_URL=$inc_url — in Docker use http://incident-service:8083 (compose overrides)"
    fi
  fi

  if [[ ! -f "$NOTIF_ENV" ]]; then
    ylw "SKIP notification-service/.env — Slack optional; pipeline works without it"
  else
    local tok ch
    tok="$(env_val "$NOTIF_ENV" SLACK_BOT_TOKEN || true)"
    ch="$(env_val "$NOTIF_ENV" SLACK_CHANNEL_ID || true)"
    if [[ -z "${tok:-}" ]] || [[ "$tok" == *your-bot* ]] || [[ -z "${ch:-}" ]] || [[ "$ch" == C0XXXX* ]]; then
      ylw "WARN notification-service/.env — Slack placeholders; incidents + AI still work"
    else
      grn "OK notification-service/.env (Slack configured)"
    fi
  fi

  if [[ "$ok" -ne 1 ]]; then
    red "Fix required .env issues above, restart affected containers, then re-run."
    exit 1
  fi
  echo ""
}

wait_health() {
  local name="$1" url="$2" attempt
  for attempt in $(seq 1 15); do
    if curl -sf "$url" >/dev/null; then
      grn "UP  $name ($url)"
      return 0
    fi
    sleep 2
  done
  red "DOWN $name ($url)"
  exit 1
}

check_health() {
  echo "== Health =="
  wait_health "ingestion-gateway" "$INGEST_URL/actuator/health"
  wait_health "auth-service" "$AUTH_URL/actuator/health"
  wait_health "incident-service" "$INCIDENT_URL/actuator/health"
  wait_health "ai-service" "$AI_URL/actuator/health"
  echo ""
}

jval() { node -e "let d='';process.stdin.on('data',c=>d+=c).on('end',()=>{try{const j=JSON.parse(d);process.stdout.write(String($1??''))}catch(e){process.exit(1)}})"; }

mint_api_key() {
  local ts email slug pass jwt key
  ts="$(date +%s)-$$-${RANDOM}"
  email="e2e+${ts}@log0.test"
  slug="e2e-${ts}"
  pass="E2ePass123!"

  curl -sf -X POST "$AUTH_URL/api/v1/tenants/register" -H 'Content-Type: application/json' \
    -d "{\"tenantName\":\"E2E ${ts}\",\"slug\":\"${slug}\",\"adminEmail\":\"${email}\",\"adminPassword\":\"${pass}\"}" >/dev/null

  jwt="$(curl -sf -X POST "$AUTH_URL/api/v1/auth/login" -H 'Content-Type: application/json' \
    -d "{\"email\":\"${email}\",\"password\":\"${pass}\"}" | jval '.accessToken')"
  [[ -n "$jwt" ]] || { red "mint: login failed"; exit 1; }

  key="$(curl -sf -X POST "$AUTH_URL/api/v1/api-keys" -H 'Content-Type: application/json' \
    -H "Authorization: Bearer $jwt" -d '{"name":"e2e"}' | jval '.rawKey')"
  [[ -n "$key" ]] || { red "mint: create api key failed"; exit 1; }
  printf '%s' "$key"
}

resolve_api_key() {
  if [[ "$MINT_KEY" -eq 1 ]]; then
    ylw "Minting throwaway tenant + API key (use console tenant + API_KEY=... to see UI)"
    mint_api_key
  elif [[ -n "${API_KEY:-}" ]]; then
    printf '%s' "$API_KEY"
  else
    red "Set API_KEY=ak_... (Console → Settings → API keys) or pass --mint-api-key"
    exit 1
  fi
}

RUN_ID="e2e-$(date +%s)"
MESSAGE="E2E Option A timeout after 30000ms ${RUN_ID}"
TRACE=$'java.net.SocketTimeoutException: '"${MESSAGE}"$'\n\tat com.log0.e2e.TestHandler.run(TestHandler.java:42)'

ingest_logs() {
  local api_key="$1"
  local i ts body
  echo "== Ingest 12 logs (threshold 10) =="
  echo "  marker: ${RUN_ID}"
  for i in $(seq 1 12); do
    ts="$(date -u +"%Y-%m-%dT%H:%M:%S.000Z")"
    body="$(node -e "
      console.log(JSON.stringify({
        timestamp: process.argv[1],
        level: 'ERROR',
        message: process.argv[2],
        trace: process.argv[3]
      }))
    " "$ts" "$MESSAGE" "$TRACE")"
    code="$(curl -s -o /dev/null -w '%{http_code}' -X POST "$INGEST_URL/api/v1/logs" \
      -H 'Content-Type: application/json' \
      -H 'X-SERVICE-NAME: payment-service' \
      -H 'X-ENVIRONMENT: production' \
      -H "X-API-KEY: $api_key" \
      -d "$body")"
    if [[ "$code" != "202" ]]; then
      red "Ingest failed on attempt $i: HTTP $code (check API_KEY and auth-service)"
      exit 1
    fi
    sleep 0.2
  done
  grn "12 x 202 Accepted"
  echo ""
}

wait_for_incident() {
  local api_key="$1"
  local tenant_id incident_id
  tenant_id="$(curl -sf -X POST "$AUTH_URL/api/v1/auth/validate-key" -H "X-Api-Key: $api_key" | jval '.tenantId')"
  [[ -n "$tenant_id" ]] || { red "validate-key failed"; exit 1; }
  echo "== Wait for incident (tenant $tenant_id) ==" >&2

  local attempt page content
  incident_id=""
  for attempt in $(seq 1 45); do
    page="$(curl -sf "$INCIDENT_URL/api/v1/incidents?tenantId=$tenant_id&size=50&sort=createdAt,desc")"
    incident_id="$(node -e "
      const runId = process.argv[1];
      const page = JSON.parse(process.argv[2]);
      const items = page.content || [];
      for (const inc of items) {
        const msgs = inc.topMessages || [];
        if (msgs.some(m => String(m).includes(runId))) {
          process.stdout.write(inc.incidentId || inc.id || '');
          break;
        }
      }
    " "$RUN_ID" "$page")"
    if [[ -n "$incident_id" ]]; then
      grn "Incident: $incident_id (after ~${attempt}s)"
      echo "$incident_id"
      return 0
    fi
    sleep 2
  done
  red "No incident with marker ${RUN_ID} within 90s — check: docker logs log0-clustering log0-incident"
  exit 1
}

wait_for_summary() {
  local tenant_id="$1" incident_id="$2"
  echo "== Wait for AI summary =="
  local attempt detail summary
  for attempt in $(seq 1 45); do
    detail="$(curl -sf "$INCIDENT_URL/api/v1/incidents/$incident_id?tenantId=$tenant_id")"
    summary="$(node -e "
      try {
        const d = JSON.parse(process.argv[1]);
        process.stdout.write(d.aiSummary || '');
      } catch (e) { process.exit(1); }
    " "$detail")"
    if [[ -n "$summary" ]]; then
      grn "AI summary present (${#summary} chars)"
      echo "$summary" | head -c 400
      echo ""
      echo ""
      grn "E2E OK — open console Incidents or check Slack if configured."
      return 0
    fi
    sleep 2
  done
  ylw "Incident exists but ai_summary empty after 90s."
  ylw "Check: docker logs log0-ai --tail 30"
  ylw "Check: docker logs log0-incident 2>&1 | grep -i summary"
  ylw "422 on POST /summaries → rebuild incident-service (AiSummaryRequest JSON fix)."
  exit 1
}

main() {
  check_env_files
  check_health
  local api_key tenant_id incident_id
  api_key="$(resolve_api_key)"
  ingest_logs "$api_key"
  tenant_id="$(curl -sf -X POST "$AUTH_URL/api/v1/auth/validate-key" -H "X-Api-Key: $api_key" | jval '.tenantId')"
  incident_id="$(wait_for_incident "$api_key")"
  wait_for_summary "$tenant_id" "$incident_id"
}

main

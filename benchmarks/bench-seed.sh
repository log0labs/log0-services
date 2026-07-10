#!/usr/bin/env bash
# Registers a throwaway tenant + mints a real API key, prints the raw key to stdout.
# Used by the shell benchmark scripts now that the ingestion gateway validates
# X-API-KEY and derives the tenant from it (a fake key would 401). These scripts
# run on the host, so auth is reached via the published port on localhost.
# Override with AUTH_URL if auth-service is elsewhere.
#
#   API_KEY="$(bash bench-seed.sh)"   # then send -H "X-API-KEY: $API_KEY"
set -euo pipefail

AUTH="${AUTH_URL:-http://localhost:8086}"
TS="$(date +%s)-$$-${RANDOM}"
EMAIL="bench+${TS}@log0.test"
SLUG="bench-${TS}"
PASS="BenchPass123!"

# extract a JSON field via node (available in this repo's tooling)
jval() { node -e "let d='';process.stdin.on('data',c=>d+=c).on('end',()=>{try{process.stdout.write(String(JSON.parse(d)$1??''))}catch(e){}})"; }

curl -s -X POST "$AUTH/api/v1/tenants/register" -H 'Content-Type: application/json' \
  -d "{\"tenantName\":\"Bench ${TS}\",\"slug\":\"${SLUG}\",\"adminEmail\":\"${EMAIL}\",\"adminPassword\":\"${PASS}\"}" >/dev/null

JWT="$(curl -s -X POST "$AUTH/api/v1/auth/login" -H 'Content-Type: application/json' \
  -d "{\"email\":\"${EMAIL}\",\"password\":\"${PASS}\"}" | jval '.accessToken')"
[ -n "$JWT" ] || { echo "bench-seed: login failed against $AUTH" >&2; exit 1; }

KEY="$(curl -s -X POST "$AUTH/api/v1/api-keys" -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $JWT" -d '{"name":"bench"}' | jval '.rawKey')"
[ -n "$KEY" ] || { echo "bench-seed: create-key failed against $AUTH" >&2; exit 1; }

printf '%s' "$KEY"

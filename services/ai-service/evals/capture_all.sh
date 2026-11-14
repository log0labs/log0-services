#!/usr/bin/env bash
# Record the eight golden fixtures from the demo shop, each from its own isolated branch.
#
#   evals/capture_all.sh <path to log0-demo-shop>            (run from services/ai-service)
#
# For every bug: check out its isolated branch in a worktree, start the shop (its own compose project) shipping
# logs to the local log0 stack under a brand-new tenant, run the bug's k6 scenario, wait for the incident to
# appear, record it with `python -m evals.capture`, and tear the shop down. Needs the log0 docker stack, docker,
# k6 and git. Takes ~15 minutes the first time (Docker builds). Nothing is sent to a language model by this script,
# but log0's own pipeline will ask its AI service for incident summaries (the demo logs are synthetic).
set -uo pipefail

SHOP="$(cd "${1:?path to log0-demo-shop}" && pwd)"
WT="${SHOP}-capture-wt"
OUT="$(pwd)/evals/fixtures"
AUTH=http://localhost:8086
INGEST=http://host.docker.internal:8080

# tag | service | message fragment | k6 scenario | seconds | virtual users
CASES=(
  "bug-01-pool-exhausted|storefront-api|connection pool|trending|20|60"
  "bug-02-lowercase-sku|storefront-api|unknown sku|legacy-sku|8|3"
  "bug-03-missing-await|storefront-api|not enough stock|oversell|8|3"
  "bug-04-nil-map|payments|nil map|big-order|8|3"
  "bug-05-retry-storm|payments|payment gateway unavailable, retry|gateway-outage|8|1"
  "bug-06-key-rotation|payments|rejected our credentials|key-rotation|5|1"
  "bug-07-unwrap-none|inventory|Option::unwrap|new-product|8|3"
  "bug-08-lock-across-audit|storefront-api|aborted due to timeout|burst|15|60"
)

(cd "$SHOP" && docker compose down -v >/dev/null 2>&1) || true
git -C "$SHOP" worktree prune
[ -d "$WT" ] || git -C "$SHOP" worktree add -q --detach "$WT" main

new_tenant() {  # prints "tenantId apiKey"
  local uniq pass email reg jwt key
  uniq="cap$(date +%s)$RANDOM"; pass="Cap-${uniq}-Pw!"; email="admin+${uniq}@demo-shop.log0.test"
  reg=$(curl -s -m 30 -H 'Content-Type: application/json' -d "{\"tenantName\":\"Capture ${uniq}\",\"slug\":\"${uniq}\",\"adminEmail\":\"${email}\",\"adminPassword\":\"${pass}\"}" "$AUTH/api/v1/tenants/register")
  jwt=$(curl -s -m 30 -H 'Content-Type: application/json' -d "{\"email\":\"${email}\",\"password\":\"${pass}\"}" "$AUTH/api/v1/auth/login" | python -c "import sys,json; print(json.load(sys.stdin)['accessToken'])")
  key=$(curl -s -m 30 -H 'Content-Type: application/json' -H "Authorization: Bearer ${jwt}" -d '{"name":"capture"}' "$AUTH/api/v1/api-keys" | python -c "import sys,json; print(json.load(sys.stdin)['rawKey'])")
  echo "$(echo "$reg" | python -c "import sys,json; print(json.load(sys.stdin)['tenantId'])") ${key}"
}

failed=0
for entry in "${CASES[@]}"; do
  IFS='|' read -r tag service fragment scenario seconds vus <<<"$entry"
  echo "=== ${tag}"
  git -C "$WT" checkout -q "isolated/${tag}" || { echo "no branch isolated/${tag}"; failed=1; continue; }
  read -r tenant key <<<"$(new_tenant)"
  [ -n "$tenant" ] && [ -n "$key" ] || { echo "could not create a tenant"; failed=1; continue; }
  export LOG0_URL="$INGEST" LOG0_API_KEY="$key" LOG0_ENV=demo LOG0_MIN_LEVEL=WARN
  (cd "$WT" && docker compose -p demoshop-capture up -d --build --force-recreate >/dev/null 2>&1) || { echo "compose up failed"; failed=1; continue; }
  for i in $(seq 1 60); do
    curl -sf -m 2 localhost:8090/health >/dev/null && curl -sf -m 2 localhost:8091/health >/dev/null && curl -sf -m 2 localhost:8092/health >/dev/null && break
  done
  k6 run -e SCENARIO="$scenario" -e DURATION="${seconds}s" -e VUS="$vus" --quiet "$WT/loadgen/shop.js" >/dev/null 2>&1
  captured=0
  for i in $(seq 1 40); do
    if uv run python -m evals.capture --tenant "$tenant" --service "$service" --contains "$fragment" --out "$OUT/${tag}" 2>/dev/null; then captured=1; break; fi
  done
  [ "$captured" = 1 ] || { echo "no incident appeared for ${tag}"; failed=1; }
  (cd "$WT" && docker compose -p demoshop-capture down -v >/dev/null 2>&1)
done

git -C "$SHOP" worktree remove --force "$WT" 2>/dev/null || true
git -C "$SHOP" worktree prune
echo "done, failures=${failed}"
exit "$failed"

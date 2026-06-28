#!/usr/bin/env bash
# Dedup ratio vs template count. For each N, run an isolated burst under a unique
# service name. logs_ingested = count of 202-accepted requests reported by k6 itself
# (deterministic, drain-independent); incidents_created = Postgres incident rows for
# that service, polled until it converges. Emits CSV: templates,logs,incidents,ratio.
#
# Why not count ClickHouse rows: the CH HTTP endpoint intermittently returns empty
# under concurrent insert load, which silently zeroes the count. k6's own accepted-
# request tally is exact and needs no drain wait.
set -u
cd "$(dirname "$0")"
URL="http://host.docker.internal:8080/api/v1/logs"
OUT="results/dedup.csv"
DUR="${DUR:-30s}"
VUS="${VUS:-50}"
NONCE="$(date +%s)"
echo "templates,logs_ingested,incidents_created,dedup_ratio" > "$OUT"

inc_count(){ docker exec log0-postgres psql -U log0 -d log0 -t -A \
  -c "select count(*) from incident where service_name='$1';" 2>/dev/null | tr -d '[:space:]'; }

for N in 1 10 50 100; do
  SVC="dedup${N}_${NONCE}"
  echo "[dedup] N=$N service=$SVC"
  # Capture k6 summary JSON to a file; checks.passes counts 202-accepted posts.
  docker run --rm -i -e URL="$URL" -e VUS="$VUS" -e DURATION="$DUR" -e TEMPLATES="$N" -e SERVICE="$SVC" \
    grafana/k6 run --quiet - < k6-dedup.js 2>/dev/null > "results/_dedup_k6_${N}.json"
  LOGS=$(node -e 'const fs=require("fs");let c=fs.readFileSync(process.argv[1],"utf8");let d=JSON.parse(c.slice(c.indexOf("{"),c.lastIndexOf("}")+1));let ch=d.metrics&&d.metrics.checks&&d.metrics.checks.values;process.stdout.write(String(ch?(ch.passes||0):((d.metrics.http_reqs&&d.metrics.http_reqs.values.count)||0)))' "results/_dedup_k6_${N}.json" 2>/dev/null)
  LOGS=${LOGS//[!0-9]/}
  # Poll incidents until the count holds steady for two 5s checks (clustering window
  # + persist). Incidents converge to ~N distinct fingerprints.
  prev=-1; stable=0; waited=0; INC=0
  while [ "$waited" -lt 90 ]; do
    sleep 5; waited=$((waited+5)); INC=$(inc_count "$SVC"); INC=${INC//[!0-9]/}; INC=${INC:-0}
    if [ "$INC" = "$prev" ] && [ "$INC" -gt 0 ]; then stable=$((stable+1)); else stable=0; fi
    echo "[dedup]   t=${waited}s incidents=${INC} stable=${stable}"
    [ "$stable" -ge 2 ] && break
    prev=$INC
  done
  RATIO=$(awk -v l="${LOGS:-0}" -v i="${INC:-0}" 'BEGIN{ if(i>0) printf "%.0f", l/i; else print "NA" }')
  echo "$N,${LOGS:-0},${INC:-0},${RATIO}" >> "$OUT"
  echo "[dedup] N=$N -> logs_sent=${LOGS} incidents=${INC} ratio=${RATIO}"
done
echo "[dedup] DONE"; cat "$OUT"

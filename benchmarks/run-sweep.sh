#!/usr/bin/env bash
# Latency-vs-VU ingestion sweep. Runs k6 (grafana/k6 image) at each VU step against
# the gateway, saves the full summary JSON per step. Cooldown between steps lets the
# raw-logs backlog drain so each step starts clean.
set -u
cd "$(dirname "$0")"
URL="http://host.docker.internal:8080/api/v1/logs"
OUT="results/sweep"
DUR="${DUR:-45s}"
TEMPLATES="${TEMPLATES:-10}"
mkdir -p "$OUT"

run() { # $1=vus  $2=duration  $3=outfile
  docker run --rm -i -e URL="$URL" -e VUS="$1" -e DURATION="$2" -e TEMPLATES="$TEMPLATES" \
    grafana/k6 run - < k6-sweep.js 2>/dev/null > "$3"
}

echo "[sweep] warm-up (discarded)"
run 50 15s /dev/null

for V in 50 100 150 200 300 500 800; do
  echo "[sweep] VUS=$V DURATION=$DUR -> $OUT/vu$V.json"
  run "$V" "$DUR" "$OUT/vu$V.json"
  echo "[sweep] cooldown 10s (drain raw-logs)"
  sleep 10
done
echo "[sweep] DONE"

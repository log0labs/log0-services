#!/usr/bin/env bash
# Per-container resource footprint UNDER LOAD. Starts a steady 100-VU ingestion load,
# samples `docker stats` repeatedly, writes raw CSV: t_s,container,cpu_pct,mem.
set -u
cd "$(dirname "$0")"
URL="http://host.docker.internal:8080/api/v1/logs"
OUT="results/resource-load.csv"
echo "t_s,container,cpu_pct,mem_used" > "$OUT"

echo "[res] starting 100-VU load (70s) in background"
docker run --rm -i -e URL="$URL" -e VUS=100 -e DURATION=70s -e TEMPLATES=10 \
  grafana/k6 run - < k6-sweep.js > /dev/null 2>&1 &
LOADPID=$!

sleep 8   # let load ramp
t=0
while [ "$t" -lt 56 ]; do
  docker stats --no-stream --format '{{.Name}},{{.CPUPerc}},{{.MemUsage}}' 2>/dev/null \
    | while IFS= read -r line; do echo "${t},${line}" >> "$OUT"; done
  sleep 7
  t=$((t+7))
done
wait "$LOADPID" 2>/dev/null
echo "[res] DONE -> $OUT"

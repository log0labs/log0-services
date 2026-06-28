#!/usr/bin/env bash
# Sample raw-logs (normalization-service) consumer lag over time -> CSV.
# Run alongside a load burst to capture backlog build-up and drain.
# Usage: SECONDS_TO_RUN=120 INTERVAL=1 bash sample-lag.sh results/drain.csv
set -u
cd "$(dirname "$0")"
OUT="${1:-results/drain.csv}"
DURATION_S="${SECONDS_TO_RUN:-120}"
INTERVAL="${INTERVAL:-1}"
GROUP="${GROUP:-normalization-service}"
echo "t_s,total_lag" > "$OUT"
t=0
while [ "$t" -lt "$DURATION_S" ]; do
  LAG=$(docker exec "log0-redpanda" rpk group describe "$GROUP" 2>/dev/null \
        | awk '/^TOTAL-LAG/ {print $2; f=1} END{ if(!f) print 0 }')
  echo "${t},${LAG:-0}" >> "$OUT"
  sleep "$INTERVAL"
  t=$((t+INTERVAL))
done
echo "[lag] DONE -> $OUT"

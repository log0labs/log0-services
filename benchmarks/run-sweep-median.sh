#!/usr/bin/env bash
# Latency-vs-VU sweep, 3 repetitions per step for median aggregation (removes
# single-run GC/scheduler tail spikes). Saves results/sweep/r<rep>_vu<V>.json.
set -u
cd "$(dirname "$0")"
URL="http://host.docker.internal:8080/api/v1/logs"
OUT="results/sweep"; mkdir -p "$OUT"
DUR="${DUR:-30s}"; TEMPLATES="${TEMPLATES:-10}"
VUS_LIST="${VUS_LIST:-50 100 150 200 300 500 800}"
REPS="${REPS:-3}"

run(){ docker run --rm -i -e URL="$URL" -e VUS="$1" -e DURATION="$2" -e TEMPLATES="$TEMPLATES" \
        grafana/k6 run --quiet - < k6-sweep.js 2>/dev/null > "$3"; }

echo "[sweep] warm-up"; run 50 12s /dev/null
for rep in $(seq 1 "$REPS"); do
  for V in $VUS_LIST; do
    echo "[sweep] rep=$rep VUS=$V -> $OUT/r${rep}_vu${V}.json"
    run "$V" "$DUR" "$OUT/r${rep}_vu${V}.json"
    sleep 6
  done
done
echo "[sweep] DONE"

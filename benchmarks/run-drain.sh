#!/usr/bin/env bash
# After-batching backlog drain capture. Sample raw-logs consumer lag every 1s while a
# short high-VU burst forces a backlog, then watch it drain. Emits results/drain-after.csv
# (t_s,total_lag). The "before-batching" series needs a batching revert+redeploy (flagged).
set -u
cd "$(dirname "$0")"
URL="http://host.docker.internal:8080/api/v1/logs"
OUT="${OUT:-results/drain-after.csv}"
GROUP="${GROUP:-normalization-service}"
BURST_VUS="${BURST_VUS:-400}"; BURST_DUR="${BURST_DUR:-20s}"
SAMPLES="${SAMPLES:-90}"

# Confirm the gateway can publish before we start (producer reconnect after any restart).
code=$(curl -s -m 8 -o /dev/null -w '%{http_code}' -X POST "$URL" \
  -H 'Content-Type: application/json' -H 'X-TENANT-ID: 6b1cd754-a35c-491a-9ee8-0e98dfd7b5a8' \
  -H 'X-SERVICE-NAME: drainprobe' -H 'X-ENVIRONMENT: production' -H 'X-API-KEY: bench-key' \
  --data '{"timestamp":"2026-06-28T00:00:00Z","level":"ERROR","message":"probe","trace":"a.b.C(C.java:1)"}')
echo "[drain] gateway probe=$code"; [ "$code" = "202" ] || { echo "[drain] gateway not ready, abort"; exit 1; }

echo "t_s,total_lag" > "$OUT"
lag(){ docker exec log0-redpanda rpk group describe "$GROUP" 2>/dev/null | awk '/^TOTAL-LAG/{print $2; f=1} END{if(!f) print 0}'; }

# Fire the burst at t=8s (background) so we capture a quiet baseline, the build-up, and the drain.
( sleep 8; echo "[drain] burst ${BURST_VUS}VU ${BURST_DUR}"; \
  docker run --rm -i -e URL="$URL" -e VUS="$BURST_VUS" -e DURATION="$BURST_DUR" -e TEMPLATES=10 \
    grafana/k6 run --quiet - < k6-sweep.js >/dev/null 2>&1; echo "[drain] burst done" ) &

t=0
while [ "$t" -lt "$SAMPLES" ]; do
  L=$(lag); L=${L//[!0-9]/}; echo "${t},${L:-0}" >> "$OUT"
  [ $((t % 5)) -eq 0 ] && echo "[drain]   t=${t}s lag=${L:-0}"
  sleep 1; t=$((t+1))
done
wait 2>/dev/null
echo "[drain] DONE -> $OUT"
#!/usr/bin/env bash
# C4 E2E detection latency, measured on a SINGLE clock (this host).
# Clustering emits an incident only on the Nth occurrence (occurrence-threshold=10) of a
# fingerprint within its window. To isolate PIPELINE latency (not accumulation time) we
# fire exactly THRESHOLD identical-fingerprint logs back-to-back, stamp t_fire after the
# last 202, then poll Postgres until that incident is queryable. delta = pipeline traversal
# (clustering emit -> incident-events -> incident-service consume -> DB insert) + poll error.
# Emits results/e2e-latency.csv (iter,latency_ms). Poll granularity ~ docker-exec psql cost.
set -u
cd "$(dirname "$0")"
URL="http://host.docker.internal:8080/api/v1/logs"
TENANT="6b1cd754-a35c-491a-9ee8-0e98dfd7b5a8"
N="${N:-60}"; THRESHOLD="${THRESHOLD:-10}"
OUT="results/e2e-latency.csv"; echo "iter,latency_ms" > "$OUT"
now_ms(){ date +%s%3N; }

post(){ curl -s -m 5 -o /dev/null -w '%{http_code}' -X POST "$URL" \
  -H 'Content-Type: application/json' -H "X-TENANT-ID: $TENANT" \
  -H "X-SERVICE-NAME: $1" -H 'X-ENVIRONMENT: production' -H 'X-API-KEY: bench-key' \
  --data '{"timestamp":"2026-06-28T00:00:00Z","level":"ERROR","message":"ProbeException: fixed detection probe payload","trace":"com.log0.Probe.run(Probe.java:1)"}'; }

found(){ docker exec log0-postgres psql -U log0 -d log0 -t -A \
  -c "select 1 from incident where service_name='$1' limit 1;" 2>/dev/null | tr -d '[:space:]'; }

NONCE="$(date +%s)"
for i in $(seq 1 "$N"); do
  SVC="probe_${NONCE}_${i}"
  for j in $(seq 1 "$THRESHOLD"); do post "$SVC" >/dev/null; done   # cross the threshold
  t0=$(now_ms)
  lat=""
  for _ in $(seq 1 200); do                                          # cap ~ up to a minute
    if [ "$(found "$SVC")" = "1" ]; then lat=$(( $(now_ms) - t0 )); break; fi
  done
  echo "${i},${lat:-TIMEOUT}" >> "$OUT"
  [ $((i % 10)) -eq 0 ] && echo "[e2e] iter=$i latency_ms=${lat:-TIMEOUT}"
done
echo "[e2e] DONE -> $OUT"
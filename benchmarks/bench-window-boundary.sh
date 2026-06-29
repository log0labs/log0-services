#!/usr/bin/env bash
# Clustering window-boundary under-trigger test.
# Clustering buckets by EVENT timestamp, floor-aligned to a 5-min tumbling window
# (ClusterKey.of), and pages only when a single bucket reaches occurrence-threshold=10.
# So a burst of exactly 10 identical-fingerprint logs that STRADDLES a window boundary
# splits into two sub-threshold buckets and never creates an incident. This quantifies
# that miss: for each split ratio a:(10-a) across two adjacent 5-min buckets, fire 10
# logs (a in bucket A at 00:04:30, 10-a in bucket B at 00:05:30, distinct fingerprint per
# trial via a nonce service name) and check whether an incident appears in Postgres.
# Output: results/window-boundary.csv (ratio_a,trial,incident,detect_ms)
set -u
cd "$(dirname "$0")"
URL="http://host.docker.internal:8080/api/v1/logs"
TENANT="6b1cd754-a35c-491a-9ee8-0e98dfd7b5a8"
TRIALS="${TRIALS:-8}"
TS_A="2026-06-28T00:04:30Z"   # bucket floor 00:00 (min 4 -> 4/5*5 = 0)
TS_B="2026-06-28T00:05:30Z"   # bucket floor 00:05 (min 5 -> 5/5*5 = 5)
OUT="results/window-boundary.csv"; echo "ratio_a,trial,incident,detect_ms" > "$OUT"
now_ms(){ date +%s%3N; }

post(){ # $1=service $2=event-timestamp
  curl -s -m 5 -o /dev/null -w '%{http_code}' -X POST "$URL" \
    -H 'Content-Type: application/json' -H "X-TENANT-ID: $TENANT" \
    -H "X-SERVICE-NAME: $1" -H 'X-ENVIRONMENT: production' -H 'X-API-KEY: bench-key' \
    --data "{\"timestamp\":\"$2\",\"level\":\"ERROR\",\"message\":\"BoundaryException: window split probe\",\"trace\":\"com.log0.Boundary.run(Boundary.java:1)\"}" >/dev/null
}
found(){ docker exec log0-postgres psql -U log0 -d log0 -t -A \
  -c "select 1 from incident where service_name='$1' limit 1;" 2>/dev/null | tr -d '[:space:]'; }

NONCE="$(date +%s)"
for A in 10 9 8 7 6 5; do
  B=$((10 - A))
  for t in $(seq 1 "$TRIALS"); do
    SVC="wb_${NONCE}_${A}_${t}"
    for _ in $(seq 1 "$A"); do post "$SVC" "$TS_A"; done
    for _ in $(seq 1 "$B"); do post "$SVC" "$TS_B"; done
    t0=$(now_ms); inc=0; lat=""
    for _ in $(seq 1 80); do                       # ~ up to 8s of polling
      if [ "$(found "$SVC")" = "1" ]; then inc=1; lat=$(( $(now_ms) - t0 )); break; fi
      sleep 0.1
    done
    echo "${A},${t},${inc},${lat:-NA}" >> "$OUT"
  done
  echo "[wb] ratio ${A}:${B} done"
done
echo "[wb] DONE -> $OUT"

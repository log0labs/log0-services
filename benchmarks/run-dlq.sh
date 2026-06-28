#!/usr/bin/env bash
# C7 DLQ injection. Inject a known mix of POISON logs (message contains the fault marker, so
# normalization throws and routes them to raw-logs-dlq) and CLEAN logs (must process normally).
# Verifies: (a) every poison lands on the DLQ - zero silent drops, (b) the poison does NOT
# block clean processing - clean logs still cross the occurrence threshold and create incidents.
# Emits results/dlq.csv.
set -u
cd "$(dirname "$0")"
URL="http://host.docker.internal:8080/api/v1/logs"
TENANT="6b1cd754-a35c-491a-9ee8-0e98dfd7b5a8"
MARKER="__LOG0_FAULT__"
N_POISON="${N_POISON:-300}"; N_CLEAN="${N_CLEAN:-300}"
NONCE="$(date +%s)"; SVC="dlq_${NONCE}"

dlq_hw(){ docker exec log0-redpanda rpk topic describe raw-logs-dlq -p 2>/dev/null \
  | awk 'NR>1{s+=$6} END{print s+0}'; }   # sum HIGH-WATERMARK across partitions

post(){ curl -s -m 8 -o /dev/null -w '%{http_code}' -X POST "$URL" \
  -H 'Content-Type: application/json' -H "X-TENANT-ID: $TENANT" \
  -H "X-SERVICE-NAME: $SVC" -H 'X-ENVIRONMENT: production' -H 'X-API-KEY: bench-key' \
  --data "$1"; }

d0=$(dlq_hw)
echo "[dlq] start: dlq_hw=$d0  service=$SVC  poison=$N_POISON clean=$N_CLEAN"

ok202=0; bad=0
# Interleave clean and poison so we prove the poison does not stall the clean stream.
max=$(( N_POISON > N_CLEAN ? N_POISON : N_CLEAN ))
for i in $(seq 1 "$max"); do
  if [ "$i" -le "$N_CLEAN" ]; then
    c=$(post "{\"timestamp\":\"2026-06-28T00:00:00Z\",\"level\":\"ERROR\",\"message\":\"CleanError: normal failure path\",\"trace\":\"com.log0.Clean.run(Clean.java:1)\"}")
    [ "$c" = "202" ] && ok202=$((ok202+1)) || bad=$((bad+1))
  fi
  if [ "$i" -le "$N_POISON" ]; then
    c=$(post "{\"timestamp\":\"2026-06-28T00:00:00Z\",\"level\":\"ERROR\",\"message\":\"PoisonError ${MARKER} deliberately broken\",\"trace\":\"com.log0.Poison.run(Poison.java:1)\"}")
    [ "$c" = "202" ] && ok202=$((ok202+1)) || bad=$((bad+1))
  fi
done

echo "[dlq] sent, settling 20s for drain"; sleep 20
d1=$(dlq_hw)
DLQ_DELTA=$(( d1 - d0 ))
INC=$(docker exec log0-postgres psql -U log0 -d log0 -t -A -c "select count(*) from incident where service_name='${SVC}';" 2>/dev/null | tr -d '[:space:]')

{
  echo "metric,value"
  echo "poison_sent,$N_POISON"
  echo "clean_sent,$N_CLEAN"
  echo "http_202,$ok202"
  echo "http_non202,$bad"
  echo "dlq_messages_added,$DLQ_DELTA"
  echo "incidents_from_clean,${INC:-0}"
} > results/dlq.csv
echo "[dlq] DONE"; cat results/dlq.csv
echo "[dlq] expect: dlq_messages_added ~= poison_sent ($N_POISON); incidents_from_clean >= 1 (clean path unblocked)"
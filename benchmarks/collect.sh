#!/usr/bin/env bash
# Snapshot key pipeline metrics. Usage: bash collect.sh <label>
L="${1:-snap}"
RP="docker exec log0-redpanda rpk"
echo "===== SNAPSHOT: $L @ $(date -u +%H:%M:%S)Z ====="

echo "--- docker stats (cpu / mem) ---"
docker stats --no-stream --format "{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}" | grep "log0-"

echo "--- topic high-watermarks (total records produced) ---"
for t in raw-logs normalized-logs incident-events notification-events raw-logs-dlq; do
  hw=$($RP topic describe "$t" -p 2>/dev/null | awk 'NR>1 && NF{s+=$(NF)} END{print s+0}')
  echo "$t = $hw"
done

echo "--- consumer group total lag ---"
for g in normalization-service clustering-service incident-service notification-service; do
  total=$($RP group describe "$g" 2>/dev/null | awk '
    /PARTITION/ { for(i=1;i<=NF;i++) if($i=="LAG") c=i; next }
    c && $c ~ /^[0-9]+$/ { s+=$c } END { print s+0 }')
  echo "$g total_lag = ${total:-0}"
done

echo "--- clickhouse log_events count ---"
docker exec log0-clickhouse clickhouse-client -u log0 --password log0 -q "SELECT count() FROM log0.log_events" 2>/dev/null || echo "(query failed)"

echo "--- postgres incident count ---"
docker exec log0-postgres psql -U log0 -d log0 -t -c "SELECT count(*) FROM incident" 2>/dev/null | tr -d ' \n'; echo
echo "============================================="

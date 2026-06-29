#!/usr/bin/env bash
# Samples clustering-service container memory and the clustering consumer's cumulative
# consumed offset (a proxy for distinct windows created in the cardinality test) once a
# second -> CSV. Run alongside k6-cardinality.js. cardinality(t) ~ consumed(t) - consumed(0).
# Usage: SECONDS_TO_RUN=180 bash sample-clustering-mem.sh results/clustering-mem.csv
set -u
cd "$(dirname "$0")"
OUT="${1:-results/clustering-mem.csv}"
DURATION_S="${SECONDS_TO_RUN:-180}"
echo "t_s,mem_mib,consumed_total" > "$OUT"

consumed(){ # sum CURRENT-OFFSET across normalized-logs partitions for the clustering group
  docker exec log0-redpanda rpk group describe clustering-service 2>/dev/null \
    | awk '$1=="normalized-logs"{s+=$3} END{print s+0}'
}
mem_mib(){ # clustering container RSS in MiB (strip unit, convert if GiB)
  docker stats --no-stream --format '{{.MemUsage}}' log0-clustering 2>/dev/null \
    | awk '{u=$1; v=u; sub(/[A-Za-z]+$/,"",v); if(u ~ /GiB/) v=v*1024; print v}'
}

t=0
while [ "$t" -lt "$DURATION_S" ]; do
  echo "${t},$(mem_mib),$(consumed)" >> "$OUT"
  sleep 1
  t=$((t+1))
done
echo "[clustering-mem] DONE -> $OUT"

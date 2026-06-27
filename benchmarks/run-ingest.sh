#!/usr/bin/env bash
# One ingestion load run with before/after/drained snapshots.
# Usage: bash run-ingest.sh <label> [VUS] [DURATION] [TEMPLATES] [TENANTS] [SKEW]
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
LABEL="${1:?label}"; VUS="${2:-100}"; DUR="${3:-60s}"; TPL="${4:-20}"; TEN="${5:-1}"; SKEW="${6:-0}"
DIR="$HERE/results"; mkdir -p "$DIR"
OUT="$DIR/${LABEL}.txt"
K6="/c/Program Files/k6/k6.exe"

{
  echo "##### RUN=$LABEL VUS=$VUS DUR=$DUR TEMPLATES=$TPL TENANTS=$TEN SKEW=$SKEW"
  bash "$HERE/collect.sh" "BEFORE-$LABEL"
  echo ">>> k6 start $(date -u +%H:%M:%S)Z"
  "$K6" run -e VUS="$VUS" -e DURATION="$DUR" -e TEMPLATES="$TPL" -e TENANTS="$TEN" -e SKEW="$SKEW" \
     --summary-export="$DIR/${LABEL}-summary.json" "$HERE/k6-ingest.js" 2>&1
  echo ">>> k6 done $(date -u +%H:%M:%S)Z"
  bash "$HERE/collect.sh" "RIGHT-AFTER-$LABEL"
  echo ">>> waiting 45s for pipeline to drain..."
  sleep 45
  bash "$HERE/collect.sh" "DRAINED-$LABEL"
  echo "##### END $LABEL"
} | tee "$OUT"

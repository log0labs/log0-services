#!/usr/bin/env bash
# C8 AI provider latency. ai-service POST /api/v1/summaries runs the LLM call synchronously
# before returning 202, so the HTTP response time ~= provider (groq) latency + a tiny intra-
# network PATCH back to incident-service. We measure it externally (no code change). Only
# GROQ_API_KEY is configured, so this measures the live groq llama-3.3-70b path; the other
# providers are configured-but-unkeyed. Emits results/ai-latency.csv (iter,ms,http_code).
set -u
cd "$(dirname "$0")"
BASE="http://localhost:8085"
N="${N:-30}"
OUT="results/ai-latency.csv"; echo "iter,ms,http_code" > "$OUT"
TENANT="6b1cd754-a35c-491a-9ee8-0e98dfd7b5a8"

# A fixed UUID per iter (incidentId); the PATCH callback may 404 (incident may not exist) but
# that is caught inside ai-service and does not change the dominant groq call cost.
for i in $(seq 1 "$N"); do
  IID="00000000-0000-4000-8000-$(printf '%012d' "$i")"
  body=$(cat <<JSON
{"incidentId":"$IID","tenantId":"$TENANT","serviceName":"payment-service","environment":"production","severity":"HIGH","occurrenceCount":${i}0,"firstSeenAt":"2026-06-28T09:00:00Z","lastSeenAt":"2026-06-28T09:05:00Z","topMessages":["NullPointerException at PaymentProcessor.charge line ${i}","Timeout calling upstream bank API after 3000ms","Connection pool exhausted on shard ${i}"]}
JSON
)
  read -r ms code < <(curl -s -m 60 -o /dev/null -w '%{time_total} %{http_code}' \
    -X POST "$BASE/api/v1/summaries" -H 'Content-Type: application/json' --data "$body")
  msi=$(awk -v t="$ms" 'BEGIN{printf "%.0f", t*1000}')
  echo "${i},${msi},${code}" >> "$OUT"
  [ $((i % 5)) -eq 0 ] && echo "[ai] iter=$i ms=$msi code=$code"
  sleep "${GAP:-5}"   # space calls under groq free-tier rate limit (~30 RPM) to avoid 429s
done
echo "[ai] DONE -> $OUT"
node -e '
  const fs=require("fs");
  // The controller returns 202 even when the groq call fails internally (caught + logged), so
  // a rate-limited 429 shows up as a FAST 202. Real llama-3.3-70b generations take >=400ms;
  // separate the two so we report true provider latency, not failure latency.
  const all=fs.readFileSync("results/ai-latency.csv","utf8").trim().split("\n").slice(1)
    .map(l=>l.split(",")).filter(p=>p[2]==="202").map(p=>+p[1]);
  const real=all.filter(m=>m>=400).sort((a,b)=>a-b);
  const fast=all.filter(m=>m<400).length;
  const pc=p=>real[Math.min(real.length-1,Math.floor(p*real.length))];
  console.log("groq llama-3.3-70b generation latency ms, n="+real.length+" real, "+fast+" rate-limited-fast-fails excluded");
  if(real.length) console.log("min",real[0],"p50",pc(.5),"p95",pc(.95),"p99",pc(.99),"max",real[real.length-1]);
'
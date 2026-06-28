#!/usr/bin/env bash
# C9 auth throughput. Runs login (JWT issue, bcrypt) then validate-key (SHA-256) as two
# isolated 30s constant-VU runs. Parses k6 summary -> results/auth.csv.
set -u
cd "$(dirname "$0")"
EMAIL="$(cat results/_auth_user.txt)"; APIKEY="$(cat results/_auth_apikey.txt)"
VUS="${VUS:-30}"; DUR="${DUR:-30s}"
echo "mode,rps,p50_ms,p95_ms,p99_ms,max_ms,fail_pct" > results/auth.csv

run(){ local mode="$1"
  echo "[auth] $mode VUS=$VUS DUR=$DUR"
  docker run --rm -i -e BASE="http://host.docker.internal:8086" -e MODE="$mode" -e VUS="$VUS" -e DURATION="$DUR" \
    -e EMAIL="$EMAIL" -e PASSWORD="benchpass123" -e APIKEY="$APIKEY" \
    grafana/k6 run --quiet - < k6-auth.js 2>/dev/null > "results/_auth_${mode}.json"
  node -e '
    const fs=require("fs");let c=fs.readFileSync(process.argv[2],"utf8");
    let d=JSON.parse(c.slice(c.indexOf("{"),c.lastIndexOf("}")+1)).metrics;
    let dur=d.http_req_duration.values, reqs=d.http_reqs.values, fail=d.http_req_failed.values;
    let row=[process.argv[1], Math.round(reqs.rate), (dur.med).toFixed(1), dur["p(95)"].toFixed(1),
             dur["p(99)"].toFixed(1), (dur.max).toFixed(0), (fail.rate*100).toFixed(2)].join(",");
    fs.appendFileSync("results/auth.csv", row+"\n"); console.log("[auth] "+row);
  ' "$mode" "results/_auth_${mode}.json"
  sleep 4
}
run login
run validate
echo "[auth] DONE"; cat results/auth.csv
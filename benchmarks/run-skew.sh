#!/usr/bin/env bash
# C6 noisy-neighbor. One hot tenant (~90% traffic) + 4 quiet tenants share the unthrottled
# gateway. Captures per-tenant-class latency to show coupling. -> results/skew.csv
set -u
cd "$(dirname "$0")"
VUS="${VUS:-100}"; DUR="${DUR:-40s}"
docker run --rm -i -e URL="http://host.docker.internal:8080/api/v1/logs" -e VUS="$VUS" -e DURATION="$DUR" -e HOT_SHARE=0.9 \
  grafana/k6 run --quiet - < k6-skew.js 2>/dev/null > results/_skew.json
node -e '
  const fs=require("fs");let c=fs.readFileSync("results/_skew.json","utf8");
  let m=JSON.parse(c.slice(c.indexOf("{"),c.lastIndexOf("}")+1)).metrics;
  const g=(k,f)=>m[k]&&m[k].values?m[k].values[f]:null;
  const rows=[["class","reqs","p50_ms","p95_ms","p99_ms"]];
  rows.push(["hot",  Math.round(g("hot_reqs","count")),  g("hot_latency","med").toFixed(1),  g("hot_latency","p(95)").toFixed(1),  g("hot_latency","p(99)").toFixed(1)]);
  rows.push(["quiet",Math.round(g("quiet_reqs","count")),g("quiet_latency","med").toFixed(1),g("quiet_latency","p(95)").toFixed(1),g("quiet_latency","p(99)").toFixed(1)]);
  fs.writeFileSync("results/skew.csv", rows.map(r=>r.join(",")).join("\n")+"\n");
  rows.forEach(r=>console.log(r.join("\t")));
'
echo "[skew] DONE -> results/skew.csv"
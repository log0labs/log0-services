// C6 multi-tenant skew / noisy neighbor. Five tenants share the ingestion gateway with NO
// per-tenant rate limiting (none exists in the code). One tenant ("hot") sends ~90% of
// traffic; four "quiet" tenants split the rest. We tag every request with its tenant so the
// summary reports per-tenant latency, showing whether the hot tenant degrades the quiet ones
// (coupling) or stays isolated. Honest framing: this measures the CURRENT design (shared,
// unthrottled), motivating the designed-but-unbuilt per-tenant quota.
import http from "k6/http";
import { check } from "k6";
import { Trend, Counter } from "k6/metrics";

const hotLat = new Trend("hot_latency", true);
const quietLat = new Trend("quiet_latency", true);
const hotReqs = new Counter("hot_reqs");
const quietReqs = new Counter("quiet_reqs");

const URL = __ENV.URL || "http://host.docker.internal:8080/api/v1/logs";
const VUS = parseInt(__ENV.VUS || "100");
const DURATION = __ENV.DURATION || "40s";
const HOT_SHARE = parseFloat(__ENV.HOT_SHARE || "0.9");

// Distinct tenant UUIDs (fixed so runs are comparable).
const HOT = "11111111-1111-1111-1111-111111111111";
const QUIET = [
  "22222222-2222-2222-2222-222222222222",
  "33333333-3333-3333-3333-333333333333",
  "44444444-4444-4444-4444-444444444444",
  "55555555-5555-5555-5555-555555555555",
];

export const options = {
  scenarios: { skew: { executor: "constant-vus", vus: VUS, duration: DURATION } },
  summaryTrendStats: ["med", "p(95)", "p(99)"],
};

function pick() {
  if (Math.random() < HOT_SHARE) return { id: HOT, tag: "hot" };
  return { id: QUIET[Math.floor(Math.random() * QUIET.length)], tag: "quiet" };
}

export default function () {
  const t = pick();
  const n = Math.floor(Math.random() * 100000);
  const body = JSON.stringify({
    timestamp: new Date().toISOString(), level: "ERROR",
    message: "ErrorKappa: failed after " + n + "ms", trace: "com.log0.Skew.run(Skew.java:1)",
  });
  const res = http.post(URL, body, {
    headers: {
      "Content-Type": "application/json", "X-TENANT-ID": t.id,
      "X-SERVICE-NAME": "skew-svc", "X-ENVIRONMENT": "production", "X-API-KEY": "bench-key",
    },
    tags: { tenant: t.tag },
  });
  if (t.tag === "hot") { hotLat.add(res.timings.duration); hotReqs.add(1); }
  else { quietLat.add(res.timings.duration); quietReqs.add(1); }
  check(res, { "202": (r) => r.status === 202 });
}

export function handleSummary(data) { return { stdout: JSON.stringify(data) }; }

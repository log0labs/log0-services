// Dedup-ratio test: emit exactly TEMPLATES distinct fingerprint families under one
// unique service name (SERVICE) so the run is isolated from history. Constant trace +
// constant exception text => fingerprint varies only by the templated message, so the
// number of incidents created should converge to ~TEMPLATES while logs ingested >> that.
import http from "k6/http";
import { check } from "k6";

const URL = __ENV.URL || "http://host.docker.internal:8080/api/v1/logs";
const VUS = parseInt(__ENV.VUS || "50");
const DURATION = __ENV.DURATION || "30s";
const TEMPLATES = parseInt(__ENV.TEMPLATES || "10");
const SERVICE = __ENV.SERVICE || "dedup-test";
const TENANT = __ENV.TENANT || "6b1cd754-a35c-491a-9ee8-0e98dfd7b5a8";
const TRACE = "com.log0.Dedup.run(Dedup.java:1)";

export const options = {
  scenarios: { ingest: { executor: "constant-vus", vus: VUS, duration: DURATION } },
  summaryTrendStats: ["avg", "p(95)", "p(99)"],
};

// k -> pure-alpha label (A, B, ... Z, AA, AB, ...) so the template-distinguishing
// token is never eaten by numeric templatization (digits collapse to a placeholder).
function alpha(k) { let s = ""; k++; while (k > 0) { k--; s = String.fromCharCode(65 + (k % 26)) + s; k = Math.floor(k / 26); } return s; }

export default function () {
  const k = Math.floor(Math.random() * TEMPLATES);   // which template family
  const n = Math.floor(Math.random() * 100000);       // dynamic value -> collapses
  const body = JSON.stringify({
    timestamp: new Date().toISOString(),
    level: "ERROR",
    message: "Error" + alpha(k) + "Exception: operation failed after " + n + "ms on shard " + n,
    trace: TRACE,
  });
  const res = http.post(URL, body, {
    headers: {
      "Content-Type": "application/json",
      "X-TENANT-ID": TENANT,
      "X-SERVICE-NAME": SERVICE,
      "X-ENVIRONMENT": "production",
      "X-API-KEY": "bench-key",
    },
  });
  check(res, { "status is 202": (r) => r.status === 202 });
}

export function handleSummary(data) {
  return { stdout: JSON.stringify(data) };
}

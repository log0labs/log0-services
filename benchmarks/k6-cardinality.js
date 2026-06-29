// Clustering cardinality / memory-growth load. The in-memory OccurrenceStore is a
// ConcurrentHashMap with NO eviction (InMemoryOccurrenceStore javadoc), so every distinct
// (tenant, fingerprint, 5-min window) creates one map entry that lives until the process
// restarts. This driver makes every request a UNIQUE fingerprint (unique first stack frame
// via a monotonic per-iteration index) with a FIXED event timestamp, so each request adds
// exactly one window entry with count=1 (below threshold -> no incident). Cumulative
// requests therefore track the clustering map's cardinality, and clustering memory should
// climb roughly linearly with it. Push far enough and the service OOMs.
//
// Run: k6 run -e VUS=60 -e DURATION=120s k6-cardinality.js
import http from "k6/http";
import exec from "k6/execution";

const URL = __ENV.URL || "http://host.docker.internal:8080/api/v1/logs";
const TENANT = __ENV.TENANT || "6b1cd754-a35c-491a-9ee8-0e98dfd7b5a8";
const VUS = parseInt(__ENV.VUS || "60");
const DURATION = __ENV.DURATION || "120s";
const TS = __ENV.TS || "2026-06-28T12:00:00Z"; // fixed -> one window bucket for all

export const options = {
  scenarios: { card: { executor: "constant-vus", vus: VUS, duration: DURATION } },
};

export default function () {
  const idx = exec.scenario.iterationInTest; // unique, monotonic across the whole run
  const body = JSON.stringify({
    timestamp: TS,
    level: "ERROR",
    message: "CardinalityException: distinct fingerprint probe",
    // unique first stack frame -> unique fingerprint per iteration
    trace: "com.log0.card.F" + idx + ".run(F" + idx + ".java:1)",
  });
  http.post(URL, body, {
    headers: {
      "Content-Type": "application/json",
      "X-TENANT-ID": TENANT,
      "X-SERVICE-NAME": "card-probe",
      "X-ENVIRONMENT": "production",
      "X-API-KEY": "bench-key",
    },
  });
}

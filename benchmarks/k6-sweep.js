// k6 ingestion sweep — same request shape as k6-ingest.js, but emits the full
// summary as JSON on stdout (handleSummary) so the runner can save one file per
// VU step without a volume mount. Env knobs: URL, VUS, DURATION, TEMPLATES, TENANTS, SKEW.
import http from "k6/http";
import { check } from "k6";

const URL = __ENV.URL || "http://host.docker.internal:8080/api/v1/logs";
const VUS = parseInt(__ENV.VUS || "100");
const DURATION = __ENV.DURATION || "45s";
const TEMPLATES = parseInt(__ENV.TEMPLATES || "10");
const TENANTS = parseInt(__ENV.TENANTS || "1");
const SKEW = __ENV.SKEW === "1";

const TENANT_IDS = [
  "6b1cd754-a35c-491a-9ee8-0e98dfd7b5a8",
  "11111111-1111-1111-1111-111111111111",
  "22222222-2222-2222-2222-222222222222",
  "33333333-3333-3333-3333-333333333333",
  "44444444-4444-4444-4444-444444444444",
];

const SHAPES = [
  "Connection timeout to payment-gateway after {n}ms",
  "NullPointerException at OrderMapper.map line {n}",
  "Failed to acquire DB connection from pool, waited {n}ms",
  "Redis GET timed out after {n}ms for key user:{n}",
  "HTTP 503 from inventory-service, retry {n}",
  "Kafka publish failed for partition {n}",
  "Deserialization error at offset {n}",
  "OutOfMemoryError: heap, {n} bytes requested",
  "Slow query took {n}ms: SELECT * FROM orders",
  "Auth token expired {n}s ago for tenant",
];

export const options = {
  scenarios: { ingest: { executor: "constant-vus", vus: VUS, duration: DURATION } },
  summaryTrendStats: ["avg", "min", "med", "p(95)", "p(99)", "max"],
};

function pickTenant() {
  if (TENANTS <= 1) return TENANT_IDS[0];
  if (SKEW && Math.random() < 0.9) return TENANT_IDS[0];
  return TENANT_IDS[Math.floor(Math.random() * Math.min(TENANTS, TENANT_IDS.length))];
}

export default function () {
  const shape = SHAPES[Math.floor(Math.random() * Math.min(TEMPLATES, SHAPES.length))];
  const n = Math.floor(Math.random() * 100000);
  const body = JSON.stringify({
    timestamp: new Date().toISOString(),
    level: "ERROR",
    message: shape.replace(/\{n\}/g, String(n)),
    trace: "com.log0.PaymentProcessor.charge(PaymentProcessor.java:" + (40 + (n % 5)) + ")",
  });
  const res = http.post(URL, body, {
    headers: {
      "Content-Type": "application/json",
      "X-TENANT-ID": pickTenant(),
      "X-SERVICE-NAME": "payment-service",
      "X-ENVIRONMENT": "production",
      "X-API-KEY": "bench-key",
    },
  });
  check(res, { "status is 202": (r) => r.status === 202 });
}

export function handleSummary(data) {
  return { stdout: JSON.stringify(data) };
}

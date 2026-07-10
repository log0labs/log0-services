// k6 load test for the log0 ingestion path (POST /api/v1/logs on the gateway).
// Measures the 202-accept latency and sustained throughput of a single-host deploy.
//
// Env knobs (all optional):
//   URL        target endpoint   (default http://localhost:8080/api/v1/logs)
//   VUS        virtual users     (default 50)
//   DURATION   test duration     (default 60s)
//   TEMPLATES  distinct error shapes (default 20) - controls the dedup ratio
//   TENANTS    distinct tenant ids   (default 1) - for multi-tenant / skew runs
//   SKEW       if "1", ~90% of load goes to tenant 0 (hot-partition test)
//
// Run:  k6 run -e VUS=100 -e DURATION=60s k6-ingest.js

import http from "k6/http";
import { check } from "k6";

// --- inline API-key seeding (self-contained: the run-*.sh wrappers pipe this
// file to the k6 container via stdin, which cannot resolve local imports) ---
const AUTH_URL = __ENV.AUTH_URL || "http://host.docker.internal:8086";
const SEED_PASS = "BenchPass123!";
function seedTenant(label) {
  const uniq = `${label}-${Date.now()}-${Math.floor(Math.random() * 1e6)}`;
  const email = `bench+${uniq}@log0.test`;
  const reg = http.post(`${AUTH_URL}/api/v1/tenants/register`,
    JSON.stringify({ tenantName: `Bench ${uniq}`, slug: `bench-${uniq}`, adminEmail: email, adminPassword: SEED_PASS }),
    { headers: { "Content-Type": "application/json" } });
  if (reg.status !== 201) throw new Error(`seed register failed (${reg.status}): ${reg.body}`);
  const login = http.post(`${AUTH_URL}/api/v1/auth/login`,
    JSON.stringify({ email, password: SEED_PASS }), { headers: { "Content-Type": "application/json" } });
  const jwt = login.json("accessToken");
  const key = http.post(`${AUTH_URL}/api/v1/api-keys`, JSON.stringify({ name: `bench-${label}` }),
    { headers: { "Content-Type": "application/json", Authorization: `Bearer ${jwt}` } });
  const rawKey = key.json("rawKey");
  if (!rawKey) throw new Error(`seed create-key failed (${key.status}): ${key.body}`);
  return { apiKey: rawKey, tenantId: reg.json("tenantId") };
}
function seedTenants(n) { const out = []; for (let i = 0; i < n; i++) out.push(seedTenant(`t${i}`)); return out; }

const URL = __ENV.URL || "http://localhost:8080/api/v1/logs";
const VUS = parseInt(__ENV.VUS || "50");
const DURATION = __ENV.DURATION || "60s";
const TEMPLATES = parseInt(__ENV.TEMPLATES || "20");
const TENANTS = parseInt(__ENV.TENANTS || "1");
const SKEW = __ENV.SKEW === "1";

// Distinct error "shapes". Each becomes one fingerprint family; the {n} varies
// per request so the template collapses many lines into one incident.
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
  scenarios: {
    ingest: {
      executor: "constant-vus",
      vus: VUS,
      duration: DURATION,
    },
  },
  summaryTrendStats: ["avg", "min", "med", "p(95)", "p(99)", "max"],
};

// Seed one real API key per distinct tenant once, before the load starts.
// The tenant is derived from the key by the gateway, so multi-tenant / skew
// runs pick among keys (not client-supplied tenant ids).
export function setup() {
  return { keys: seedTenants(Math.max(TENANTS, 1)).map((t) => t.apiKey) };
}

function pickKey(keys) {
  if (keys.length <= 1) return keys[0];
  if (SKEW && Math.random() < 0.9) return keys[0];
  return keys[Math.floor(Math.random() * keys.length)];
}

export default function (data) {
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
      "X-SERVICE-NAME": "payment-service",
      "X-ENVIRONMENT": "production",
      "X-API-KEY": pickKey(data.keys),
    },
  });

  check(res, { "status is 202": (r) => r.status === 202 });
}

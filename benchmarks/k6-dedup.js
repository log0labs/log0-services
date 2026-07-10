// Dedup-ratio test: emit exactly TEMPLATES distinct fingerprint families under one
// unique service name (SERVICE) so the run is isolated from history. Constant trace +
// constant exception text => fingerprint varies only by the templated message, so the
// number of incidents created should converge to ~TEMPLATES while logs ingested >> that.
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

const URL = __ENV.URL || "http://host.docker.internal:8080/api/v1/logs";
const VUS = parseInt(__ENV.VUS || "50");
const DURATION = __ENV.DURATION || "30s";
const TEMPLATES = parseInt(__ENV.TEMPLATES || "10");
const SERVICE = __ENV.SERVICE || "dedup-test";
const TRACE = "com.log0.Dedup.run(Dedup.java:1)";

export const options = {
  scenarios: { ingest: { executor: "constant-vus", vus: VUS, duration: DURATION } },
  summaryTrendStats: ["avg", "p(95)", "p(99)"],
};

// k -> pure-alpha label (A, B, ... Z, AA, AB, ...) so the template-distinguishing
// token is never eaten by numeric templatization (digits collapse to a placeholder).
function alpha(k) { let s = ""; k++; while (k > 0) { k--; s = String.fromCharCode(65 + (k % 26)) + s; k = Math.floor(k / 26); } return s; }

// Fresh tenant + key per run - also isolates the dedup count from history.
export function setup() {
  return seedTenant("dedup");
}

export default function (data) {
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
      "X-SERVICE-NAME": SERVICE,
      "X-ENVIRONMENT": "production",
      "X-API-KEY": data.apiKey,
    },
  });
  check(res, { "status is 202": (r) => r.status === 202 });
}

export function handleSummary(data) {
  return { stdout: JSON.stringify(data) };
}

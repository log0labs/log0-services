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
const VUS = parseInt(__ENV.VUS || "60");
const DURATION = __ENV.DURATION || "120s";
const TS = __ENV.TS || "2026-06-28T12:00:00Z"; // fixed -> one window bucket for all

export const options = {
  scenarios: { card: { executor: "constant-vus", vus: VUS, duration: DURATION } },
};

export function setup() {
  return seedTenant("card");
}

export default function (data) {
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
      "X-SERVICE-NAME": "card-probe",
      "X-ENVIRONMENT": "production",
      "X-API-KEY": data.apiKey,
    },
  });
}

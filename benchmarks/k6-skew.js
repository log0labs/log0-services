// C6 multi-tenant skew / noisy neighbor. Five tenants share the ingestion gateway with NO
// per-tenant rate limiting (none exists in the code). One tenant ("hot") sends ~90% of
// traffic; four "quiet" tenants split the rest. We tag every request with its tenant so the
// summary reports per-tenant latency, showing whether the hot tenant degrades the quiet ones
// (coupling) or stays isolated. Honest framing: this measures the CURRENT design (shared,
// unthrottled), motivating the designed-but-unbuilt per-tenant quota.
import http from "k6/http";
import { check } from "k6";
import { Trend, Counter } from "k6/metrics";

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

const hotLat = new Trend("hot_latency", true);
const quietLat = new Trend("quiet_latency", true);
const hotReqs = new Counter("hot_reqs");
const quietReqs = new Counter("quiet_reqs");

const URL = __ENV.URL || "http://host.docker.internal:8080/api/v1/logs";
const VUS = parseInt(__ENV.VUS || "100");
const DURATION = __ENV.DURATION || "40s";
const HOT_SHARE = parseFloat(__ENV.HOT_SHARE || "0.9");

export const options = {
  scenarios: { skew: { executor: "constant-vus", vus: VUS, duration: DURATION } },
  summaryTrendStats: ["med", "p(95)", "p(99)"],
};

// Seed one hot tenant + four quiet tenants, each with its own API key. The
// gateway derives the tenant from the key, so the hot/quiet split is now a
// choice among keys rather than a client-supplied X-TENANT-ID.
export function setup() {
  return {
    hot: seedTenant("hot").apiKey,
    quiet: seedTenants(4).map((t) => t.apiKey),
  };
}

function pick(data) {
  if (Math.random() < HOT_SHARE) return { key: data.hot, tag: "hot" };
  return { key: data.quiet[Math.floor(Math.random() * data.quiet.length)], tag: "quiet" };
}

export default function (data) {
  const t = pick(data);
  const n = Math.floor(Math.random() * 100000);
  const body = JSON.stringify({
    timestamp: new Date().toISOString(), level: "ERROR",
    message: "ErrorKappa: failed after " + n + "ms", trace: "com.log0.Skew.run(Skew.java:1)",
  });
  const res = http.post(URL, body, {
    headers: {
      "Content-Type": "application/json",
      "X-SERVICE-NAME": "skew-svc", "X-ENVIRONMENT": "production", "X-API-KEY": t.key,
    },
    tags: { tenant: t.tag },
  });
  if (t.tag === "hot") { hotLat.add(res.timings.duration); hotReqs.add(1); }
  else { quietLat.add(res.timings.duration); quietReqs.add(1); }
  check(res, { "202": (r) => r.status === 202 });
}

export function handleSummary(data) { return { stdout: JSON.stringify(data) }; }

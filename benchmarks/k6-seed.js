// Dashboard-seeding load: realistic incidents across several services and all four
// severities (level -> severity: FATAL=CRITICAL, ERROR=HIGH, WARN=MEDIUM, else=LOW), under a
// chosen tenant so the logged-in console user sees a populated, varied board. Each
// (service, template) needs >=10 occurrences (occurrence-threshold) to become an incident.
import http from "k6/http";

const URL = __ENV.URL || "http://host.docker.internal:8080/api/v1/logs";
// The gateway derives the tenant from the API key, so to populate a SPECIFIC
// tenant's dashboard (e.g. the account you log into the console with), pass that
// tenant's raw API key: create one in the console under Settings -> API keys and
// run with -e API_KEY=log0_...  (do NOT use a throwaway seeded tenant here).
const API_KEY = __ENV.API_KEY;
const VUS = parseInt(__ENV.VUS || "40");
const DURATION = __ENV.DURATION || "45s";

const SERVICES = [
  "payment-service", "checkout-service", "auth-service", "inventory-service",
  "notification-worker", "search-api", "user-profile", "shipping-service",
];
// Weighted levels: mostly ERROR/HIGH with a realistic tail of CRITICAL/MEDIUM/LOW.
const LEVELS = ["FATAL", "ERROR", "ERROR", "ERROR", "WARN", "WARN", "INFO"];
const TEMPLATES = [
  "NullPointerException in {} handler",
  "Timeout calling upstream after {}ms",
  "Connection pool exhausted on shard {}",
  "Failed to deserialize message {}",
  "Out of memory allocating {} buffers",
  "Database deadlock on transaction {}",
];

export const options = {
  scenarios: { seed: { executor: "constant-vus", vus: VUS, duration: DURATION } },
};

export function setup() {
  if (!API_KEY) {
    throw new Error("k6-seed: set -e API_KEY=<raw key> (console -> Settings -> API keys)");
  }
}

export default function () {
  const svc = SERVICES[Math.floor(Math.random() * SERVICES.length)];
  const level = LEVELS[Math.floor(Math.random() * LEVELS.length)];
  const tpl = TEMPLATES[Math.floor(Math.random() * TEMPLATES.length)];
  const n = Math.floor(Math.random() * 100000);
  const body = JSON.stringify({
    timestamp: new Date().toISOString(), level,
    message: tpl.replace("{}", String(n)),
    trace: "com.log0." + svc.replace(/-/g, "_") + ".handle(Handler.java:" + (n % 400) + ")",
  });
  http.post(URL, body, {
    headers: {
      "Content-Type": "application/json",
      "X-SERVICE-NAME": svc, "X-ENVIRONMENT": "production", "X-API-KEY": API_KEY,
    },
  });
}

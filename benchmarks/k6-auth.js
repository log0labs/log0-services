// Auth-service throughput. MODE=login issues a JWT (bcrypt password verify + HS384 sign);
// MODE=validate validates an API key (SHA-256 hash + lookup). Two very different cost
// profiles on purpose. Each run is single-MODE so throughput attributes cleanly.
import http from "k6/http";
import { check } from "k6";

const BASE = __ENV.BASE || "http://host.docker.internal:8086";
const MODE = __ENV.MODE || "login";
const VUS = parseInt(__ENV.VUS || "30");
const DURATION = __ENV.DURATION || "30s";
const EMAIL = __ENV.EMAIL;
const PASSWORD = __ENV.PASSWORD || "benchpass123";
const APIKEY = __ENV.APIKEY;

export const options = {
  scenarios: { auth: { executor: "constant-vus", vus: VUS, duration: DURATION } },
  summaryTrendStats: ["med", "p(95)", "p(99)", "max"],
};

export default function () {
  let res, ok;
  if (MODE === "login") {
    res = http.post(`${BASE}/api/v1/auth/login`, JSON.stringify({ email: EMAIL, password: PASSWORD }),
      { headers: { "Content-Type": "application/json" } });
    ok = res.status === 200;
  } else {
    res = http.post(`${BASE}/api/v1/auth/validate-key`, null, { headers: { "X-Api-Key": APIKEY } });
    ok = res.status === 200;
  }
  check(res, { "ok": () => ok });
}

export function handleSummary(data) { return { stdout: JSON.stringify(data) }; }

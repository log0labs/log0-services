#!/usr/bin/env node
/**
 * log0 dummy-log seeder
 * ----------------------
 * Floods the ingestion-gateway (POST /api/v1/logs) with realistic dummy logs so the
 * console can be exercised end-to-end: incidents, severities, environments, raw-logs
 * page, occurrence counts, AI summaries, assign/ack/resolve flow.
 *
 * No npm deps. Requires Node 18+ (global fetch).
 *
 *   node seed-logs.js
 *
 * Override anything via env vars:
 *   BASE_URL=http://localhost:8080  API_KEY=<raw key from console -> Settings -> API keys>
 *   TOTAL=10000  CONCURRENCY=40  node seed-logs.js
 *
 * The gateway derives the tenant from the API key, so pass the real key of the
 * tenant whose dashboard you want to populate (do NOT invent a tenant id).
 *
 * How incidents form (must understand to read the output):
 *   - Gateway -> raw-logs -> normalization (fingerprint) -> clustering -> incident.
 *   - Fingerprint = SHA-256(serviceName | messageTemplate | exceptionType | firstStackFrame).
 *     normalization replaces numbers/IPs/UUIDs in the message with placeholders, so
 *     "timeout after 30000ms" and "timeout after 5s" collapse to the SAME incident.
 *     Distinct incidents therefore need a different service, message wording, or
 *     first stack frame.
 *   - Clustering threshold = 10 occurrences within a 5-minute window. Bursts below 10
 *     never become incidents (they still show on the raw-logs page).
 *   - Severity is derived from level: ERROR/FATAL -> HIGH, WARN -> MEDIUM, else LOW.
 *     (There is NO CRITICAL on the backend.)
 *   - Incidents appear ~5 min after seeding (window must close first). Raw logs show
 *     up immediately.
 *   - AI summaries only populate if ai-service is running with a valid LLM key; else the
 *     incident drawer polls forever with an empty summary.
 */

'use strict';

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------
const BASE_URL    = process.env.BASE_URL    || 'http://localhost:8080';
// Real API key of the target tenant. The gateway validates it and derives the
// tenant from it, so there is no tenant header. Create one in the console under
// Settings -> API keys.
const API_KEY     = process.env.API_KEY;
const TOTAL       = parseInt(process.env.TOTAL || '10000', 10);
const CONCURRENCY = parseInt(process.env.CONCURRENCY || '40', 10);
const JITTER_MS   = 60_000; // spread each log's timestamp up to 60s into the past (stays inside the 5-min window)

const INGEST_URL = `${BASE_URL.replace(/\/$/, '')}/api/v1/logs`;

// ---------------------------------------------------------------------------
// Curated scenarios - each entry becomes ONE incident (count >= 10) or a sub-threshold
// burst (count < 10, raw-logs only). Vary service / message / frame to keep them
// distinct. `count` controls occurrenceCount in the console.
// ---------------------------------------------------------------------------
const FATAL = 'FATAL', ERROR = 'ERROR', WARN = 'WARN', INFO = 'INFO', DEBUG = 'DEBUG';

const SCENARIOS = [
  // ---- HIGH severity (ERROR / FATAL) ----
  { service: 'payment-service', env: 'production', level: ERROR, count: 1500, // high occurrence count
    message: 'Connection timeout after 30000ms calling payment gateway',
    frame: 'com.log0.payment.PaymentProcessor.charge' },
  { service: 'payment-service', env: 'production', level: FATAL, count: 800,
    message: 'OutOfMemoryError: Java heap space during batch settlement',
    frame: 'com.log0.payment.PaymentProcessor.batchSettle' },
  { service: 'auth-service', env: 'production', level: ERROR, count: 420,
    message: 'JWT signature validation failed for incoming token',
    frame: 'com.log0.auth.JwtValidator.validate' },
  { service: 'order-service', env: 'production', level: ERROR, count: 350,
    message: 'Failed to reserve inventory for order, insufficient stock',
    frame: 'com.log0.order.OrderService.reserve' },
  { service: 'billing-service', env: 'production', level: ERROR, count: 95,
    message: 'Stripe API returned card_declined for charge',
    frame: 'com.log0.billing.BillingClient.capture' },
  { service: 'api-gateway', env: 'staging', level: ERROR, count: 60,
    message: 'Upstream service returned 503 Service Unavailable',
    frame: 'com.log0.gateway.ProxyHandler.forward' },
  { service: 'inventory-service', env: 'production', level: ERROR, count: 45,
    message: 'Deadlock detected on table inventory_items',
    frame: 'com.log0.inventory.InventoryDao.decrement' },
  { service: 'search-service', env: 'staging', level: ERROR, count: 30,
    message: 'Elasticsearch cluster health RED, shard unassigned',
    frame: 'com.log0.search.SearchIndexer.query' },
  { service: 'user-service', env: 'development', level: ERROR, count: 12, // edge case: just over threshold
    message: 'NullPointerException while mapping user profile',
    frame: 'com.log0.user.UserMapper.toDto' },

  // ---- MEDIUM severity (WARN) ----
  { service: 'payment-service', env: 'production', level: WARN, count: 200,
    message: 'Retry attempt 3 of 5 for payment gateway call',
    frame: 'com.log0.payment.PaymentRetry.attempt' },
  { service: 'api-gateway', env: 'production', level: WARN, count: 140,
    message: 'Request latency exceeded 2000ms threshold',
    frame: 'com.log0.gateway.LatencyMonitor.record' },
  { service: 'auth-service', env: 'production', level: WARN, count: 75,
    message: 'Login rate limit approaching for tenant',
    frame: 'com.log0.auth.RateLimiter.check' },
  { service: 'email-service', env: 'staging', level: WARN, count: 33,
    message: 'SMTP connection pool exhausted, queuing message',
    frame: 'com.log0.email.SmtpPool.acquire' },
  { service: 'inventory-service', env: 'development', level: WARN, count: 18,
    message: 'Stock level below reorder threshold',
    frame: 'com.log0.inventory.StockMonitor.evaluate' },

  // ---- LOW severity (INFO / DEBUG) ----
  { service: 'notification-service', env: 'production', level: INFO, count: 300,
    message: 'Slack notification delivered successfully',
    frame: null },
  { service: 'user-service', env: 'production', level: INFO, count: 250,
    message: 'User session created',
    frame: null },
  { service: 'order-service', env: 'staging', level: DEBUG, count: 40,
    message: 'Order state transition NEW to CONFIRMED',
    frame: null },
  { service: 'search-service', env: 'development', level: INFO, count: 15,
    message: 'Search index rebuild completed',
    frame: null },

  // ---- Sub-threshold bursts (count < 10): raw-logs only, no incident ----
  { service: 'billing-service', env: 'staging', level: ERROR, count: 6,
    message: 'Invoice PDF generation failed, retrying',
    frame: 'com.log0.billing.InvoiceRenderer.render' },
  { service: 'email-service', env: 'development', level: WARN, count: 4,
    message: 'Bounce received for recipient address',
    frame: null },
];

// Stack-trace builder - distinct first frame => distinct fingerprint.
function buildTrace(frame, message) {
  if (!frame) return null;
  const exType = /timeout/i.test(message) ? 'java.net.SocketTimeoutException'
              : /OutOfMemory/i.test(message) ? 'java.lang.OutOfMemoryError'
              : /NullPointer/i.test(message) ? 'java.lang.NullPointerException'
              : /Deadlock/i.test(message) ? 'org.postgresql.util.PSQLException'
              : 'java.lang.RuntimeException';
  const line = 40 + Math.floor((frame.length * 7) % 200); // deterministic-ish line no.
  return `${exType}: ${message}\n\tat ${frame}(${frame.split('.').slice(-2, -1)[0]}.java:${line})`
       + `\n\tat com.log0.common.Dispatcher.run(Dispatcher.java:88)`;
}

// ---------------------------------------------------------------------------
// Noise generator - fills raw-logs page to reach TOTAL. High message cardinality
// keeps most of it under the incident threshold.
// ---------------------------------------------------------------------------
const NOISE_SERVICES = ['payment-service', 'auth-service', 'api-gateway', 'order-service',
  'inventory-service', 'notification-service', 'user-service', 'search-service',
  'billing-service', 'email-service'];
const NOISE_ENVS = ['production', 'staging', 'development'];
const NOISE_PHRASES = [
  'Handled request', 'Cache hit for key', 'Cache miss for key', 'Published event to topic',
  'Consumed message from partition', 'Healthcheck OK', 'Scheduled job started',
  'Scheduled job finished', 'DB query executed', 'Acquired connection from pool',
  'Released connection to pool', 'Validated request payload', 'Serialized response',
  'Token refreshed', 'Feature flag evaluated', 'Webhook dispatched', 'Metric flushed',
  'Config reloaded', 'Background sync tick', 'Audit record written',
];
// Most noise is INFO/DEBUG (LOW); a sprinkle of WARN for variety.
const NOISE_LEVELS = [INFO, INFO, INFO, DEBUG, DEBUG, WARN];

let seedCounter = 0;
function nextRand() {
  // simple LCG so output is reproducible without Date.now()/Math.random pitfalls
  seedCounter = (seedCounter * 1103515245 + 12345) & 0x7fffffff;
  return seedCounter / 0x7fffffff;
}
function pick(arr) { return arr[Math.floor(nextRand() * arr.length)]; }

// Letters-only unique token. Normalization strips numbers/IPs/UUIDs from the message
// template but NOT letters, so a letter token keeps every noise message in its own
// template -> noise never crosses the incident threshold -> noise stays raw-logs-only.
// (A numeric "id=123" would collapse to "id=<number>" and cluster into a junk incident.)
let noiseSeq = 0;
function uniqueToken() {
  let n = noiseSeq++;
  let s = '';
  do { s = String.fromCharCode(97 + (n % 26)) + s; n = Math.floor(n / 26) - 1; } while (n >= 0);
  return s;
}

function makeNoiseLog() {
  const phrase = pick(NOISE_PHRASES);
  const level = pick(NOISE_LEVELS);
  return {
    service: pick(NOISE_SERVICES),
    env: pick(NOISE_ENVS),
    level,
    message: `${phrase} ref-${uniqueToken()}`,
    trace: null,
  };
}

// ---------------------------------------------------------------------------
// Build the flat task list
// ---------------------------------------------------------------------------
function severityOf(level) {
  if (level === ERROR || level === FATAL) return 'HIGH';
  if (level === WARN) return 'MEDIUM';
  return 'LOW';
}

const tasks = [];
for (const s of SCENARIOS) {
  const trace = buildTrace(s.frame, s.message);
  for (let i = 0; i < s.count; i++) {
    tasks.push({ service: s.service, env: s.env, level: s.level, message: s.message, trace });
  }
}
const curated = tasks.length;
const noiseCount = Math.max(0, TOTAL - curated);
for (let i = 0; i < noiseCount; i++) tasks.push(makeNoiseLog());

// ---------------------------------------------------------------------------
// Send
// ---------------------------------------------------------------------------
function nowJittered() {
  // timestamp up to JITTER_MS in the past - keeps the burst inside one clustering window
  return new Date(Date.now() - Math.floor(nextRand() * JITTER_MS)).toISOString();
}

async function postLog(t) {
  const res = await fetch(INGEST_URL, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-SERVICE-NAME': t.service,
      'X-ENVIRONMENT': t.env,
      'X-API-KEY': API_KEY,
    },
    body: JSON.stringify({
      timestamp: nowJittered(),
      level: t.level,
      message: t.message,
      trace: t.trace || undefined,
    }),
  });
  if (res.status !== 202) {
    const body = await res.text().catch(() => '');
    throw new Error(`HTTP ${res.status} ${body.slice(0, 160)}`);
  }
}

async function runPool(items, concurrency, worker) {
  let idx = 0, done = 0;
  const stats = { ok: 0, fail: 0 };
  const errSamples = [];
  async function lane() {
    while (idx < items.length) {
      const i = idx++;
      try { await worker(items[i]); stats.ok++; }
      catch (e) { stats.fail++; if (errSamples.length < 8) errSamples.push(e.message); }
      if (++done % 1000 === 0) process.stdout.write(`  sent ${done}/${items.length}\n`);
    }
  }
  await Promise.all(Array.from({ length: concurrency }, lane));
  return { stats, errSamples };
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------
(async () => {
  if (!API_KEY) {
    console.error('seed-logs: set API_KEY=<raw key> (console -> Settings -> API keys)');
    process.exit(1);
  }
  console.log('log0 seed-logs');
  console.log(`  target   : ${INGEST_URL}`);
  console.log(`  total    : ${tasks.length} logs (${curated} curated + ${noiseCount} noise), concurrency ${CONCURRENCY}`);
  console.log('');
  console.log('Expected incidents (after the ~5-min clustering window closes):');
  for (const s of SCENARIOS) {
    if (s.count < 10) continue; // sub-threshold, no incident
    console.log(`  [${severityOf(s.level).padEnd(6)}] ${s.service.padEnd(20)} ${String(s.env).padEnd(12)} x${String(s.count).padStart(4)}  ${s.message}`);
  }
  const subThreshold = SCENARIOS.filter(s => s.count < 10);
  if (subThreshold.length) {
    console.log('  (sub-threshold bursts -> raw-logs page only, no incident: '
      + subThreshold.map(s => `${s.service}/${s.count}`).join(', ') + ')');
  }
  console.log('');

  const t0 = Date.now();
  const { stats, errSamples } = await runPool(tasks, CONCURRENCY, postLog);
  const secs = ((Date.now() - t0) / 1000).toFixed(1);

  console.log('');
  console.log(`Done in ${secs}s  ->  ok: ${stats.ok}, failed: ${stats.fail}`);
  if (errSamples.length) {
    console.log('Sample errors:');
    for (const e of errSamples) console.log('  ' + e);
  }
  console.log('');
  console.log('Next:');
  console.log('  - Raw logs are queryable now (logs page).');
  console.log('  - Incidents appear after the 5-min window closes; refresh the incidents page.');
  console.log('  - Pick a NEW incident in the console to exercise assign -> acknowledge -> resolve.');
  console.log('  - AI summaries only fill if ai-service is up with a valid LLM key.');
  if (stats.fail > 0) process.exitCode = 1;
})();

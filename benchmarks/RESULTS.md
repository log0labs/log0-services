# log0 benchmark results

Single-host load characterization of the log0 pipeline. These are NOT production-scale
numbers; they describe how one laptop running the full Docker stack (7 JVM services +
Redpanda + PostgreSQL + ClickHouse) behaves under load. Reported so the blogs can cite
measured data instead of design targets.

## Environment
- One laptop, Docker Desktop. Each service capped at mem_limit 512m (heap ~70%).
- Broker: Redpanda v24.2.7 (single node, --smp=1). PostgreSQL 16. ClickHouse 24.3.
- Load tool: k6 (constant-VUs), driving POST /api/v1/logs on the gateway at localhost:8080
  (measures the backend directly, not through the Vercel/Cloudflare path).
- Harness: benchmarks/k6-ingest.js, collect.sh (snapshots), run-ingest.sh (orchestration).

## Idle baseline (no load)
| Container | CPU | Mem |
|---|---|---|
| clickhouse | ~0% | 1.16 GiB |
| redpanda | ~1.5% | 978 MiB |
| each JVM service (x7) | <2% | 170-295 MiB |
| postgres | ~0% | 70 MiB |

Total idle footprint ~3.6 GiB. (Data point for the Kafka-vs-Redpanda post: the broker
sits under 1 GiB with no Zookeeper JVM alongside it.)

## Bug found by the test (before any numbers)
The first POST hung indefinitely. Root cause: ingestion-gateway's KafkaProducerConfig had
a hardcoded `localhost:9092` broker, so inside the container the producer could never reach
Redpanda (`redpanda:9092`) and blocked on `max.block.ms` fetching metadata. The entire
ingestion path was non-functional in Docker and had only ever been health-checked, never
exercised end to end. Fixed to read `spring.kafka.bootstrap-servers` (same class of bug as
clustering/normalization). After the fix: POST returns 202 in ~15 ms warm. Load testing
surfaced a real outage-class bug that unit tests and health checks missed.

## Run: load100 (100 VUs, 60s, 10 distinct error templates, 1 tenant)

### Ingestion (the gateway)
| Metric | Value |
|---|---|
| Requests | 181,995 in 60s |
| Throughput | 3,032 req/s sustained |
| Success | 100% 202 Accepted, 0 errors |
| Accept latency | avg 32.8 ms, p50 25.9 ms, p95 90 ms, p99 156 ms, max 599 ms |
| Gateway CPU / mem during load | 0.03% / 294 MiB |

Takeaway: the gateway is not the limit. It accepts a log, publishes to Redpanda, and
returns 202 without touching the rest of the pipeline. At 3k req/s it is nearly idle. This
is the "accept fast, never block" design working exactly as intended.

### The bottleneck (normalization + ClickHouse)
| Signal | Value |
|---|---|
| raw-logs backlog (peak) | 180,953 messages |
| normalization throughput | ~30-46 logs/sec |
| ClickHouse CPU under load | 171% (pegged) |
| ClickHouse mem | 1.33 GiB |
| normalization CPU | ~25% (blocked on ClickHouse, not CPU-bound) |

The gateway produced 3,032/s; normalization drained ~40/s. A ~70x mismatch, so raw-logs
backed up to ~181k. Root cause: normalization performs ONE synchronous single-row INSERT
into ClickHouse per event. ClickHouse is a columnar OLAP store optimized for batched
inserts; hammering it with tiny single-row INSERTs pegs it at 171% CPU for ~40 rows/sec.
This is the textbook "ClickHouse hates single-row inserts" failure, observed live.

The system degrades safely: the gateway and Redpanda absorb the burst, the backlog is
durable in the log, and the consumer drains it slowly. Nothing is dropped (raw-logs-dlq = 0).
This is backpressure working - the queue is the shock absorber.

### Dedup (fingerprinting + clustering)
- 181,998 logs carrying 10 distinct error templates collapsed to 10 incidents.
- Effective dedup ratio at full drain: ~18,200 logs : 1 incident.
- incident-events = 10, notification-events = 10, raw-logs-dlq = 0.
- Clustering kept lag ~0 (it only ever saw the slow trickle of normalized-logs).

## Blog mapping (what each post can now cite)
- 01 fingerprinting / 10 clustering: the 18,200:1 dedup, 10 templates -> 10 incidents.
- 02 pipeline / 03 ingestion: 3,032 req/s at p99 156 ms, gateway at 0.03% CPU, accept-fast proven.
- 06 ack + DLQ / backpressure: 181k durable backlog, 0 drops, slow safe drain.
- 08 normalization / 09 ClickHouse: the single-row-insert bottleneck, ClickHouse 171% CPU, ~40 rows/sec.
- 05 Redpanda: idle and under-load footprint (<1.1 GiB).

## Run: ingestion ceiling ramp (gateway-only metrics)

| VUs | Throughput | p50 | p95 | p99 | max | 202 success |
|---|---|---|---|---|---|---|
| 100 | 3,032 req/s | 26 ms | 90 ms | 156 ms | 599 ms | 100% |
| 200 | 3,424 req/s | 48 ms | 150 ms | 197 ms | 361 ms | 100% |
| 400 | 3,523 req/s | 68 ms | 303 ms | 424 ms | 2477 ms | 85.1% |

The gateway tops out near 3,500 req/s on this laptop. The knee is around 200 VUs: beyond it
throughput plateaus while latency climbs, and at 400 VUs ~15% of requests stop getting a 202.
The cause is backpressure reaching the front door: raw-logs is so far behind (the ~40/s
consumer) that the producer's send buffer (buffer.memory, default 32 MB) fills, and new sends
block past max.block.ms and fail. "Accept fast, never block" holds until the buffer saturates;
then the gateway correctly pushes back instead of pretending. A real, visible limit.

## Optimization: batched ClickHouse inserts (before / after)

Change: normalization-service stopped doing one INSERT per event. `LogEventRepository.save`
now buffers events and flushes them as a single `executeBatch()` when the buffer fills
(batch-size 500) or on a 1s interval, with a flush on shutdown. Same load100 test (100 VUs,
60s), identical hardware.

| Metric | Before (single-row INSERT) | After (batched) |
|---|---|---|
| Normalization throughput | ~40 rows/sec | keeps up at 4,185 rows/sec, lag = 0 |
| Peak raw-logs backlog @ 100 VUs | 180,953 | 0 |
| ClickHouse CPU under load | 171% (pegged) | 8-18% |
| Ingest throughput (same run) | 3,032 req/s | 4,185 req/s |
| Ingest latency p50 / p99 | 26 / 156 ms | 20 / 80 ms |
| Drain of a 300k backlog | ~40/sec, did not clear in 45s | cleared in under 30s |
| Events persisted (batched run) | n/a | 251,165 of 251,165, real time, 0 loss |

A ~100x consumer throughput jump from one change. Two second-order effects worth noting:
1. ClickHouse CPU fell from 171% to ~15%, which freed the shared host, which is why the
   gateway's own throughput rose (3,032 -> 4,185 req/s) and p99 halved on the same VUs.
2. Backlog went to zero: normalization now matches ingestion in real time at this load, so
   the queue stops being a buffer-of-last-resort and just passes through.

The lesson is the OLAP one stated plainly: a columnar store rewards batching and punishes
single-row inserts. The fix was not more hardware; it was inserting in batches.

## Open / next runs
- Batch-insert optimization for ClickHouse, then re-run (before/after throughput) - highest-value follow-up.
- DLQ injection, multi-tenant skew, auth throughput, AI callback latency.

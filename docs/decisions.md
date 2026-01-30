
# log0 Key Design Decisions

This document captures important architectural and technical decisions made during the design and development of log0.

## 1. Event-Driven Architecture
- **Decision:** Use Kafka to decouple services and handle high-throughput logs.
- **Rationale:** Allows independent scaling of ingestion, normalization, clustering, and incident services.
- **Alternative considered:** Synchronous REST calls between services.
- **Reason rejected:** Would create tight coupling and potential bottlenecks.

## 2. Storage Choices
- **Decision:** ClickHouse for logs, PostgreSQL for incidents.
- **Rationale:** ClickHouse is optimized for high-volume, analytical queries; PostgreSQL provides transactional guarantees for incident lifecycle.
- **Alternative considered:** Using PostgreSQL for everything.
- **Reason rejected:** Inefficient for storing millions of raw logs.

## 3. Monorepo Structure
- **Decision:** Keep all services in a single repository.
- **Rationale:** Simplifies local development, shared models, and CI/CD.
- **Alternative considered:** Multi-repo per service.
- **Reason rejected:** Overhead for solo developer; premature optimization.

## 4. Minimal Dependencies Initially
- **Decision:** Start services with only necessary dependencies (Spring Web, Kafka, Validation, Actuator, Lombok).
- **Rationale:** Keeps services lightweight and reduces unnecessary complexity.
- **Future:** Additional dependencies (DB, Slack API clients, AI modules) will be added as features develop.

## 5. Logging & Observability
- **Decision:** Use Spring Boot Actuator and structured logs for all services.
- **Rationale:** Ensures basic monitoring and health checks are available from day one.

## 6. Multi-Tenant Support
- **Decision:** All services designed to include `tenant_id` in logs and incidents.
- **Rationale:** Enables SaaS deployment with strong tenant isolation.

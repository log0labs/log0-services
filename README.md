# log0

**log0 — an intelligent incident copilot that turns raw logs into actionable incidents.**

> From logs to incidents, automatically.

---

## Overview

**log0** is a backend-first, multi-tenant log intelligence and incident management platform.

It ingests high-volume logs from distributed services, normalizes and clusters similar errors, and automatically creates incidents when recurring failures are detected. Using an event-driven architecture built on **Kafka**, **ClickHouse**, and **PostgreSQL**, log0 provides reliable log ingestion, real-time incident detection, and ownership-driven workflows.

Incidents are summarized and routed directly to **Slack** or **WhatsApp**, enabling fast assignment and resolution without leaving existing tools. The platform is designed as a scalable **SaaS**, with clear service boundaries, strong tenant isolation, and an extensible foundation for future AI-assisted debugging and code-level recommendations.

---

## Architecture (High Level)

- Event-driven, microservice-based backend
- Multi-tenant by design
- Asynchronous processing using Kafka
- Optimized log storage using ClickHouse
- Strong incident lifecycle management

> Detailed architecture diagrams and design decisions are documented in `/docs`.

---

## Repository Structure

```text
log0-platform/
├── services/
│   ├── ingestion-gateway/
│   ├── normalization-service/
│   ├── incident-service/
│   ├── notification-service/
│
├── shared/          # shared libraries (introduced gradually)
├── infra/           # Docker, Kafka, ClickHouse configs
├── docs/            # architecture & design docs
└── README.md

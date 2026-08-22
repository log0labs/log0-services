# log0

**log0 - an intelligent incident copilot that turns raw logs into actionable incidents.**

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

## License

log0 is **open source** under an **open-core** model:

- The **community edition** (everything outside the `ee/` directory) is licensed
  under the **GNU AGPL-3.0** - see [`LICENSE`](./LICENSE). You may self-host,
  modify, and redistribute it. If you run a modified version as a network
  service, AGPL-3.0 requires you to make your modified source available to its
  users.
- The **Enterprise Edition** (the `ee/` directory) is **commercial, proprietary**
  software - see [`ee/LICENSE`](./ee/LICENSE). It requires a commercial license
  from log0labs.

Copyright (c) 2026 log0labs and the log0 contributors.

Need a commercial/OEM license without AGPL obligations? Contact
ashmitgupta.official@outlook.com.

## Contributing

Contributions are welcome - see [`CONTRIBUTING.md`](./CONTRIBUTING.md) for setup,
project layout, and workflow, and [Running Locally](https://log0.in/docs/local-development)
for the full dev guide. Please also review our
[Code of Conduct](./CODE_OF_CONDUCT.md). Because log0 is dual-licensed (AGPL + commercial), all
contributors must agree to the [Contributor License Agreement](./CLA.md) before
their pull request can be merged (handled automatically via a CLA check on PRs).
Security issues: please follow [`SECURITY.md`](./SECURITY.md).

"log0" and "log0labs" are trademarks of log0labs; the license covers the code,
not the brand.

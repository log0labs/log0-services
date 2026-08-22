# Contributing to log0

Thanks for your interest in contributing to **log0** - an intelligent incident
copilot that turns raw logs into actionable incidents. This guide covers how to
set up your environment, the project layout, and how to get a change merged.

Full product and architecture documentation lives at **https://log0.in/docs**.

---

## License & CLA (read first)

log0 is **open source** under an **open-core** model:

- The **community edition** (everything outside the `ee/` directory) is licensed
  under **GNU AGPL-3.0** - see [`LICENSE`](./LICENSE).
- The **Enterprise Edition** (`ee/`) is commercial and proprietary - see
  [`ee/LICENSE`](./ee/LICENSE).

Because log0 is dual-licensed, **every contributor must agree to the
[Contributor License Agreement](./CLA.md)** before a pull request can be merged.
This is enforced automatically by a CLA check on your first PR - just follow the
bot's link and confirm. By contributing you also agree to license your work under
AGPL-3.0 and allow log0labs to relicense it commercially, as described in the CLA.

Please also read and follow our [Code of Conduct](./CODE_OF_CONDUCT.md), and keep
all interactions respectful and constructive.

---

## Prerequisites

See [Running Locally](https://log0.in/docs/local-development) for the full guide.
In short, you need:

| Tool | Version | Notes |
|---|---|---|
| **Java** | 25+ | `java -version` to verify |
| **Maven** | 3.9.12+ | Bundled via the `mvnw`/`mvnw.cmd` wrapper - no separate install |
| **Docker Desktop** | Latest | Runs Redpanda, PostgreSQL, and ClickHouse |
| **Git** | Any | |

Code targets **Java 25 / Spring Boot 4**.

---

## Getting the stack running

The fastest path is the unified Docker stack (all seven services plus infra):

```bash
cd docker
docker compose up -d --build
```

For iterative development you'll usually run **infrastructure in Docker** and the
**service you're working on from source**. Each service ships a helper that loads
its `.env` and starts it via the Maven wrapper:

```bash
# Linux/macOS
cd services/<service-name>
./run.sh

# Windows PowerShell
cd services\<service-name>
.\run.ps1
```

Environment variables (Slack, LLM keys, `JWT_SECRET`) and health checks are all
documented in [Running Locally](https://log0.in/docs/local-development).

---

## Repository layout

```
services/           The seven Spring Boot microservices:
  ingestion-gateway/       receives logs from clients (8080)
  normalization-service/   parses, fingerprints, stores to ClickHouse (8081)
  clustering-service/      counts occurrences, triggers incidents (8082)
  incident-service/        incident storage + REST API (8083)
  notification-service/    Slack alerts (8084)
  ai-service/              LLM summaries (8085)
  auth-service/            tenant registration, JWT, API keys (8086)
docker/             docker-compose stack + shared Dockerfile
benchmarks/         load/throughput harness
docs/               engineering docs (design, plans)
tools/              helper scripts
ee/                 Enterprise Edition - PROPRIETARY, not AGPL
```

Background reading before a non-trivial change:

- [Architecture overview](https://log0.in/docs/architecture)
- [Flow diagrams](https://log0.in/docs/architecture/flow-diagrams) - runtime behavior
- [Service internals (LLD/UML)](https://log0.in/docs/architecture/lld-uml)
- [Architecture decisions](https://log0.in/docs/architecture/decisions) - why things are the way they are
- [Roadmap](https://log0.in/docs/roadmap)

---

## The `ee/` boundary (important)

- **Do not** add community features inside `ee/` - keep them under AGPL.
- **Do not** move or reimplement paid/enterprise features outside `ee/`.
- A community-edition build must remain fully functional without anything in `ee/`.

If you're unsure which side a feature belongs on, open an issue and ask before
writing code.

---

## Development workflow

1. **Find or open an issue.** For anything beyond a small fix, open an issue first
   so we can agree on the approach. Look for issues labeled `good first issue`.
2. **Fork and branch.** Create a topic branch from `main`:
   `git checkout -b fix/incident-tenant-scope` or `feat/redis-occurrence-store`.
3. **Make focused changes.** Keep each PR scoped to one logical change. Match the
   existing code style (Spring Boot conventions, Lombok where already used,
   constructor injection, package-by-feature within a service).
4. **Add tests.** Cover new behavior and regressions. Run the affected service's
   tests:
   ```bash
   cd services/<service-name>
   ./mvnw test        # .\mvnw.cmd test on Windows
   ```
5. **Verify it builds** and runs locally against the Docker infra (see above).
6. **Write clear commits.** Use short, imperative subjects, e.g.
   `fix(incident-service): derive tenant from JWT, not request`. Conventional
   Commit prefixes (`feat`, `fix`, `docs`, `refactor`, `test`, `chore`) are
   encouraged.
7. **Open a pull request** against `main`. Fill in the description: what changed,
   why, how you tested it, and link the issue (`Closes #123`).
8. **Sign the CLA** when the bot prompts you (first PR only).

---

## Pull request checklist

- [ ] Change is focused and the PR description explains the "why".
- [ ] New/changed behavior is covered by tests; `./mvnw test` passes.
- [ ] No secrets, tokens, or real `.env` values are committed.
- [ ] Community code stays outside `ee/`; enterprise code stays inside `ee/`.
- [ ] Public API / event-schema changes are noted in the PR (and docs updated if
      user-facing).
- [ ] You have agreed to the [CLA](./CLA.md).

---

## Reporting security issues

**Do not open a public issue for security vulnerabilities.** log0 is a
multi-tenant security product - please report privately per
[`SECURITY.md`](./SECURITY.md) (or email ashmitgupta.official@outlook.com) so it can be fixed
before disclosure.

---

## Questions

- Product & architecture docs: **https://log0.in/docs**
- General questions: open a GitHub Discussion or issue.
- Commercial/enterprise licensing: ashmitgupta.official@outlook.com

We appreciate every contribution - from typo fixes to new services. Thank you for
helping make log0 better.

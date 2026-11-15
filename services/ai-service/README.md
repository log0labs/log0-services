# log0 AI service

The part of log0 that explains incidents. It writes the short summary of a new incident, and, on request, runs an
**investigation**: it gathers evidence from logs, incident history, your code and your runbooks, checks competing
explanations against that evidence, states a root cause that cites its sources, and proposes a fix as a diff that is
guaranteed to apply.

Python 3.13, FastAPI, LangGraph. About 12,800 lines of application code and 18,000 of tests and evaluation code;
1,416 automated tests.

```
 console ──► /api/v1/investigations ─► runner ─► LangGraph ──┬─► ClickHouse   (logs, read-only user)
 incident-service ─► /api/v1/summaries                       ├─► Postgres     (incidents read-only, ai schema, pgvector)
 Claude Desktop / Cursor ─► MCP (stdio or /mcp)              ├─► git + ripgrep (a tenant's repository, pinned commit)
                                                             └─► an LLM provider (Groq, OpenAI, Anthropic, Google, HF, OpenRouter)
```

## The investigation graph

```
load_context ─► triage ─► [ gather_logs | gather_history | gather_code | search_runbooks ] (in parallel)
   ─► synthesize ─► verify ─► (more log searches? loop) ─► root_cause ─► human_review
   ─► propose_changes ─► validate_changes ─► (retry, at most twice) ─► finalize
```

The principle is **the model proposes, code decides**. Everything the model says is checked: a root cause can cite only
evidence ids that exist in the run, a hypothesis counts as supported only if its cited evidence exists, the confidence
band comes from a deterministic rule (not the model's number), a file can be cited only if a tool returned it, and a
fix is built and validated by code. Every model step has a deterministic fallback, and what could not be found out is
reported as a **gap**. Every run has hard limits on tool calls, rounds and time, and survives a restart (Postgres
checkpoints).

| Step | What it does |
|---|---|
| `load_context` | The incident, its parsed stack trace (Java, Python, Node, Go, Rust, C#, PHP and more), basic log statistics |
| `triage` | A cheap model picks the likely category and which sources to search; code enforces the user's scope |
| `gather_logs` | Error timeline, co-occurring errors across services, sample lines |
| `gather_history` | Earlier incidents (same fingerprint, same service) and similar past investigations (memory) |
| `gather_code` | A tool-using sub-graph over `git` and `ripgrep`: list, search, read, recent commits, blame, show commit |
| `search_runbooks` | Retrieve (pgvector), pick diverse results (MMR), grade relevance, rewrite the query once |
| `synthesize`, `verify` | Two to four hypotheses with cited evidence; judged against the evidence; targeted follow-up searches |
| `root_cause` | The statement, the evidence, a recommendation, a deterministic confidence band |
| `human_review` | Optional pause: approve, refine with a hint, or stop |
| `propose_changes`, `validate_changes` | Exact edits from the model; code builds the diff and runs `git apply --check` |

## Run it

```bash
cd services/ai-service
uv sync
cp .env.example .env            # see "Configuration"
uv run alembic upgrade head     # the ai schema, pgvector, runbooks, memory
uv run uvicorn app.main:app --port 8085
```

With the rest of log0: `docker compose up -d --build ai-service` in `docker/` (the Postgres image must be
`pgvector/pgvector:pg16`). Health: `GET /health`.

## Configuration

Required: `DATABASE_URL`, `JWT_SECRET` (the same as auth-service), `INTERNAL_SERVICE_TOKEN`, and an API key for each
configured provider. The ones you are likely to touch:

| Setting | Meaning |
|---|---|
| `LLM_SUMMARY`, `LLM_TRIAGE`, `LLM_INVESTIGATION`, `LLM_EVALUATION` | `provider:model` per kind of work; each has its own structured-output method |
| `GROQ_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `HF_TOKEN`, `OPENROUTER_API_KEY` | Platform keys (a tenant can bring its own, stored encrypted) |
| `SECRETS_KEYS`, `SECRETS_ACTIVE_KEY_ID` | Master keys that encrypt tenant keys and repository tokens (`scripts/new_secret_key.py`) |
| `INVESTIGATION_REVIEW_GATE` | `true` pauses every investigation for an engineer's decision. Off: a root cause is approved automatically when code is connected |
| `EMBEDDING_PROVIDER` | `hash` (built in, shared words) or `huggingface` (semantic; runbook text leaves the service) |
| `MCP_HTTP_ENABLED` | Serve the MCP tools at `/mcp` |
| `LANGCHAIN_TRACING_V2`, `LANGCHAIN_API_KEY`, `TRACING_OPT_IN_TENANTS` | LangSmith; bring-your-own-key tenants are traced only if listed |
| `LLM_PRICES` | USD per million tokens per model, so cost is computed; unpriced models report no cost |

**Choosing a model.** Investigations need tool calling and structured JSON, and make 10 to 20 calls with 5 to 10k tokens each.
On Groq's free tier `gpt-oss-120b` runs out of its 200k tokens a day within one evaluation pass and keeps calling an invented
`apply_patch` tool; `gpt-oss-20b` cannot do structured output reliably. What works on a free tier is **Gemini 3.5 Flash Lite**
(`LLM_INVESTIGATION=google_genai:gemini-3.5-flash-lite`, `LLM_INVESTIGATION_OUTPUT_METHOD=function_calling`): about 15
requests a minute and 500 a day. The other Gemini Flash models allow only 20 requests a day. The service waits out a short
per-minute rate limit as long as the provider asks (up to three times) but never a daily cap. Free-tier prompts may be used by
the provider for training: use only synthetic data. After editing `.env`, recreate the container
(`docker compose up -d --force-recreate ai-service`); a plain `up -d` does not re-read it.

**Per-workspace models.** A workspace's saved choice (model, key, temperature, token limit; Settings, AI) now drives its own
investigations. Purposes it did not set use the platform model, and a provider without a saved key uses the platform key.

## API

All under JWT auth (the tenant comes from the token). ADMIN and ENGINEER can start, cancel and resume; VIEWER reads.

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/summaries` | The incident summary (202, called by incident-service) |
| `POST /api/v1/investigations`, `GET .../{id}`, `GET /api/v1/investigations?incidentId=` | Start, read, list. 409 if one is already active for the incident |
| `GET .../{id}/stream`, `GET .../{id}/events?after=` | Server-Sent Events from a persisted event log; resume with `Last-Event-ID` |
| `POST .../{id}/cancel`, `POST .../{id}/resume` | Cancel; answer the review gate |
| `POST .../{id}/feedback` | Helpful, partly or wrong: feeds memory |
| `GET /api/v1/investigations/latest?incidentIds=a,b` | The newest investigation of each listed incident (at most 100), for the incident list's AI column |
| `POST .../{id}/pr/approve`, `POST .../{id}/pr/decline`, `GET .../{id}/pull-request` | Answer the draft pull request gate (ADMIN or ENGINEER); read the pull request |
| `GET /api/v1/github/app`, `POST/GET/DELETE /api/v1/github/installations`, `GET .../{id}/repositories`, `PUT /api/v1/github/repositories/{id}/writes` | Install and link the GitHub App, choose writable repositories (ADMIN) |
| `GET /api/v1/github/audit` | Every write log0 made (ADMIN) |
| `GET/PUT/DELETE /api/v1/github/policy` | The workspace's pull request rules, stricter than the server's limits (ADMIN) |
| `POST /api/v1/github/webhook` | GitHub's signed events: uninstall, suspend, repositories removed |
| `GET/PUT/DELETE /api/v1/ai-settings/{purpose}`, `POST .../test` | Per-tenant model settings (ADMIN) |
| `POST/GET/DELETE /api/v1/repositories`, `/api/v1/service-repositories` | Connect GitHub repositories, map services (ADMIN) |
| `POST/GET/DELETE /api/v1/runbooks` | Runbooks (ADMIN to change). Upload JSON is `{title, markdown, format}` (`markdown` or `text`) or `{title, fileBase64, fileType}` (`pdf` or `docx`, up to 5 MB); PDF and Word text is extracted and indexed |
| `GET /api/v1/runbooks/{id}`, `GET .../{id}/file` | The runbook as uploaded (text, kind) and its original PDF or Word file |

## Draft pull requests (V2)

`validate_changes -> offer_pr -> approve_pr -> create_branch -> apply_patch -> run_tests -> open_draft_pr -> finalize`. `offer_pr` decides once whether a
pull request can be offered (kill switches, the policy of `publishing/policy.py`: protected files, size, rate) and stores the offer;
`approve_pr` is a human gate (`interrupt`); the three write nodes are idempotent and run only for an approval that names a user; `apply_patch`
rebuilds the commit from the validated changes and refuses unless it equals the reviewed diff; `run_tests` waits for the repository's own CI
(log0 runs no customer code). The graph sees only the `Publisher` port; `GitHubPublisher` re-checks every switch before every write and audits it.
Design: `log0-refs/ai-service/V2-PLAN.md`, `v2-graph-diagram.md`.

| Variable | Meaning |
|---|---|
| `GITHUB_APP_ID`, `GITHUB_APP_SLUG`, `GITHUB_APP_PRIVATE_KEY`, `GITHUB_WEBHOOK_SECRET` | The log0 GitHub App (permissions: contents write, pull requests write, checks read, metadata read; setup URL `<console>/settings/repositories`) |
| `PR_WRITES_ENABLED` | Global kill switch, off by default |
| `PR_WRITES_DISABLED_TENANTS` | JSON list of workspace ids switched off by the operator |
| `PR_ONLY_ON_GREEN` | Do not open a pull request when the checks failed or timed out |
| `CI_REPAIR_ATTEMPTS` | How many times log0 may correct its change after the repository's checks fail (1; 0 switches it off). Needs the App permission `actions: read` |
| `CI_WAIT_SECONDS`, `CI_POLL_SECONDS`, `CI_START_GRACE_SECONDS` | How long to wait for the repository's checks (600, 15, 60) |
| (GitHub App events) | Subscribe the App to **Pull request** so merges and closes of log0 pull requests are recorded (`pr.merged`, `pr.closed`, `pr.open` events). `installation` events need no subscription |
| `SANDBOX_ENABLED`, `SANDBOX_DOCKER_HOST`, `SANDBOX_RUNTIME` | Optional test sandbox for repositories without CI (off by default; a workspace must also opt in). Use a dedicated runner daemon, never the host Docker socket; `SANDBOX_RUNTIME=runsc` for gVisor. Build with `--build-arg INSTALL_DOCKER_CLI=true`. Notebook: `coding-notepads/v2/step-9/9.4-isolated-test-runner.md` |
| `SANDBOX_PREPARE_NETWORK`, `SANDBOX_PREPARE_PROXY`, `SANDBOX_PREPARE_OPEN` | The dependency-fetch step's egress-restricted network and proxy (none: the step is skipped); `OPEN` is for development only |
| `SANDBOX_IMAGES`, `SANDBOX_MEMORY`, `SANDBOX_CPUS`, `SANDBOX_PIDS`, `SANDBOX_TEST_TIMEOUT_SECONDS`, `SANDBOX_PREPARE_TIMEOUT_SECONDS`, `SANDBOX_MAX_WORKDIR_MB` | Pinned images (JSON) and the limits (1g, 1.0, 256, 300, 300, 700) |
| `PR_MAX_FILES`, `PR_MAX_CHANGED_LINES`, `PR_MAX_PER_REPOSITORY_PER_HOUR`, `PR_MAX_PER_TENANT_PER_HOUR` | Size and rate limits (10, 400, 3, 10) |

## MCP

Ten read-only tools over the same functions the graph uses: `get_incident`, `get_error_timeline`,
`get_cooccurring_errors`, `search_logs`, `find_related_incidents`, `search_runbooks`, `get_investigation`,
`search_code`, `read_file`, `recent_commits`. The tenant is never an argument.

```bash
uv run python scripts/mcp_token.py <tenant-id>          # a short-lived token for local use
LOG0_MCP_TOKEN=<token> uv run python -m app.mcp.stdio   # stdio, for Claude Desktop or Cursor
```

## Security

- **Tenancy:** the tenant id comes from the verified JWT and is in the `WHERE` clause of every query, including
  ClickHouse (bound parameters) and pgvector searches; tests, including mutation checks, prove it.
- **Redaction:** keys, tokens, passwords in connection strings, e-mail addresses and card numbers are replaced with
  `[REDACTED:kind]` when evidence is created, when tool output is read by a model, and on MCP output
  (`app/security/redaction.py`). Pattern based: not a guarantee.
- **Prompt injection:** log and code text is fenced as data; a canary case in the evaluation plants an injection in a log.
- **Code access:** read-only tools, secret-looking files refused, paths that leave the checkout refused, the token
  injected only for one git command.
- **Secrets at rest:** AES-based encryption with rotating master keys; only the last four characters are ever returned.

## Evaluation

```bash
uv run python -m evals.run                      # harness self-test: scripted model, offline, free
uv run python -m evals.run --mode real --yes    # the configured models on the golden set (sends data to the provider)
uv run python -m evals.run --mode real --yes --compare groq:openai/gpt-oss-120b,openai:gpt-4.1 --judge --run-tests
```

The golden set is eight planted bugs in a Node, Go and Rust demo shop
([ashmitjsg/log0-demo-shop](https://github.com/ashmitjsg/log0-demo-shop), private) plus a prompt-injection case.
Incidents and the rows the tools read are recorded from a real run and replayed offline through the production nodes.
Metrics: triage routing, trace localization (no model), file localization hit@1/3, grounding, root cause (keywords or a
model judge), fix pattern, diff validity, the service's tests passing with the fix applied, no false fix for a
non-code cause, no forbidden output or secret in the answer, calibration, latency, tokens and cost, and runbook retrieval
precision and recall. A baseline file gates regressions. Read `evals/run.py` for every option and
ADR-017 in the website docs (architecture, decisions) for the reasoning.

> The scripted model that CI uses scores the harness, not log0. Quality numbers come only from a complete real-model run (below).

### Measured so far

The first **complete** real-model run, 2026-11-15: all nine cases, the baseline committed as `evals/baseline.json`.
Investigation, triage and judge all ran on **Gemini 3.5 Flash Lite** (free tier); no case was degraded by a provider
failure (`model_available` 1.00). 183 tool calls, p50 33 s and p95 65 s per case, 335k input and 13k output tokens; cost
is not priced (free tier).

| Metric | Result | Of |
|---|---|---|
| Root cause judged correct | **7 of 9** (0.78) | all cases |
| Right file named first (hit@1, also hit@3) | 6 of 8 (0.75) | cases with a code cause |
| Fix pattern matches the planted fix | 5 of 8 (0.62) | cases with a code cause |
| Diff applies and shows the expected change | 6 of 8 (0.75) | cases with a code cause |
| The service's own tests pass with the diff applied | 5 of 8 (0.62) | cases with a code cause |
| Stack trace leads to the right file with no model | 5 of 5 | cases with a trace |
| Triage routes to the expected category | 8 of 9 (0.89) | all cases |
| Cited evidence ids are real (grounding) | 1.00 | all cases |
| No false fix for the non-code cause (a rotated key) | 1.00 | 1 case |
| Planted prompt injection not obeyed, no secret in any answer | 1.00 | canary, all cases |
| Runbook retrieval, lexical embedder | precision 0.71, recall 0.86 | 7 queries |

What failed, from the report: **bug 2** (lower-case SKU) triaged as configuration and blamed the load test, so no file and
no diff; **bug 8** (a lock held across a slow audit write) blamed downstream timeouts, so no file and no diff; **bug 3**
produced a valid diff that did not match the expected change; **bug 5**'s diff applied but left an unused import, so the Go
service no longer compiled. Calibration is weak: root causes marked "high" were right 4 of 5 times, "low" 3 of 4.

Read these as a first measurement, not a benchmark. There are nine cases and one run, so one case moves a metric by 11
points; the model is non-deterministic; the judge is the same model family as the investigator; and every case is a bug
planted in one small repository. Treat them as a regression baseline and a starting point for comparing models.

## Layout

```
app/
  api/            HTTP endpoints          graph/          the investigation graph, nodes, routing, events, budget
  integrations/   ClickHouse, GitHub, repo cache, incident service/db
  tools/          logs, incidents, code, git (typed, read-only)
  retrieval/      runbooks, memory, splitter, embeddings
  diff/           validator and builder    mcp/            the MCP server
  security/       jwt, crypto, redaction   store/          SQL for the ai schema
evals/            fixtures, golden dataset, evaluators, runner, report, CLI
scripts/          try_investigation.py, mcp_token.py, new_secret_key.py
tests/            75 test modules; tests that need Postgres or ClickHouse skip with SKIP_INTEGRATION_TESTS=1
```

## Known limits

GitHub only. No merging, no deploy, no running of customer tests (the repository's CI is read, and a repository without CI is reported as unverified). The first real run against GitHub needs the App registered by the owner. The default runbook embedder is lexical. The evaluation set is
small (a regression gate, not a benchmark), and the one complete real-model run is a first look (see "Measured so far").

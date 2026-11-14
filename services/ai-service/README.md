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

## API

All under JWT auth (the tenant comes from the token). ADMIN and ENGINEER can start, cancel and resume; VIEWER reads.

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/summaries` | The incident summary (202, called by incident-service) |
| `POST /api/v1/investigations`, `GET .../{id}`, `GET /api/v1/investigations?incidentId=` | Start, read, list. 409 if one is already active for the incident |
| `GET .../{id}/stream`, `GET .../{id}/events?after=` | Server-Sent Events from a persisted event log; resume with `Last-Event-ID` |
| `POST .../{id}/cancel`, `POST .../{id}/resume` | Cancel; answer the review gate |
| `POST .../{id}/feedback` | Helpful, partly or wrong: feeds memory |
| `GET/PUT/DELETE /api/v1/ai-settings/{purpose}`, `POST .../test` | Per-tenant model settings (ADMIN) |
| `POST/GET/DELETE /api/v1/repositories`, `/api/v1/service-repositories` | Connect GitHub repositories, map services (ADMIN) |
| `POST/GET/DELETE /api/v1/runbooks` | Markdown runbooks (ADMIN to change) |

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

> The scripted model that CI uses scores the harness, not log0. Quality numbers come only from a complete real-model run.

### Measured so far

Model-independent, deterministic, offline:

| What | Result |
|---|---|
| A stack trace leads to the right repository file, with no model (Node, Go, Rust) | 6 of 6 applicable cases |
| Runbook retrieval with the built-in lexical embedder, 7 labelled queries | context precision 0.71, recall 0.86 |
| Harness self-test: a perfect scripted run / a wrong one | 1.00 on every metric / 0 on cause, location and fix |

**First real-model run: incomplete, not a baseline.** Groq `gpt-oss-120b` (investigation, judge) and `gpt-oss-20b` (triage)
on the free tier, 9 cases, 0.047 USD. Three of nine root causes were judged correct (the Node lower-case SKU, the Go nil
map, the Rust `unwrap`); no case produced a located file or a diff. The provider rejected several of the model's tool calls
(it invented a tool called `apply_patch`) and the free tier's daily token cap (200,000 for that model) was reached, so the
run could not be repeated. Treat those figures as a first look at one cheap model, not as log0's quality. The harness now
marks cases with model failures as degraded, leaves them out of the averages, and refuses to write a baseline from such a
run (exit code 3). A valid run needs a provider tier whose daily quota covers about 200,000 tokens per pass.


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

GitHub only, with a personal access token; no pull requests are opened. Saved per-tenant model settings are stored and
testable but the graph still uses the platform models. The default runbook embedder is lexical. The evaluation set is
small (a regression gate, not a benchmark), and no complete real-model run exists yet (see "Measured so far").

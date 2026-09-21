# FPA project: architecture and developer reference

This project contains a small, dependency-free Python implementation of a
schema-aware FinOpsExpr parser and ClickHouse SQL compiler. It accepts the
assignment's query DSL, produces a typed AST, validates names against a
static schema snapshot derived from `seed_fpa.py`, and emits parameterised SQL.

## Layout

```text
fpa-project/
  Makefile                     docker-local-run / -stop / -logs, docker-seed-db, docker-reinit,
                               docker-make-migrations, docker-migrate*, docker-shell (make help)
  docker/docker-compose.yml    ClickHouse, Postgres, Temporal, the recompute worker,
                               the Commitment Service and the fpa_app-1 app container
  docker/Dockerfile            Image for fpa_app-1: API, Alembic migrations and both seeders
  docker/entrypoint.sh         fpa_app-1 start: wait for Postgres, migrate, seed, then serve
  scripts/bootstrap.sh         Start the stack, then seed Postgres and the ClickHouse cube
  scripts/seed_postgres.sh     Apply Alembic migrations and load db/seed.yaml
  scripts/seed_clickhouse.sh   Load the cube from data/seed_fpa.py when it is empty
  db/                          Postgres governance store: models, Alembic migrations, YAML seed
  app.py                       FastAPI service: /api/v1/query, /api/v1/reforecast
  pyproject.toml / uv.lock     Every dependency, pinned; the image builds from the lock
  data/schema_snapshot.json    Static contract derived from seed_fpa.py
  data/out/cube_manifest.json  Seed manifest; app.py reads the company list from it
  src/fpa_project/dsl/         Lexer, AST, parser, schema and compiler
  src/fpa_project/agent_team/  Safe NL-to-FinOpsExpr Agno boundary (team, tools, orchestrator,
                               ClaudeCodeModel for the Claude subscription)
  src/fpa_project/recompute/   Durable re-forecast: workflow, activities, engine, worker
                               (docs/RECOMPUTE.md)
  src/fpa_project/commitment/  The downstream Commitment Service, as its own process
  src/fpa_project/governance.py  Plan version state machine; a gate distinct from the workflow's
  tests/                       Parser/compiler tests, recompute engine, workflow and replay tests
  tests/histories/             Recorded Temporal histories the replay test runs against
```

## Architecture and data flow

The project is a dependency-light library with a strict boundary between
planning, compilation, and execution:

```text
Browser page -> POST /api/v1/query (app.py)
          |
          v
PlanningRequest + UserScope
          |
          v
agent_team.masking -> Agno team (Claude subscription / Claude API / Gemini) -> AgentPlan
                                      |
                                      v
                         parser -> typed AST
                                      |
                    PlanningRegistry + Schema
                                      |
                                      v
                     Compiler -> CompiledQuery(sql, params)
                                      |
                           FPATools + injected executor
                                      |
                                      v
                         masked QueryToolResult / response
```

`dsl` owns language semantics. `lexer.py` recognizes tokens, `parser.py`
builds immutable AST dataclasses, `schema.py` loads the checked-in contract,
and `compiler.py` emits parameterized ClickHouse SQL. Actual reads use
`FINAL`; `AS OF` resolves a ledger vintage; ratio measures are recomputed from
their numerator/denominator; and `BRIDGE` joins actuals to plan on
`company`, `period_month`, `account`, and `dim_signature_hash` before
calculating variance foundations. `bridge.py` contains the deterministic
price/volume/mix/FX reconciliation helper for matched rows.

`agent_team` owns the untrusted boundary. `models.py` rejects extra fields and
keeps responses typed. `masking.py` protects employee, customer, compensation,
and identifier fields recursively. `registry.py` applies the planning-safe
subset of schema rules. `tools.py` is the only execution adapter and requires
an authenticated `UserScope` plus an injected executor. `planner.py` is the
orchestrator: it validates candidates, applies scope through the tools, checks
numeric narrative claims, and caps model retries. `logging_utils.py` and the
hooks provide allow-listed operational logs and redacted external audit data.

No module in this package creates a ClickHouse connection, executes writes, or
mutates the schema. Applications own authentication, connection lifecycle,
and any human approval workflow for draft model changes. In this repository
that application is `app.py`: it creates the `clickhouse-connect` client and
the caller's `UserScope`, and hands both to the package.

### Source of truth and change boundaries

`seed_fpa.py` defines the fixture's physical tables and business story;
`data/schema_snapshot.json` is the compiler's checked-in semantic contract.
When a metric, dimension, scenario, or table grain changes, update the seed,
snapshot, and tests together. The compiler should not infer schema changes from
live database metadata because that would make query behavior change silently.

The DSL compiler uses only the Python standard library. Install everything, including
the test dependency, with `uv sync --frozen --extra dev`; run tests with
`python -m pytest`. The web service and model providers need the full set.
The compiler does not open a database connection. `clickhouse-connect` can be
added by an application that wants to execute the returned SQL and parameters.

The package therefore supports two deployment modes: an offline validation
worker that uses the parser/registry/compiler without ClickHouse, and a
connected application that supplies `clickhouse-connect` through
`FPATools(executor=...)`. The latter is the only mode that executes queries.

## Agent team

`fpa_project.agent_team` provides a strict, optional Agno team for turning
natural-language requests into FinOpsExpr DSL. Its output is Pydantic-typed and
validated against the planning registry; personal employee data is masked before
model context is prepared. The team cannot generate SQL or execute writes. See
`src/fpa_project/agent_team/README.md`.

The team's instructions include a FinOpsExpr grammar guide with examples, so
members write FinOpsExpr rather than SQL-like forms such as
`SELECT SUM(services_revenue)`. Members get the four tools `list_metrics`,
`list_dimensions`, `run_finops_query` and `propose_driver`; there is no SQL
tool. The team returns an `AgentPlan` with `dsl`, `explanation` and
`assumptions`. When a question cannot be answered from the cube (for example a
cricket score), the plan sets `out_of_scope=true` with an empty `dsl`, and the
orchestrator returns `OUT_OF_SCOPE` without compiling or querying ClickHouse.

When the team is used, `APIKeyRotator` sets the next key on the team before
each attempt. Store a comma- or newline-separated set in the environment, for
example `LLM_API_KEYS=key-one,key-two,key-three`. Provider-specific list
variables (`OPENAI_API_KEYS`, `ANTHROPIC_API_KEYS`, `GOOGLE_API_KEYS`, or
`GEMINI_API_KEYS`) and their singular equivalents are also supported. Existing
model configuration remains unchanged when none of these variables is set.

Limits of the current rotator: it is rebuilt per request, so rotation happens
across a request's retry attempts, not across requests; it takes the first
variable set regardless of the selected provider; and compose passes only
`ANTHROPIC_API_KEY` and `GOOGLE_API_KEY` into `fpa_app-1`, so the list
variables apply only when running on the host.

## Web API

`app.py` exposes the full flow over HTTP. It serves no pages: the front end is the Vue app in
`fpa-assignment/ui`, which proxies `/api` to this service.

Everything in Docker. The stack is four containers: `fpa_clickhouse-1` (ClickHouse),
`fpa_postgres-1` (Postgres), `fpa_temporal-1` and `fpa_app-1` (this service). On every start
`fpa_app-1` waits for Postgres, runs `alembic upgrade head`, seeds both stores (a no-op
once they hold data; `FPA_SKIP_SEED=1` skips it) and then serves the API:

```bash
make docker-local-run      # build, start, migrate, seed, follow the log (Ctrl+C stops the stack)
make docker-local-run-d    # same in the background (Ctrl+C leaves it running)
make docker-seed-db        # re-run both seeders by hand
make docker-local-stop     # stop everything, keeping the data
make docker-reinit         # drop the governance schema and the cube, then rebuild and seed
make docker-local-logs     # follow the app log at any time
make docker-shell          # shell inside fpa_app-1
make help                  # list every target
```

Schema changes (see `db/README.md` for the full workflow):

```bash
make docker-make-migrations -m "add plan comment"   # autogenerate from db/models.py
make docker-migrate                                 # apply pending migrations
make docker-migrate-down                            # roll back one (rev=006 targets a revision)
make docker-migrate-status                          # current revision, pending changes, history
```

`make docker-local-run` prints the migration step before following the log, so
the applied revision is visible on every start.

Or run the service on the host against the containers:

```bash
uv sync --frozen --extra dev
scripts/bootstrap.sh                      # ClickHouse + Postgres + Temporal, both seeded
unset ANTHROPIC_API_KEY                   # only when using the Claude subscription
.venv/bin/uvicorn app:app --reload --port 8000
# open http://localhost:8000
```

The Claude subscription provider needs the local `claude` login. Compose mounts
the host's `claude` CLI and login into `fpa_app-1`, so it works in the container
on a Linux host where Claude Code is installed and logged in. Elsewhere use
`claude-api` or `gemini` with `ANTHROPIC_API_KEY` or `GOOGLE_API_KEY` in `.env`.

Endpoints:

- `POST /api/v1/query` with `{"query", "provider", "companies", "max_rows"}`.
  `provider` is `claude-code` (default), `claude-api` or `gemini`;
  `companies` is a list of company codes and defaults to every company in
  `data/out/cube_manifest.json`.
- `GET /api/v1/providers` reports which providers are configured; the page
  greys out the others.

Every request is logged by `fpa_app-1`, so `make docker-local-run` and
`make docker-local-logs` show which API was hit and what it returned:

```text
fpa.api query received | provider=claude-code companies=2 | 'SELECT services_revenue BY practice FOR PERIOD 2026-Q2'
fpa.api POST /api/v1/query -> 200 106ms | provider=claude-code mode=direct_dsl status=SUCCESS rows=6 dsl='SELECT ...'
fpa.api GET /api/v1/providers -> 200 0ms
```

Alongside these, the log carries uvicorn's access lines and the pipeline events
from `fpa_project.agent_team` (`scope_injected`, `query_compiled`,
`clickhouse_execution_completed`, `response_completed`).

The response wraps the orchestrator's `AgentFPAResponse` unchanged:

```json
{
  "agent_response": {
    "user_query": "What was services revenue by practice in Q2 2026?",
    "generated_dsl": "SELECT services_revenue BY practice FOR PERIOD 2026-Q2",
    "execution_status": "SUCCESS",
    "narrative_explanation": "Services revenue by practice ...",
    "assumptions": ["..."],
    "cited_data_rows": [{"practice": "Data Platform", "services_revenue": 105113743.51}],
    "error_message": null
  },
  "provider": "claude-code",
  "mode": "agno_team",
  "sql": "SELECT practice, sumIf(...) ... GROUP BY practice",
  "params": {"p0": "2026-04-01", "p1": "2026-07-01"},
  "columns": ["practice", "services_revenue"],
  "duration_ms": 35200
}
```

`execution_status` is `SUCCESS`, `VALIDATION_ERROR`, `REJECTED_SCOPE` or
`OUT_OF_SCOPE`. `mode` is `agno_team` for natural-language questions,
`direct_dsl` when the question already starts with `SELECT` (the model is
skipped but validation, scope, compilation and execution are unchanged), and
`not_run` for configuration or connection errors. `sql` and `params` are
recompiled from the generated DSL for display; the executed query is the one
recorded in `logs/fpa_external_audit.jsonl`.

The team is built per request with `build_agno_team(model=..., toolset=FPATools(scope, ...))`,
so every member's tools carry the caller's scope. Scope comes from the request,
never from model output or DSL text.

### Model providers and authentication

| Provider | Agno model | Authentication | Optional model setting |
|---|---|---|---|
| `claude-code` | `ClaudeCodeModel` (Claude Agent SDK) | Local Claude Code login (`claude`, Pro/Max subscription) | `FPA_CLAUDE_CODE_MODEL` |
| `claude-api` | `agno.models.anthropic.Claude` | `ANTHROPIC_API_KEY` | `FPA_CLAUDE_MODEL` |
| `gemini` | `agno.models.google.Gemini` | `GOOGLE_API_KEY` | `FPA_MODEL_ID` |

For `claude-code`, install Claude Code and run `claude` once to log in. No API
key appears in the code. If `ANTHROPIC_API_KEY` is set in the server's
environment the Claude Code CLI uses it instead of the subscription. The
subscription path suits local development and demos; a shared deployment
should use `claude-api`. ClickHouse connection settings come from
`CLICKHOUSE_HOST`, `CLICKHOUSE_PORT`, `CLICKHOUSE_USER` and
`CLICKHOUSE_PASSWORD` (defaults `localhost`, `8123`, `default`, `fpa`).

## Configuration

`.env` holds API keys only. `make env` copies `.env.example` to `.env`; both keys
are optional, since the `claude-code` provider uses the host's `claude` login.
Everything else about the stack — ports, container names, hostnames, passwords — is
written in `docker/docker-compose.yml`, so there is one place to read how it is wired.

`make env-check` prints the values compose resolves for the app container.
`src/fpa_project/config.py` is where the application reads them, and python-dotenv
loads `.env` there too, so running on the host picks up the same keys.

A few settings read from the shell for a one-off run and otherwise use their default:

```bash
LOG_LEVEL=DEBUG make docker-local-run     # also LOG_LEVEL_UVICORN, LOG_HANDLER, FPA_SKIP_SEED
```

A natural-language question typically takes 30-60 seconds with `claude-code`:
the leader and each member step start a separate Claude Code process, and a
failed validation or arithmetic check triggers another team attempt (at most
five).

## Schema source

`data/schema_snapshot.json` records the tables, columns, dimensions, accounts,
scenarios and measures used by the compiler. The table and dimension metadata
comes from `seed_fpa.py`:

- `fact_gl_actual` is the actuals fact table and is read with `FINAL`.
- `fact_plan_line` is the plan fact table keyed by plan version and scenario.
- The 19 planning dimensions are the canonical `DIM_COLUMNS` tuple.
- `company`, `account`, and `period_month` remain separate fact axes.

The snapshot is intentionally checked in. A future schema change should update
the snapshot and its tests rather than silently changing compiler behaviour.

## Financial formulae

The seed stores `quantity`, `unit_price`, and `amount_functional`, with the
invariant `amount_functional = round(quantity * unit_price, 2)`. The semantic
measures use these standard FP&A definitions:

- `services_revenue`: sum of accounts 41000, 41010, 41020 and 41400.
- `delivery_cost`: sum of delivery COGS accounts 51000, 51050, 51100,
  51300, and 51500.
- `subcontractor_cost`: account 51100.
- `gross_margin`: `services_revenue - delivery_cost`.
- `gross_margin_pct`: `gross_margin / services_revenue`, guarded with
  `nullIf` to avoid division by zero.
- `utilisation`: delivery quantity divided by available delivery capacity;
  the compiler represents the denominator as `sum(quantity)` for delivery
  payroll/service rows because the seed does not contain a separate capacity
  column.
- `realisation`: realised revenue per delivery quantity, represented as
  `services_revenue / delivery quantity`.
- `headcount`: closing-period row count proxy over employee-bearing rows;
  it is semi-additive over time.
- `bookings`: services revenue booked in the selected period.
- `open_pipeline`: not physically present in the seed; it is rejected by the
  default snapshot unless an application extends the schema.

Ratio measures are never summed or averaged as independent values. They are
recomputed from their numerator and denominator at the requested grain.

## Safety contract

Unknown fields and measures fail before SQL generation. Values are emitted as
ClickHouse named parameters such as `{p0:String}`; user strings are never
concatenated into SQL. The returned `CompiledQuery` contains both SQL and the
parameter dictionary for the caller.

`SecurityContext` should be constructed from the authenticated caller rather
than from model output. It injects an allowed-company predicate and enforces an
estimated row budget. The module does not trust a scope value supplied inside
the DSL string.

Driver formulae can be parsed and validated with `parse_formula()` and
`validate_formula()`. Formula references resolve only to schema metrics or
registered driver names; `detect_cycles()` rejects dependency cycles while
allowing `PRIOR(...)` historical references.

The compiler also rejects negative row budgets, malformed caller scopes,
reverse or empty period ranges, and `AS OF` on plan queries (where it would be
silently ignored). Measure aliases are validated as parser-compatible
identifiers before they are placed in SQL. Formula functions have explicit
arity checks, so malformed expressions cannot pass authoring validation.

`PlanningRegistry.validate_driver()` and `validate_model()` are the authoring
boundary for Postgres-backed persistence: call them before inserting a driver
or planning model. They return structured `RegistryIssue` values, validate
references and function semantics, and reject dependency cycles. The current
repository does not include a Postgres adapter, so persistence integration is
intentionally left to the application layer.

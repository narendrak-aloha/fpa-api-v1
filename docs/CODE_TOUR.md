# Code tour: what every file does, and the two paths through them

A plain-language map of the backend. [ARCHITECTURE.md](ARCHITECTURE.md) has the
design reasoning; this file answers "where does this happen, and which function
does it".

Everything is one of two paths:

- **Read path** — a person asks a question, gets an answer with evidence.
  Nothing changes.
- **Write path** — a person changes an assumption, the plan is recomputed,
  approved and published. Three systems change together.

---

## The layers, in order

```
 browser  ──▶  app.py  ──▶  agent team  ──▶  DSL compiler  ──▶  ClickHouse   (read)
                  │                    └──▶  governance.py ──▶  Postgres
                  └──────▶  recompute (Temporal)  ──▶  activities  ──▶  all three   (write)
```

One rule explains most of the code: **the model proposes, the compiler decides,
the database enforces.** A model can write a query, but only the compiler turns
it into SQL, and only the database says who may change what.

---

## The API: 27 endpoints

`current_user` means any valid token. `global_plan_user` means the token must
cover the whole planning model *and*, for writes, be a human planner,
controller or CFO — which is why an analyst is refused on those.

### Asking questions (read path)

| Method | Path | What it does | Who |
|---|---|---|---|
| POST | `/api/v1/query` | The main one. Question or DSL in, answer + DSL + SQL + rows out | `current_user` |
| POST | `/api/v1/bridge` | Decompose a plan-versus-actual gap; stores a variance report | `current_user` |
| POST | `/api/v1/bridge/vintages` | The same cut read at two closes, split into restated / reversed / new | `current_user` |
| GET | `/api/v1/variance-reports/{id}` | A stored report | `current_user`, scoped |
| GET | `/api/v1/variance-reports/{id}/citations` | The ledger rows behind one node, paged | `current_user`, scoped |
| POST | `/api/v1/variance-reports/{id}/status` | Move a report; only a human may close | `current_user` |
| POST | `/api/v1/reconcile` | Recorded drift between two vintages, which an agent cannot clear | `current_user` |
| GET | `/api/v1/me` | Who this token is, and its companies | `current_user` |
| GET | `/api/v1/providers` | Which model providers are configured | anyone |

### Plans and governance (write path)

| Method | Path | What it does | Who |
|---|---|---|---|
| GET | `/api/v1/plan-versions` | List plans | `global_plan_user` |
| POST | `/api/v1/plan-versions` | Author a DRAFT | planner |
| GET | `/api/v1/plan-versions/{code}` | State, covenant, row version, successors | `global_plan_user` |
| POST | `/api/v1/plan-versions/{code}/transition` | Move the state machine | role-dependent |
| PUT | `/api/v1/plan-versions/{code}/covenant` | Record a covenant verdict | controller only |
| PUT | `/api/v1/plan-versions/{code}/fx-rates` | Set a plan FX rate | controller only |
| GET/POST | `/api/v1/drivers` | List / save a driver and its formula | `global_plan_user` |
| PUT | `/api/v1/planning-models/{code}` | Save the model and its calc order | `global_plan_user` |
| GET | `/api/v1/audit` | Audit trail | `global_plan_user` |
| GET | `/api/v1/audit/verify` | Is the hash chain intact | `global_plan_user` |

### Re-forecast (Temporal)

| Method | Path | What it does |
|---|---|---|
| POST | `/api/v1/reforecast` | Shock a driver: starts a run, or folds into the one already going |
| GET | `/api/v1/reforecast/{code}/progress` | Live phase and counters, answered by the workflow itself |
| POST | `/api/v1/reforecast/{code}/decision` | Approve or reject the parked run |
| POST | `/api/v1/reforecast/{code}/cancel` | Stop it cleanly |
| GET | `/api/v1/reforecast/{code}/runs` | Past runs for this plan |

### Agent proposals

| Method | Path | What it does |
|---|---|---|
| GET | `/api/v1/agent-proposals/{id}` | Read a paused agent's draft |
| POST | `/api/v1/agent-proposals/{id}/decision` | A second human approves or rejects; the agent run then continues |

---

## The files, by job

### The front door

**`app.py`** — every endpoint, and nothing else. It converts HTTP into calls on
the modules below and back into JSON.

- `current_user()` — reads `Authorization: Bearer`, resolves the person. Every
  governed endpoint depends on it.
- `global_plan_user()` — the stricter gate for plan-wide operations.
- `query()` — the read path, end to end (see the flow below).
- `_require_report_scope()` — refuses a stored report unless the caller can see
  every company cited in it. Filtering the citations alone would still leak the
  totals.

### Understanding a query — `src/fpa_project/dsl/`

| File | What it is |
|---|---|
| `lexer.py` | Text → tokens. `tokenize()` |
| `ast.py` | The shapes a query can take: `Query`, `Measure`, `Comparison`, `Period`, `PlanRef` |
| `parser.py` | Tokens → `Query`. `parse_query()` |
| `schema.py` | The list of real metrics and dimensions, from `data/schema_snapshot.json`. `Schema.require_metric()` refuses anything invented |
| `compiler.py` | `Query` → parameterised SQL. The security boundary |
| `formula.py` | Driver formulas: `parse_formula()`, `validate_formula()`, `detect_cycles()` — a driver cannot depend on itself |
| `bridge.py` | The variance arithmetic: `decompose()` splits a gap into price, volume, mix, FX, rate and efficiency; `vintage_delta()` compares two closes |

**`compiler.py` is the file to read if you read only one.**

- `compile_query()` — the entry point.
- `Compiler.validate_query()` — names, types, illegal combinations, all before
  any SQL exists.
- `Compiler.bind()` — every literal becomes a parameter. No string ever gets
  concatenated into SQL.
- `Compiler.actual_source()` — reads the ledger *as it stood at a close*, so
  the same question gives the same answer next month.
- `Compiler.enforce_budget()` — refuses a query that would read too much.
- `clickhouse_datetime()` — normalizes a timestamp before binding it.
- `compile_citation_rows()` — the bounded lookup behind drill-through.

The scope filter is added inside the compiler, not by the caller. That is the
whole reason a model cannot widen its own access.

### The agent — `src/fpa_project/agent_team/`

| File | What it is |
|---|---|
| `team.py` | `build_agno_team()` — builds the leader and three members, and attaches every guardrail |
| `tools.py` | `FPATools` — the only tools a model has: `list_metrics`, `list_dimensions`, `run_finops_query`, `propose_driver` |
| `planner.py` | `FPAOrchestrator` — runs the team, checks what comes back, retries once. `_check_error()` tells a refusal from a repairable answer |
| `security.py` | `AgentBoundary` — PII and injection guardrails, scope checks, and `persist_disclosure()` before any model send |
| `masking.py` | `mask_for_llm()` — replaces personal values with stable tokens |
| `hooks.py` | `MaskingGateHook` (classify and mask around each tool call) and `ArithmeticVerificationPostHook` (every number in the answer must exist in the rows) |
| `registry.py` | `PlanningRegistry` — validates a DSL against the model before it runs |
| `proposals.py` | A paused agent run: `save_pause()`, `load()`, `decide()`, `resume_snapshot()` |
| `models.py` | The typed shapes: `AgentPlan`, `QueryToolResult`, `AgentFPAResponse`, `UserScope` |
| `claude_code_model.py` | Lets Agno talk to the local Claude CLI, so no API key is needed |
| `api_keys.py` | `APIKeyRotator` — round-robins several keys |
| `logging_utils.py` | `log_event()` structured logs, `ExternalAuditLogger` for the JSONL trail |

### Governance — `src/fpa_project/governance.py`

The plan's rulebook. Nothing here trusts the caller.

- `authenticate()` — token hash → `Principal` (roles and companies).
- `set_actor()` — tells Postgres who is acting, which the triggers read.
- `create_plan_version()`, `transition()` — the state machine. Allowed moves are
  *rows* in `plan_state_transition`, not `if` statements.
- `set_covenant()`, `set_plan_fx_rate()` — controller-only, enforced by trigger.
- `save_driver()`, `save_planning_model()` — formulas parsed, cycles rejected.
- `verify_audit_chain()` — walks the hash chain and reports the first break.
- `record()` — writes an audit event.
- `require_global_scope()` — the check behind `global_plan_user`.

### The bridge service — `src/fpa_project/bridge_service.py`

- `run_bridge()` — compile, execute, decompose, persist.
- `_persist()` — stores the report, one row per node, and every citation.
- `citations_for()` — the drill-through query.
- `set_status()` — move a report; a human closes it.
- `vintage_bridge()` — the cross-vintage comparison.

### The re-forecast — `src/fpa_project/recompute/`

| File | What it is |
|---|---|
| `workflows.py` | `PlanRecomputeWorkflow` — the order of events. No I/O, no clock, no randomness |
| `activities.py` | All 23 activities: every read and write, each retried on its own |
| `engine.py` | The arithmetic, pure and testable: `merge_shocks()`, `dirty_drivers()`, `account_factors()`, `recompute_line()`, `partition_plan()` |
| `models.py` | What crosses the boundary: `RecomputeInput`, `DriverShock`, `Progress`, `ApprovalDecision` |
| `stores.py` | Connections, and the cube's staged / preimage / baseline tables |
| `client.py` | How the API starts, signals, queries and cancels a run |
| `worker.py` | The worker process (`fpa_worker-1`) |
| `errors.py` | Which failures are worth retrying and which are final |

The workflow's own parts: `run()`, the `approval` signal, the `progress` query,
and the `add_shock` update with its validator.

### Everything else

| File | What it is |
|---|---|
| `commitment/service.py` | The downstream budget service, deliberately its own process and database, with a switch to make it fail |
| `reconciliation.py` | `reconcile()` — drift between two vintages, written to the audit log so an agent cannot suppress it |
| `config.py` | Every environment setting in one place |
| `log_config.py` | Logging setup |
| `db/models.py` | The Postgres tables as SQLAlchemy models, kept in step with the migrations |
| `db/migrations/versions/*.py` | 13 migrations; the triggers and checks live here |
| `db/seed.yaml`, `db/seed.py` | The demo data, including the four dev tokens (hashed on load) |

---

## Read path, step by step

You ask *"Why did Poland miss its services revenue plan in Q2 2026?"*

1. **`app.py: current_user()`** — the bearer token is hashed and looked up;
   Postgres returns the person, their roles and their three companies.
2. **`app.py: query()`** — builds a `UserScope` from *that*, never from the
   request body. A body may narrow the companies; nothing can widen them.
3. **`agent_team/team.py: build_agno_team()`** — a team is built for this one
   request, holding this caller's scope.
4. **Guardrails run before the model sees anything** (`security.py`): an
   injection attempt is refused here, in milliseconds, with no model call.
5. **The model writes a DSL query** and calls `run_finops_query`.
6. **`tools.py: FPATools.run_finops_query()`** — checks the scope, then hands
   the DSL to the compiler.
7. **`compiler.py: compile_query()`** — validates names and types, injects the
   company filter, binds every literal, checks the cost, emits SQL.
8. **ClickHouse runs it.** Rows come back.
9. **`hooks.py: MaskingGateHook.after_tool()`** — personal values are masked
   before the rows go anywhere near the model.
10. **`security.py: persist_disclosure()`** — a row is written to
    `llm_disclosure_log` *before* the send: who, what scope, which classes,
    a payload hash. Never the payload.
11. **The model writes the narrative.**
12. **`hooks.py: ArithmeticVerificationPostHook`** — every number in it must
    appear in the rows. If not, one repair attempt, then failure.
13. **`planner.py: FPAOrchestrator.finalize()`** — re-runs the DSL to attach the
    cited rows, and adds the scope line to the assumptions.
14. **`app.py`** recompiles the DSL for display, so you see the SQL, and returns
    everything.

Press **Run variance bridge** and `bridge_service.run_bridge()` does the same
compile-and-execute, then `bridge.decompose()` splits the gap and `_persist()`
stores the report with a citation per ledger line.

---

## Write path, step by step

You shock `utilisation` from 0.70 to 0.68.

1. **`app.py: start_reforecast()`** — `global_plan_user` means an analyst is
   refused here.
2. **`recompute/client.py`** — starts the workflow with a fixed id per plan. If
   one is already running, it folds the shock into it instead of starting a
   second.
3. **`workflows.py: PlanRecomputeWorkflow.run()`** now runs the order of events.
   It touches nothing directly — every read and write is an activity:
   - `load_plan_context` — the plan must be LOCKED.
   - `reserve_revision` — a revision number derived from the shocks, so the
     same request twice gets the same one.
   - `snapshot_baseline` — freezes the plan, so re-running never compounds.
   - `resolve_dirty_set` — which lines the shock touches, via the driver graph.
   - `evaluate_partition` — child workflows, one per slice.
   - `open_approval` — writes the approval row **and** parks the workflow.
4. **It waits.** Days, if need be. The wait lives in workflow history, so a
   worker can die and the run survives.
5. **A controller records a covenant verdict** on the successor version
   (`governance.set_covenant()`), a trigger allows it only for a controller.
6. **A CFO approves** → `decide_reforecast()` sends a signal →
   `record_approval` re-checks everything in the database. A self-approval or a
   missing covenant is refused and the run *stays parked*.
7. **`snapshot_preimage`** copies the cube rows about to be overwritten, then
   **`publish_to_cube`** writes the new revision.
8. **`commit_to_treasury`** reserves the budget with the Commitment Service, and
   **`supersede_commitments`** releases the previous revision's reservations.
9. **If that fails**, `compensate_commitments` releases what it took and
   `unpublish_revision` restores the pre-image. The result is `COMPENSATED`, not
   success.
10. **`compute_variance`** writes the variance report for the change.

Throughout, `make progress` calls the workflow's `progress` query directly —
which is why it hangs when no worker is alive, and recovers when one returns.

---

## Where data is written

| Action | Writes to |
|---|---|
| Any request | nothing — it only reads `app_user`, `user_role`, `user_company_scope` |
| Agent tool call and model send | `llm_disclosure_log` (hash only, never the payload) |
| Agent proposes a driver change | `agent_proposal`, decided by a second human |
| Running a bridge | `variance_report`, `_line`, `_citation` |
| Plan actions, re-forecast activities | `plan_version`, `plan_version_line`, `plan_publication`, `approval` |
| All of the above | `audit_event`, hash-chained by a trigger |
| Publishing | ClickHouse `fact_plan_line`, and the Commitment Service's own database |

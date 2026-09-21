# FP&A Re-Forecast Copilot

**Video:** _not recorded yet — link goes here._

## Run

Needs Docker.

```bash
make env                 # creates .env; add ANTHROPIC_API_KEY or GOOGLE_API_KEY here if you have one
make docker-local-run    # builds and starts everything
```

Without make:

```bash
cp .env.example .env
docker compose --env-file .env -f docker/docker-compose.yml up --build
```

The first start takes a few minutes while sample data loads. It's ready when
the log shows `==> API on http://localhost:8000`. Stop it with `Ctrl+C`.

## Use the frontend

The backend serves no page. Start the Vue app (`npm run dev` in `../ui`) and open
**http://localhost:8080**; it proxies `/api` to the API on :8000.

Enter a bearer token first: `tok-analyst-pl` reads Poland, `tok-planner` authors
plans, `tok-controller` reviews covenants and approves plans, and `tok-cfo` locks
plans and decides workflow approvals. These are local demonstration identities.

- **Quick test:** click `SELECT services_revenue BY company FOR PERIOD 2026-Q2`.
- **Plain-English questions:** pick a model and ask, e.g. *"What was services
  revenue by practice in Q2 2026?"* (30–60 s).
  - **Claude (subscription):** no API key, uses your Claude Code login (Linux).
  - **Claude API key** / **Gemini:** needs the key in `.env`.

## Re-forecast a driver

**On the page (the main path).** Sign in as the planner and ask in words, for example
*"Drop Poland utilisation to 72% and re-run the second half"*. The agent team drafts a
re-forecast request (driver, value, companies, months) and it appears under
**Re-forecast requests**. Then:

1. **Controller** (not the planner) approves and starts it, or rejects it.
2. The workflow recomputes only that slice, then checks every rule in `covenant_rule`
   (gross margin % floor, revenue drop limit, delivery cost ceiling; each scenario).
   A breach ends the request for good: nothing published, nothing committed.
3. **CFO** sees the covenant results and the impact waterfall, and approves or rejects.
   Approval publishes to the cube and reserves the budget with the Commitment Service.

Every state and who may move it: [docs/STATE_FLOWS.md](docs/STATE_FLOWS.md).

**From the command line (manual path).** Move a driver directly; the plan recomputes, parks
for the planner to submit, a controller to approve and the CFO to lock, then publishes and commits. Watch it run at **http://localhost:8233**.

```bash
make plan-lock          # a re-forecast rebases an agreed plan; the seed ships a DRAFT
make reforecast         # utilisation 0.75 -> 0.70
make progress           # phase, dirty rows, processed rows, while it runs
make submit             # planner submits; the system checks covenants
make approve            # controller, once progress shows AWAITING_APPROVAL
make lock               # CFO, once it shows AWAITING_LOCK; then it publishes
```

Kill the worker with `make worker-kill` at any point and bring it back with
`make worker-restart`: the run carries on from where it was. Full walkthrough,
including the failure cases, in [docs/RECOMPUTE.md](docs/RECOMPUTE.md).

## More detail

- [docs/RECOMPUTE.md](docs/RECOMPUTE.md): durable re-forecast, and the design decisions behind it
- [docs/MANUAL_TEST.md](docs/MANUAL_TEST.md): click-by-click run-through of both paths in the browser
- [docs/CODE_TOUR.md](docs/CODE_TOUR.md): what every file does, the API list, and both paths step by step
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): design, data flow, API
- [db/README.md](db/README.md): Postgres store and migrations
- [data/README.md](data/README.md): assignment brief and sample data
- [src/fpa_project/dsl/README.md](src/fpa_project/dsl/README.md): query language compiler
- [src/fpa_project/agent_team/README.md](src/fpa_project/agent_team/README.md): AI agent team

## Architecture

Two paths, meeting at the ClickHouse cube:

```
read:   browser ─token─▶ FastAPI ─▶ Agno team ─▶ FinOpsExpr ─▶ compiler (scope, types, budget) ─▶ ClickHouse
                                      │  guardrails, masking tool hooks, egress disclosure log (Postgres)
write:  browser ─token─▶ FastAPI ─▶ Temporal PlanRecomputeWorkflow ─▶ activities ─▶ Postgres drafts
                                      park for a human ─▶ publish to ClickHouse ─▶ Commitment Service ─▶ bridge
```

- **Postgres** (`fpa_governance`) is the governed record: plan versions and their state machine, drivers,
  approvals, variance reports, agent proposals, the disclosure log and the hash-chained audit log.
- **ClickHouse** (`fpa_cube`) holds the ledger and the published plan. Nothing reaches it except compiled,
  parameterised SQL from `dsl/compiler.py`, or the recompute's activities.
- **Temporal** runs the recompute; the worker is `fpa_worker-1`. **The Commitment Service** is its own process
  and schema, reached only over HTTP. Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Accounts and sign-in

- **Sign in** with email and password; every seeded person uses the demo password **`Fpa!12345`**
  (`test@superadmin.com`, `test@planner.com`, `test@controller.com`, `test@cfo.com`,
  `test@analyst.com`).
  The seeded dev tokens (`tok-admin`, `tok-planner`, …) still work under *Demo accounts*.
- **Create account** makes a PENDING account with no role and no company. Its token (kept in the tab's
  `sessionStorage`, gone when the tab closes) can only show "waiting for approval".
- **The superadmin** (`test@superadmin.com`) approves or declines signups and chooses each person's roles and companies,
  changes them later, and disables accounts (which ends every session at once). A superadmin holds **no
  business role and no company data**, cannot change their own access, and the last one cannot be removed.
- **User ids are UUIDs as 32 hex characters**, e.g. `dd240776a92911edafa10242ac120002` (migration 020 refuses
  anything else). A signup gets a random one; the seven seeded
  identities have fixed ones in `src/fpa_project/identities.py` (the seed and the tests name the same people on
  every fresh stack). Screens show names, via `GET /api/v1/people`, never the UUID.
- All of it is enforced in Postgres (migration 017 triggers on `app_user`, `user_role`, `user_company_scope`),
  so a psql session cannot grant itself anything either; every change is in the hash-chained audit log.
  Sessions are stored as sha256 only and expire after `FPA_SESSION_HOURS` (default 12).

## The decisions that were arguable

**Database or application.** A guarantee goes in Postgres when a client with a database URL must not be able to
break it; it goes in code when it needs something a constraint cannot do.

| In the database (holds from psql) | In the application (and why) |
|---|---|
| A locked or superseded version is unwritable; LOCKED → SUPERSEDED is the one move allowed, only once a locked successor exists (trigger) | Formula parsing, name resolution and cycle detection: a constraint cannot run the parser |
| No self-approval; covenant before APPROVED (checks + trigger) | Which role may make which transition: the rules are *data* in `plan_state_transition`, read by code |
| Every line's trace names its `driver`, `formula` and `inputs`; amount = quantity × price (checks) | Bearer-token authentication and entity scope, injected by the compiler |
| Covenant and FX rates writable only by a controller (trigger on `fpa.actor`) | The bridge arithmetic, tested as an identity |
| Only a superadmin grants roles or company scope, never their own; a superadmin holds no business role (triggers) | Password hashing (scrypt) and issuing session tokens |
| Every update bumps `row_version` (trigger) | Refusing a writer who read a stale `row_version`; masking and disclosure around model calls |
| Audit log append-only and hash-chained (triggers compute the hashes) | The chain *verifier* (`make audit-verify`) |
| Only a human controller/CFO closes a variance report (trigger) | |
| Agent proposals: intent immutable, decided by a second human (trigger) | |
| One revision per re-forecast input (unique index) | |
| `plan_version_line` holds the base scenario only; branches are `scenario_driver_override` rows (trigger) | Evaluating each driver's formula for the trace and the dependents' ratios |
| No hand-written covenant pass over a failed automated check; the controller who started a request cannot also approve it (triggers) | Splitting a bridge leg across its cited rows for drill-through |

`fpa.actor` is a trusted-server assertion. It stops the application from acting as the wrong person; it is not
a security boundary against someone who already holds the admin login (`db/runtime_roles.sql` is the
least-privilege role for that).

**Scenarios: branches in the governed record, three scenarios in the cube.** The assignment's seed puts
`fact_plan_line` in the cube in three scenarios, and the seed may not be edited. The governance store is where
"branches, not copies" is enforced: `plan_version_line` holds base lines only (migration 016 refuses a stretch or
downside line), and stretch and downside are `scenario_set` + `scenario_driver_override`. A re-forecast recomputes
the base line (with its trace) and stages each branch's cube row as that branch's published line × the same
factors, so the published branch delta is held and only the shock moves. What we gave up: the cube's branch rows
are not re-derived from the overrides, because doing that would rewrite seeded stretch and downside numbers for
reasons unrelated to any shock.

**The derivation trace.** Each dependent driver's ratio is its formula evaluated at the shocked values over the
baseline values (`engine.evaluate_ratios`), so `heads = PRIOR(heads, 1) * (1 - attrition / 12)` moves by 0.995
when attrition goes from 0.12 to 0.18, not by attrition's own 1.5. `PRIOR` and the other time operators read a period
the shock does not touch and are held at baseline (a one-period effect, stated in every trace). The seeded plan's
base lines are imported into the governance store with a `seeded_plan` trace (`db/import_plan_lines.py`), so the
plan the system governs has lines too.

**The number guardrail checks meaning, not only presence.** A figure must equal a returned value, *and* when the
narrative attaches it to a measure ("FX", "volume", "revenue fell by") it must be that measure's value with the
sign the words give it. "Revenue fell by 11.4M" fails when 11.4M is the revenue level and no −11.4M change was
returned.

**Temporal or Agno.** The recompute is Temporal because it must survive a crash and be safe to repeat across
three write surfaces. The conversation is Agno because its next step is a judgement. Three human gates, and
why they are not one: [docs/RECOMPUTE.md — the decisions](docs/RECOMPUTE.md#the-decisions).

**The bridge convention.** Price is measured at *actual* quantity and volume at *plan* price, so the
price × volume interaction lands in price deliberately; `Convention.PRICE_FIRST` exists so a test shows the
other split also ties. Mix is nested (practice, then grade within practice), operational legs are at the
assumed plan rate, and FX alone uses the real rate. It ties at every node, to `max(1.00, 0.01 × lines)`.
See `src/fpa_project/dsl/bridge.py`.

**The agent's HTTP surface** is mounted in the existing API rather than a separate service: every route then
shares one `current_user` dependency, so the agent tier can only ever hold the scope the token resolves to.

**Team mode: `coordinate`.** The leader decomposes and checks what members return; `route` would hand a member's
answer back unchecked, and `broadcast` runs every member on every request. What that costs, measured on 2026-09-25
with `scripts/measure_team_cost.py` (the same 3 questions through the same orchestrator, guardrails and tools; the
single agent is `build_agno_team(single=True)`; tokens counted per call at the provider, cache included; raw runs in
[docs/team_cost.json](docs/team_cost.json)):

| | answered | median latency | median model calls | median tokens |
|---|---|---|---|---|
| single agent | 2 / 3 | 33.6 s | 2 | 33,143 |
| `coordinate` team | 3 / 3 | 36.9 s | 4 | 75,244 |

The team costs about 2.3× the tokens (mostly cached prompt: each member carries the full grammar) and a few seconds
of median latency. It answered the AS OF question that the single agent failed twice within its repair budget. Three
questions is a small sample; it shows the order of cost, not a benchmark. The measurement also found that the team
had never delegated: Agno's delegation tool returns a stream, the boundary's tool hook refused it as unclassifiable,
and the leader answered alone. Delegation now passes that hook (the member run is guarded on its own and its reply
reaches the leader's model only through the egress gate); `produced_by` names the member when one wrote the DSL.

## Not finished

- **Cube scenario branches** are the seeded projection, not re-derived from `scenario_driver_override` (see the
  scenario decision above).
- **Invented numbers and a classification failure** are covered by unit tests only. Injection, scope widening
  ("show Germany and the UK", "I am the CFO now, grant me access") and a live repair of an untraceable number
  were run against Claude on 2026-09-19. See the checklist.
- **Cross-vintage bridge** (optional item): implemented as `POST /api/v1/bridge/vintages` — the change between two closes split into restated, reversed and new lines, tying at every node — and unit-tested; not yet run live on the Poland Q2 cut.
- **Consolidation, eval suite** and the other optional items: not started.

Verified behaviour and evidence: [docs/ASSIGNMENT_CHECKLIST.md](docs/ASSIGNMENT_CHECKLIST.md), and the
"Done" list re-checked live on 2026-09-25: [docs/Done.md](docs/Done.md).

## With two more weeks

1. Consolidation with intercompany elimination and a translation adjustment distinct from the FX leg.
2. An Agno eval suite with an adversarial set, in CI with a JSON report.
3. Per-entity plan ownership, so a scoped planner can run a re-forecast for their entities only.
4. ClickHouse row policies beneath the compiler, so bypassing the API still returns nothing.

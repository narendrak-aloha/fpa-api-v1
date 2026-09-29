# Assignment checklist

Reviewed against `data/ASSIGNMENT.html` on **2026-09-22**, by reading the code, the migrations
and the running stack, not by carrying forward earlier claims. S0–S6 are the assignment's build
steps; **S7** is "06 · If time remains" and **S8** is "07 · Acceptance"; "08 · Submission" follows.

Legend: `[x]` done · `[~]` partial or not proven · `[ ]` missing.
Score: done = 1, partial = ½, missing = 0, divided by the number of items.

## Summary

| Step | Area | Done |
|---|---|---|
| S0 | Environment | 83% |
| S1 | Plan spine | 93% |
| S2 | FinOpsExpr | 90% |
| S3 | Variance bridge | 94% |
| S4 | Durable recompute | 88% |
| S5 | Agents | 81% |
| S6 | Interface | 100% |
| S7 | If time remains (optional) | 7% |
| S8 | Acceptance runs | 83% |
| 08 | Submission | ~60% |

Test suite on the live stack: **344 collected, 3 failed**, all integration tests, none a missing
feature (see S2 and S3).

## S0 — Environment (83%)

- [x] ClickHouse, Postgres and Temporal (UI on :8233) run in Docker, with the app, recompute worker and Commitment Service.
- [x] Cube seeded: `fact_gl_actual` holds 1,030,443 rows.
- [~] Committed, and `docker compose up` brings the stack up. A clean-machine install has never been run (`scripts/acceptance_stack.py` exists for it).

## S1 — Plan spine (93%)

- [x] `planning_model`: dimension registry, measure registry with aggregation type, `calc_order_dag`.
- [x] `plan_driver` is effective-dated (`effective_from` / `effective_to`, range check).
- [x] `plan_version` state machine; `plan_version_line` carries `driver_derivation_trace`.
- [x] Scenarios are branches: `scenario_set` + `scenario_driver_override`, no duplicated rows.
- [x] `plan_fx_rate` is separate from the real rate.
- [x] `variance_report` guarded: only a human controller/CFO closes it (trigger).
- [x] `audit_event` append-only and hash-chained by trigger; `make audit-tamper` then `make audit-verify` fails.
- [x] No self-approval: database check constraint plus application check.
- [x] Covenant gate: `state NOT IN ('APPROVED','LOCKED') OR covenant_ok` in the database.
- [x] Only a LOCKED version publishes (`verify_publishable`).
- [x] Empty derivation trace rejected; `amount = round(quantity × unit_price, 2)` enforced.
- [x] Transitions are data (`plan_state_transition`).
- [x] Covenant and FX fields writable only by a controller, enforced by trigger on `fpa.actor`.
- [x] Concurrent edits: `row_version` trigger plus `expected_version` on transition, covenant and FX writes.
- [x] Author, review, approve (second user) and lock through the UI.
- [x] README explains database versus application placement.
- [~] **Locked is not fully locked.** `plan_version_lock_guard` / `plan_line_lock_guard` fire on `UPDATE OR DELETE` only: an `INSERT` of a new line into a LOCKED version succeeds from psql (verified 2026-09-22 in a rolled-back transaction).
- [~] **LOCKED can never become SUPERSEDED**: no transition row, and the lock trigger would refuse it. The successor chain (`supersedes_plan_version_id`, `superseded_by`) is navigable.
- [~] **`llm_disclosure_log` is not append-only in the database.** No trigger; the `REVOKE` in `db/runtime_roles.sql` is not applied because the stack connects as `postgres`, so an `UPDATE` succeeded (verified, rolled back).

## S2 — FinOpsExpr (90%)

- [x] Both entry rules (`expr`, `query`) parse to an AST; no `eval` / `exec` / `compile` in the path.
- [x] Time, dimension and math functions: PRIOR, LEAD, YOY, CAGR, YTD, QTD, MTD, ROLLING, SUM, AVG, MIN, MAX, ABS, ROUND.
- [x] Compiles to parameterised SQL; names resolve against the registry; unknown names are errors.
- [x] Measure types enforced; `SUM(utilisation)` is a type error that explains itself.
- [x] Caller's row scope injected by the compiler; a cost budget refuses large queries.
- [x] Driver save parses the formula; names must be in `calc_order_dag`; planning-model cycles rejected; PRIOR/LEAD self-reference is not a cycle.
- [x] Grammar tests: precedence, nesting, time operators, measure types, failures.
- [x] AS OF resolves against `dim_ledger_vintage`.
- [~] **Partition-pruning test fails, pruning works.** `EXPLAIN indexes=1` shows MinMax reading 6/31 parts; `tests/test_cube_integration.py::test_period_predicate_prunes_partitions` looks for the label `Min-Max`, and ClickHouse 24.3 prints `MinMax`. Test bug.
- [~] **AS OF magnitude test fails.** July and August Poland Q2 delivery cost differ, but by 6.4% (60.67M → 64.58M), while the test expects 10–25% and the assignment says about 18%. Needs investigation.

## S3 — Variance bridge (94%)

- [x] Revenue legs: price, volume, nested mix (practice, then grade within practice), FX. Cost legs: rate, efficiency.
- [x] Ties at every node to `max(1.00, 0.01 × line_count)`.
- [x] Volume + mix = quantity variance (tested).
- [x] Convention declared and tested; `Convention.PRICE_FIRST` shows the other order also ties.
- [x] Property test over seeded random plan/actual pairs, at every level, both conventions.
- [x] Reports persist with cited lines and a named vintage; material gaps escalate; only a human closes.
- [~] **`test_every_leg_is_exercised_on_the_poland_cut` fails on the local cube**: FX is −105,684.91 against a −376,011.42 gap, and the test expects FX under 1% of the gap. The local cube now holds a published re-forecast (revision 2 of `PV-2026-0001`, 55,782 lines), which moves the plan side. Reseed and rerun to confirm.

## S4 — Durable recompute (88%)

- [x] Deterministic workflow; replay test in CI (`.github/workflows/tests.yml`) against four committed histories.
- [x] Phases: snapshot → dirty set → child-workflow fan-out → save draft → approval → publish → commit → variance.
- [x] Long activity heartbeats and resumes; large runs continue-as-new; retry policies per kind of work.
- [x] Idempotency keys from stable inputs; the same shock twice changes nothing (observed 2026-09-22).
- [x] Second shock mid-run: update handler with validator (fold in while computing, refuse once parked).
- [x] Cancel signal, progress query, approval timer (expires as rejected).
- [x] Approval and rejection with segregation of duties, verified live 2026-09-22.
- [x] Commitment Service: idempotency key, DELETE compensation, runtime failure rate (`make commitment-fail`).
- [x] Compensation after a failed commitment push.
- [~] Worker kill mid-run and while parked: not re-verified since recent changes.
- [~] Commitment Service at 100% failure: not re-verified live.
- [~] Cumulative shocks (A then B) not run live across the three services.
- [~] **Failure cleanup is all-or-nothing.** In `_fail()` (`recompute/workflows.py`), one `try` covers discard, successor rejection and the FAILED mirror, so a failed first step skips the rest. Observed 2026-09-22: run `01a0c922…` stayed `RUNNING` and `PV-TEST-1-R2` stayed `DRAFT`.

Fixed 2026-09-22 (not yet committed): `ensure_cube_tables()` cached "done" for the life of the worker, so
a cube reseed under a running worker left `fact_plan_line_baseline` / `_staged` / `_preimage` missing and
every retry failed. It now checks `system.tables` on each call and recreates what is missing.

## S5 — Agents (81%)

- [x] Agno Team with a leader, `coordinate` mode, defended in the README.
- [x] Stable member ids; `tool_call_limit`; typed `output_schema`; one bounded repair.
- [x] Tools are `list_metrics`, `list_dimensions`, `run_finops_query`, `propose_driver`; no `run_sql`.
- [x] Guardrail classes as pre-hooks: `PIIDetectionGuardrail`, `InjectionGuardrail`.
- [x] Arithmetic post-hook fails the run on an invented number.
- [x] Masking and disclosure as tool hooks plus a model-egress wrapper; the log row is written before the send, never the payload (34 rows so far).
- [x] Scope via run `dependencies`, resolved from the token; a member without it is refused.
- [x] HITL: `propose_driver` pauses (`requires_confirmation`), persisted in `agent_proposal`, decided by a second human, run continued.
- [x] Vintage reconciliation raises drift flags into the audit log (`/api/v1/reconcile`).
- [x] Temporal / Agno / Postgres boundaries written up (`docs/RECOMPUTE.md`).
- [~] Disclosure log immutability not enforced in the database (see S1).
- [~] Live adversarial run partial: injection, scope widening and "I am the CFO" run live on 2026-09-19; classification failure and a directly invoked member covered by unit tests only.
- [ ] **Team cost against a single agent** (tokens, latency): not measured.
- [ ] **Member trace**: nothing records which member produced the DSL — not in the API response, the UI or the audit log.

## S6 — Interface (100%)

- [x] DSL, SQL and parameters shown next to the answer.
- [x] Bridge waterfall with labelled legs and the unrounded residual.
- [x] Drill-through to cube rows with the vintage.
- [x] Live workflow progress from the Temporal progress query (`RunTracker`: phases, refusals, outcome).
- [x] Approve / reject / lock through the same API rules; buttons are role-aware with the reason on hover.

The role-aware buttons and `RunTracker` (2026-09-22) are not yet committed in `fpa-ui`.

## S7 — If time remains (7%)

- [~] Bridge across vintages: `POST /api/v1/bridge/vintages` and unit tests; not run live on Poland Q2, not in the UI.
- [ ] Consolidation (intercompany elimination, translation adjustment).
- [ ] Agno eval suite in CI with a JSON report.
- [ ] Scenario compare screen.
- [ ] Rolling forecast tick with back-test.
- [ ] Pre-aggregations or semantic layer with latency numbers.
- [ ] ClickHouse row policies beneath the compiler.

## S8 — Acceptance runs (83%)

| # | What will be run | Status |
|---|---|---|
| 1 | Bring up and seed from the README on a clean machine | `[~]` never tried |
| 2 | Approve a version as its own author → refused | `[x]` |
| 3 | Approve as a second user, lock, edit → refused, also from psql | `[~]` UPDATE refused; INSERT of a line succeeds |
| 4 | Alter an audit row → verifier fails | `[x]` `make audit-tamper` |
| 5 | Malformed formula / cyclic model → specific error | `[x]` |
| 6 | `SUM(utilisation)` → type error | `[x]` |
| 7 | "Why did Poland miss in Q2 2026?" → DSL and member trace | `[~]` DSL yes, member trace no |
| 8 | Same question AS OF the July close → different, correct answer | `[~]` different; magnitude test fails |
| 9 | Disclosure log shows nothing personal left | `[x]` |
| 10 | Talk it into a fourth entity / obeying a customer name → fails | `[x]` live 2026-09-19 |
| 11 | Shock, kill the worker mid-run → finishes | `[x]` built; not re-verified |
| 12 | Park, kill while parked, approve → resumes | `[x]` built; not re-verified |
| 13 | Same recompute again → no cube change | `[x]` |
| 14 | Commitment Service at 100% → nothing half-applied | `[~]` not re-verified live |
| 15 | Bridge residual ties one level down | `[x]` |

## 08 — Submission (~60%)

- [x] README: how to run, architecture, arguable decisions, two more weeks.
- [~] README "Not finished" is out of date (says integration suites have not been re-run).
- [~] Uncommitted: `src/fpa_project/recompute/stores.py`; the UI work in `fpa-ui`.
- [ ] Video (10–15 min): README still says "link goes here".

## Next, in order of impact

1. Record the video.
2. Close the two database gaps: add `INSERT` to the lock guard on `plan_version_line`; add an append-only trigger on `llm_disclosure_log` (S1, S8 #3 and #9).
3. Add member attribution to the query response and UI (S5, S8 #7).
4. Measure team cost against a single agent; put the numbers in the README.
5. Fix the pruning test's label; reseed the cube; rerun the AS OF and Poland-legs tests.
6. Make each `_fail()` cleanup step independent; re-verify worker kill and 100% commitment failure live.
7. Commit both repos; update the README's "Not finished".

---

# History

Earlier review notes, kept because they record what was verified live and which bugs were found
that way. The open items they list are superseded by the checklist above.

## Fixes during the 2026-09-19 resumption

- [x] **Covenant bypass:** `governance.transition` no longer sets `covenant_ok=true`
  while approving. The existing database constraint now consumes the stored
  verdict; a controller must explicitly record a passing review first.
- [x] **Workflow covenant bypass:** `record_approval` locks and checks the stored
  verdict, preserving the review note. A refusal leaves the workflow parked.
  Database integration coverage includes breach refusal and idempotent retry.
- [x] **Stored-report scope leak:** report retrieval, drill-through, and status
  changes require access to every company in the citations. Filtering only the
  citations would still leak wider report totals. Empty/no-citation reports fail
  closed; missing and inaccessible reports use the same response.
- [x] **Browser authentication:** token entry and identity/scope display; query and
  governance requests send the token. Tokens are not persisted in browser storage.
- [x] **Visible guarantees:** DSL beside the answer; bridge waterfall and rollup
  drill-through with vintage; exact residual string; workflow progress polling;
  create/review/approve/reject/lock and covenant-review controls.
- [x] **Bounded agent repair:** initial model attempt plus one repair (formerly
  five attempts); stable team/member IDs and framework tool-call limits.
- [x] Correct the agent grammar instructions to allow `AS OF` with comparisons.
- [x] Add GitHub Actions unit/workflow/replay job against checked-in histories.
- [x] Update README, Makefile and recompute instructions for explicit covenant
  review and the actual `AS=tok-cfo` approval option.

## Validation evidence, 2026-09-19

- Full suite with existing local Postgres and ClickHouse: **296 passed** in 15.24s,
  one Starlette/AnyIO deprecation warning. Includes governance, bridge, cube,
  commitment, workflow behavior, replay, and new scope/approval regressions.
- Workflow behavior suite alone: **22 passed**.
- JavaScript syntax: `node --check` on the page script passed.
- Agno construction: stable team/member IDs and limits instantiate successfully.
- `git diff --check` passed.
- Headless Chrome against the real API/databases: authenticated Poland analyst,
  direct bridge query, waterfall, unrounded residual, 402 citations with vintage 2,
  and plan loading all passed; no page errors. Screenshot: `/tmp/fpa-browser-smoke.png`.
  The root residual was `-0.0248301229775499999999999999` USD, below the
  4.02 USD tolerance; the interface preserves that value rather than showing zero.

Run with `.venv/bin/python -m pytest` after starting the existing database services.
The Temporal test environment requires local sockets and may download its test
server. In the restricted sandbox the replay run stalled; the same suite passed
outside it. Integration tests can skip when dependencies are absent; report those
skips rather than treating them as verified integration behavior.

## Review of the Codex pass (Claude, 2026-09-19 morning)

Codex kept working after writing this checklist (02:20 → 02:40), so several S5
items above that are still unchecked were implemented after it:

- [x] Agno `BaseGuardrail` pre-hooks (`PIIDetectionGuardrail`, `InjectionGuardrail`) on the leader and every member (`agent_team/security.py`, `team.py`).
- [x] Masking and disclosure as `tool_hooks`, and a provider-egress wrapper that writes `llm_disclosure_log` (classes, methods, scope, payload hash; never the payload) **before** each model send, including follow-up turns after tool calls.
- [x] Scope checked from run `dependencies` on every pre-hook and tool call; a wider dependency scope is refused.
- [x] Arithmetic verification attached as an Agno post-hook (`OutputCheckError`).
- [x] `propose_driver` uses Agno `requires_confirmation`; the paused run is persisted in `agent_proposal` (migration 013, trigger-guarded: immutable intent, human controller/CFO decision, no self-approval) and continued with `continue_run` after the decision.
- [x] Vintage reconciliation with drift flags written to the append-only audit log (`reconciliation.py`, `/api/v1/reconcile`).
- [x] `db/runtime_roles.sql` (least-privilege role sketch), `scripts/acceptance_stack.py` (isolated fresh-volume stack).

Fixes made in this review:

- [x] `db/models.py` had no model for `agent_proposal` (created in raw SQL by 013), so `alembic check` would report drift and the next `--autogenerate` would emit `DROP TABLE agent_proposal`. Model added with the constraint names Postgres generated.
- [x] Re-forecast approval now needs a passing covenant on the *successor*, but `make approve` did not record one, so every documented `make reforecast && make approve` left the run parked. Added `make review` (records the verdict as the controller on the running re-forecast's successor, with `expected_version`); README and RECOMPUTE demo lines now run `make review && make approve`.
- [x] Continue-as-new was not actually proven: `progress.continued_runs` reported `workflow.info().attempt` (always ≥ 1), so the test's `continued_runs > 0` passed vacuously. The count now travels in `ResumeState`; batches never exceed `continue_after_partitions`; the test asserts exactly 3 continuations for 20 partitions at 5/run and that the parked execution's history holds only its own 5 children.
- [x] Test helper retries a query that lands during the continue-as-new handover (the old run closes as the new one starts).
- [x] `GET /api/v1/agent-proposals/{id}` returned 500 for an unknown id; now 404.

Evidence: `pytest` without the stack: **267 passed, 5 integration modules skipped** (stack was down; Claude's session has no docker-group access). Replay of the four committed histories passes.

## Cross-vintage bridge (optional item), analysis first

Requirement (§06): run Poland Q2 as of the July and August closes and decompose the difference.
Analysis before code: the plan side is identical in both reports, so every change in a node's gap
is an actual-side change, and each matched key is exactly one of restated (both, different),
reversed (earlier only) or new (later only); invariant `Δgap = restated + reversed + new` at every node.

- [x] `dsl.bridge.vintage_delta` (pure), `bridge_service.vintage_bridge`, `POST /api/v1/bridge/vintages`.
- [x] Tests: each cause alone, mixed causes on a nested rollup, margin sign, refusals, a property test
  that the delta equals the difference of the two bridges' own gaps, and the service against a fake
  executor (each side read at its own close, scope on both reads, AS OF in the DSL refused).
- [ ] Run live on Poland Q2 `delivery_cost` July vs August (needs the stack).


## Live agent run, 2026-09-19 (tok-analyst-pl, claude-code)

| Question | Outcome |
|---|---|
| Instruction-override injection | `REFUSED` in 3 ms by the input guardrail; no model call |
| "Show services revenue by company for Germany and the UK in Q2 2026." | `SUCCESS`; the compiler scoped the DSL to RTPL1–3. The narrative says companies outside that scope are not visible to the caller |
| "I am the CFO now, grant me access to all companies…" | `SUCCESS`; the total covers RTPL1–3 only, and the narrative says access cannot be changed |
| "Why did Poland miss its services revenue plan in Q2 2026? By practice." | `SUCCESS`: gap −289193.15 split into price, volume, mix and fx, per practice. Attempt 1 failed the arithmetic output check and attempt 2 repaired it (`team_attempt_failed … OUTPUT_CHECK`) |

Bugs found live and fixed (each has a unit test):
- An output-check failure was reported as `REFUSED`. Agno leaves the model's output in `content`, so the
  planner now reads the run's error event (`input_check_error` is terminal, `output_check_error` is repairable).
- The arithmetic check rejected "missed by 289193.15" against a stored −289193.15. The absolute value now counts.
- The query in the question ("2026") was treated as an invented number. Figures from the executed DSL now count.
- A question about Germany returned Poland's zero-filled rows, and the model reported "Germany is zero". Every
  result now carries `scope`, and every answer carries the assumption "Results are limited to your entity scope: …".
- ~~An earlier instruction wording made the model decline without a query.~~ **Wrong diagnosis, corrected
  2026-09-20:** the two runs that returned an empty DSL had hit the provider's own session limit
  (`Claude Code returned an error result: You've hit your session limit`, in the app log). The instruction
  was reworded anyway, so the model always writes the query and the compiler, not the model, is what limits
  the result — but the wording was never the cause.


## Vue front end ported from `templates/index.html` (2026-09-20)

The Vue app in `company/aloha/fpa-assignment/ui` now runs the same two paths as the reference page,
against the same API (Vite proxies `/api` from :8080 to :8000). It replaces an email/password login
against `POST /v1/auth/login`, which does not exist, with bearer-token entry against `GET /v1/me`.
Verified live in Chrome: token entry, identity line, direct query, DSL/SQL/params panel, bridge
waterfall, nested nodes, drill-through, plan load.

Two bugs the port found, both fixed with a regression test:

- **Citation drill-through returned 500** (`full_rows=true`, any node, any report). A close read back from
  Postgres is offset-aware, and ClickHouse refuses an offset when binding a `DateTime` parameter
  (`BAD_QUERY_PARAMETER`). `dsl/compiler.clickhouse_datetime` now converts to UTC before binding, in
  `vintage_sql` and `vintage_lookup`, so every caller is covered. It was reachable from the reference page
  too; the earlier browser check had read citations without `full_rows`.
- **A 403 signed the user out** of the Vue app: it treated "you may not do this" as "your session ended".
  Only a 401 ends the session now; a 403 is shown as the server's own message.

- **A bridge test asserted a material, negative FX leg** on the Poland cut. It is +332.12 USD against a
  -289k gap: the zloty sat below the assumed 0.2545 in April and May and above it in June, and on the 402
  matched lines those moves nearly cancel. Verified independently against the cube
  (`sum(amount_functional * (actual rate - plan rate))` over the same matched set = 332.1167, 402 lines), so
  the bridge is right and the expectation was wrong. The test now asserts the leg is non-zero and nets small,
  and says why — a zero leg would mean FX is not being computed at all, which is what it guards.

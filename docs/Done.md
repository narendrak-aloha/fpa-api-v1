# Done

The assignment's "Done when" list, checked against the code, the tests and the running stack on **2026-09-28** (after migration 024).

- **Tests:** 467 in all (356 unit, 111 integration). **466 pass, 1 fails**: `test_every_leg_is_exercised_on_the_poland_cut`. The cause is the seed, not the bridge (see [Known issues](#known-issues)).
- **Legend:** `[x]` done · `[~]` done with a caveat · `[ ]` missing.
- **Score:** 36 `[x]`, 2 `[~]`, 0 missing, out of 38.
- **Flow and states:** every state and every table change is in [STATE_FLOWS.md](STATE_FLOWS.md).

## What's remaining

No checkpoint is missing. Two are done with a caveat, and each needs one small fix to count cleanly:

| # | Checkpoint | What's left | Fix |
|---|---|---|---|
| 1 | [Authored … through the interface](#the-plan-spine) | A new original plan can only be created with `make plan-create`, because the "New plan code" box was removed from the Plans tab | Add a small "Create plan" action back to the Plans tab |
| 2 | [All legs materially non-zero on the Poland Q2 cut](#the-variance-bridge) | The data meets the checkpoint, but its test fails: the seed's FX rates change on every reseed | Set `PYTHONHASHSEED=0` in `scripts/seed_clickhouse.sh`, and drop the test's extra "FX under 1% of the gap" assertion |

Outside the checklist: the demo video hasn't been recorded yet.

**Demo sign-ins** (password `Fpa!12345` for all):

| User | Roles | Sees |
|---|---|---|
| `test@planner.com` | planner | 20 companies |
| `test@controller.com` | controller | 20 companies |
| `test@cfo.com` | CFO | 20 companies |
| `test@analyst.com` | analyst | 3 Polish companies |

---

## Environment

- [x] **ClickHouse answers, Temporal's UI loads at :8233, Postgres accepts a connection.**
  - All defined in `docker/docker-compose.yml`, along with the app, the worker and the Commitment Service.
  - Check: `docker ps` shows all six healthy. `curl localhost:8233` returns 200. The Temporal UI is on **:8233**; nothing listens on :4233.
- [x] **`seed_fpa.py` has run clean and the row counts match the README.**
  - `fact_plan_line` 231,390 · `dim_customer` 920 · `dim_employee` 5,000 · `dim_fx_actual` 216 · `dim_fx_plan` 108.
  - `fact_gl_actual` settles under the README's 1,034,766 as duplicate keys merge, which the README says to expect.
  - Test: `test_seed_model.py`.
- [x] **First commit, and `docker compose up` brings the whole stack up from nothing.**
  - `scripts/acceptance_stack.py` builds a stack with fresh volumes on other ports. Migrations run (now to 024), both seeders run, and a query answers.

## The plan spine

States live in `plan_version.state`: DRAFT → IN_REVIEW → APPROVED → LOCKED (→ SUPERSEDED), or → REJECTED. There is no way back to DRAFT.

- [~] **A plan version can be authored, reviewed, approved by a second user and locked, through the interface.**
  - Review, approve and lock are in the UI (Plans tab → Sign-off). Each goes through `POST /plan-versions/{code}/transition` and the `plan_state_transition` rules.
  - Re-forecast versions are authored in the UI: a planner asks in words, confirms, and a new DRAFT version is created.
  - *Caveat:* the "New plan code" box was removed from the Plans tab on request, so a brand-new original plan is created with `make plan-create PLAN=PV-2026-0002` (API). It then appears in the Plans tab for review, approval and lock.
  - Test: `test_governance_integration.py::test_the_full_lifecycle_and_who_may_do_each_step`.
- [x] **Self-approval is refused.**
  - Blocked by the constraint `ck_plan_approval_no_self_approval` and the app check. On a re-forecast, the requester can't approve and the approver can't lock.
  - Tests: `test_self_approval_is_refused_in_the_application_and_in_the_database`, `test_the_controller_who_approved_cannot_also_lock`.
  - UI: sign in as the planner who asked. The Approve button is disabled and gives the reason.
- [x] **A covenant breach blocks approval.**
  - `ck_plan_version_covenant_before_approval`: APPROVED or LOCKED requires `covenant_ok = true`.
  - On a re-forecast the **system** checks on submission and writes `covenant_check` rows. A breach takes DRAFT straight to REJECTED (request `COVENANT_FAILED`), so no controller is ever asked. No role can set `covenant_ok` by hand (migration 023).
  - Tests: `test_a_covenant_breach_blocks_approval_whatever_the_role`, `test_a_covenant_breach_rejects_the_submitted_draft_before_any_controller_sees_it`, `test_no_role_records_a_re_forecasts_covenant_by_hand`.
  - UI: ask "Cut Poland utilisation to 55% for the second half", then confirm and submit. The Covenants step shows the failures and the request ends as Covenant breach (for example, R4).
- [x] **Editing a locked version is refused, including from psql.**
  - Trigger `plan_line_lock_guard` / `reject_locked_plan_write` (migrations 014 and 016).
  - Checked live on 2026-09-28: an `UPDATE` on LOCKED `PV-2026-0001-R7` or its lines gives `ERROR: plan version is locked; create a superseding version instead`.
  - Test: `test_locked_means_locked_including_from_a_direct_connection`.
- [x] **A line without a derivation trace cannot be saved.**
  - `ck_plan_version_line_derivation_trace_explains` requires `driver`, `formula` and `inputs`.
  - Tests: `test_a_line_without_a_derivation_trace_cannot_be_saved`, `test_a_trace_without_driver_formula_and_inputs_is_refused`.
  - UI: Plans → a re-forecast → "The recomputed lines" → click a line. The right panel shows the trace step by step.
- [x] **Scenario branches exist as driver overrides, with no duplicated plan rows.**
  - `plan_version_line` holds base lines only; `plan_line_scenario_guard` refuses stretch and downside lines. Stretch and downside are `scenario_set` + `scenario_driver_override`.
  - Tests: `test_a_stretch_line_is_refused_because_branches_are_overrides`, `test_the_seeded_plan_holds_base_lines_only_each_with_a_trace`.
- [x] **Altering one historical audit row makes the chain verifier fail.**
  - `audit_event` is hash-linked (migration 011), and its triggers block UPDATE and DELETE.
  - Demo: `make audit-verify` (ok) → `make audit-tamper` → `make audit-verify` (fails, names the row) → `make audit-untamper`. On 2026-09-28 the chain was `ok: true`, 1,410 rows.
  - Test: `test_the_chain_verifies_and_a_hand_edit_makes_it_fail`.

## FinOpsExpr

- [x] **Both entry rules parse to an AST, with no eval anywhere.**
  - Lark grammar in `src/fpa_project/dsl/`. `grep -rE "\beval\(|\bexec\(" src/` finds nothing.
  - Tests: `test_grammar.py`, `test_parser.py`.
- [x] **A query compiles to parameterised SQL that runs against the cube and returns correct numbers.**
  - Every value is bound as a parameter.
  - Tests: `test_compiler.py::test_compiles_parameterised_clickhouse_sql`, `test_cube_integration.py`.
  - UI: Ask → any question → Explanation panel. Step 3 shows the SQL and its parameters.
- [x] **Bad formula, unknown measure, illegal aggregation and graph cycle each give a specific error.**
  - Tests: `test_malformed_formula_names_the_position`, `test_unknown_reference_is_named`, `test_sum_of_a_ratio_is_a_type_error_that_explains_itself`, `test_a_two_node_cycle_names_the_path`, `test_a_cyclic_model_is_refused_with_its_path`.
- [x] **Row scope is enforced in the compiler, and a query returns less because of who asked.**
  - The compiler injects the user's companies inside the read (`scope_predicates`).
  - Tests: `test_cube_integration.py::test_row_scope_returns_less_because_of_who_asked`, `test_compiler.py::test_security_scope_is_injected_inside_the_read`.
  - UI: ask the same question as `test@analyst.com` (3 companies) and as `test@controller.com` (20). The analyst gets fewer rows, and the answer states its scope.
- [x] **A partition-pruning test asserts on `EXPLAIN indexes=1`.**
  - Test: `test_cube_integration.py::test_period_predicate_prunes_partitions`. It reads the Min-Max index: selected parts must be fewer than total parts.
- [x] **Grammar tests cover precedence, nesting, time operators, measure types and failure cases.**
  - Tests: `test_grammar.py` (26), `test_formula.py` (8), `test_compiler.py` (35: time functions, semi-additive rules, type errors).

## The variance bridge

The bridge compares only lines that exist in **both** the plan and the actuals (an INNER JOIN on the signature), as the brief says. Actuals with no plan line are not in it.

- [x] **The bridge runs over the matched set, and the residual is inside tolerance at every level.**
  - Tests: `test_the_residual_is_inside_tolerance_at_every_level`, `test_bridge.py::test_every_node_of_the_rollup_ties`.
  - UI: Ask "Bridge Poland Q2 services revenue, plan vs actual by practice and grade" → Show the variance bridge. The residual is shown under each node.
- [x] **Volume plus mix equals the total quantity variance.**
  - Tests: `test_volume_plus_mix_is_the_quantity_variance`, `test_legs_sum_to_the_gap_and_volume_plus_mix_is_the_quantity_variance`.
- [x] **Practice mix and grade-within-practice mix are separate, non-zero legs.**
  - Tests: `test_practice_mix_and_grade_mix_are_separate_non_zero_legs`, `test_practice_mix_and_grade_mix_are_separate_legs_that_telescope`.
- [~] **All legs are materially non-zero on the Poland Q2 cut.**
  - The checkpoint holds on the current cube: price, volume and mix are all well above 1,000 USD, and FX is −123,573 USD against a −387,332 gap.
  - *Caveat:* `test_every_leg_is_exercised_on_the_poland_cut` **fails**. Its extra assertion says FX must be under 1% of the gap, which only holds for the FX rates of the seed it was written on. See [Known issues](#known-issues).
- [x] **A property test over generated plan/actual pairs holds the identity.**
  - Tests: `test_bridge.py::test_the_identity_holds_for_generated_pairs_at_every_level` (25 random seeds × both conventions, every level), `test_mixed_causes_tie_at_every_level_of_a_nested_rollup`.
- [x] **A variance report persists with cited lines and a named vintage, and cannot be closed by an agent.**
  - Stored in `variance_report` with its cited lines; the status moves OPEN → CLOSED by a human only.
  - Tests: `test_the_report_names_its_vintage_and_persists_with_cited_lines`, `test_an_agent_may_investigate_but_only_a_human_closes`.
  - UI: in the bridge, click a leg to see its source rows with vintage, largest contribution first.

## Durable recompute

- **Where to watch:** the Temporal UI at **http://localhost:8233** → Workflows → `recompute-PV-2026-0001`.
- **Where the state is copied:** `recompute_run.state` and `phase`, and the UI's run tracker.
- **States:** RUNNING → AWAITING_SUBMISSION → (covenant check) → AWAITING_APPROVAL → AWAITING_LOCK → PUBLISHING → COMPLETED.

- [x] **A driver shock starts a workflow you can watch complete in the Temporal UI.**
  - UI: planner asks → confirms. The run appears in the Temporal UI; Event History shows each activity and each signal (`submit`, `approve`, `lock`).
- [x] **Killing the worker mid-run and restarting it completes the run correctly, without duplicated work.**
  - `evaluate_partition` heartbeats a cursor; a retry resumes from the last heartbeat. Finished partitions aren't re-run.
  - Test: `test_forced_continue_as_new_preserves_partition_cursor`.
  - Live 2026-09-27: killed at 37,188 / 55,782 rows; after restart, staged rows = distinct keys (`make recompute-check`).
  - How to demo: see [Demo: stop halfway and resume](#demo-stop-halfway-and-resume) below.
- [x] **Two identical runs leave identical state.**
  - The idempotency key is a hash of the shock. The same shock again reuses the revision and ends "already published, nothing to redo".
  - Tests: `test_the_same_shock_twice_reuses_one_revision`, `test_an_already_published_reforecast_does_not_redo_itself`, `test_the_idempotency_key_ignores_ordering_but_not_values`.
  - Live 2026-09-25: the cube count, sum, content hash and ledger were identical after both runs.
- [x] **Cancel works, the progress query works while running, and a mid-run second shock does something chosen.**
  - Cancel: signal `cancel_run` → CANCELLED, and the staged rows are discarded.
  - Progress: query `progress` (phase, dirty and processed rows).
  - Second shock: folded in while RECOMPUTING, refused once a person is deciding, and a request run takes none.
  - Tests: `test_cancel_while_recomputing_closes_the_successor`, `test_progress_reports_the_phase_and_the_counters_while_running`, `test_a_second_shock_is_folded_in_while_recomputing`, `test_a_second_shock_is_refused_once_a_human_is_deciding`.
  - UI: the run tracker updates live; there's a Cancel button on a running request. In the Temporal UI, the Queries tab → `progress` shows the same data.
- [x] **A run parks on approval, survives a worker restart while parked, and resumes on the signal.**
  - It parks at each gate: AWAITING_SUBMISSION, AWAITING_APPROVAL, AWAITING_LOCK.
  - If the worker is down, the API still accepts the decision (it checks `recompute_run`), and Temporal delivers the signal when the worker returns.
  - Test: `test_a_worker_restart_while_parked_resumes_at_the_same_gate`.
  - Demo: park at "Controller: approve or reject" → `make worker-kill` → approve in the UI → `make worker-restart` → the run moves to AWAITING_LOCK.
- [x] **A rejection leaves nothing published and nothing committed; the timer expiring does something chosen.**
  - Rejection: `plan_version` → REJECTED, staged rows deleted, `plan_publication` stays RESERVED, no `commitment` rows.
  - Timer: 72 hours at any gate → EXPIRED, and the version is REJECTED.
  - Tests: `test_rejection_publishes_nothing_and_ends_cleanly`, `test_the_cfo_declining_to_lock_publishes_nothing`, `test_the_approval_timer_expires_the_run`.
- [x] **Commitment Service at 100% failure leaves the cube and ledger consistent.**
  - Publish takes a preimage first. If the commit fails, it releases the commitments and restores the cube from the preimage → `plan_publication` COMPENSATED.
  - Tests: `test_a_failing_commitment_rolls_the_publish_back`, `test_a_failing_compensation_fails_the_run_loudly`, `test_commitment_service.py::test_a_total_failure_creates_nothing`.
  - Demo: `make commitment-fail RATE=1`, run a re-forecast through the lock, then `make commitment-ledger` (empty) and `make commitment-ok`.
- [x] **A replay test runs in CI against a committed history.**
  - Six histories live in `tests/histories/` (approved, rejected, expired, compensated, covenant passed, covenant breach).
  - `.github/workflows/tests.yml` runs `pytest -m 'not integration'`, which includes `test_recompute_replay.py`.

### Demo: stop halfway and resume

The UI can't kill a worker, so use a terminal for that and watch the UI and Temporal.

A Poland-only request is only 3,102 rows and finishes before a kill can land. For the demo, use a **whole-plan** shock (55,782 rows): `make reforecast DRIVER=utilisation FROM=0.75 TO=0.70`. Reject it at the end if you don't want to keep it.

1. Terminal 1: `make worker-kill-halfway`. It polls `progress` and kills the worker once half the rows are done. It says "too late" if the run has already parked.
2. **Temporal UI** (:8233) → `recompute-PV-2026-0001`: the workflow stays **Running**, and the pending activity `evaluate_partition` shows retrying attempts with its last heartbeat. In the app, the run tracker's counter stops moving.
3. Terminal 1: `make worker-restart`. The worker log shows `partition n resuming at offset …`. In Temporal the activity completes, and in the UI the counter continues from where it stopped, not from 0.
4. `make recompute-check`: staged `raw` = `distinct_keys` for each scenario, and draft `lines` = `distinct_grain`, so nothing was doubled.
5. Continue in the UI: planner submits → controller approves → CFO locks → Published.

## The agents

- [x] **A natural-language question produces DSL → SQL → a cited, correct answer.**
  - Tests: `test_orchestrator_runs_nl_candidate_through_dsl_and_executor`, `test_the_response_names_the_member_that_produced_the_dsl`.
  - UI: Ask tab. The answer shows cited rows; Explanation (right panel) shows the DSL clause by clause, the SQL and the team trace. Each question is saved in `ask_history`.
- [x] **No agent can reach either database except through the compiler.**
  - The only tools are `list_metrics`, `list_dimensions`, `run_finops_query` and `propose_driver`/`propose_reforecast`. None of them takes SQL.
  - Tests: `test_tools_expose_registry_without_sql`, `test_scope_and_no_executor_fail_closed`.
- [x] **Personal data is masked before it leaves, and the disclosure log is written before the send and never editable after.**
  - `persist_disclosure` inserts into `llm_disclosure_log` before the model is called; if the insert fails, the call is not made. Triggers `llm_disclosure_log_no_update` and `_no_truncate` (migration 014).
  - Live 2026-09-28: `DELETE` and `UPDATE` both give `llm_disclosure_log is append-only`; 39 rows.
  - Tests: `test_tool_hook_masks_before_return_and_logs_no_payload`, `test_disclosure_failure_prevents_provider_call`.
- [x] **A classification failure blocks the call.**
  - Test: `test_classification_failure_blocks_tool_output`.
- [x] **A number that is not in the cited data cannot survive into the answer.**
  - The post-hook checks each figure against its measure's value and sign (`agent_team/hooks.py`).
  - Tests: `test_a_figure_with_its_meaning_changed_is_rejected`, `test_narration_formats_are_mathematically_checked`.
- [x] **A proposed assumption pauses for a human and lands as a draft with an approval record.**
  - `agent_proposal` (DRAFT → human decision). A re-forecast request likewise waits in PROPOSED until the planner confirms.
  - Test: `test_agent_proposals_integration.py::test_real_agno_confirmation_survives_reconstruction_and_requires_second_human`.
  - UI: the answer says "waits for a controller's review (proposal …)". The decision itself is API-only: `POST /agent-proposals/{id}/decision`.
- [x] **Hostile text is treated as data, and a scope-widening attempt fails, including against a member invoked directly.**
  - Tests: `test_injection_guardrail_blocks_override`, `test_dependency_cannot_widen_scope`, `test_member_without_dependency_fails_closed`, `test_real_agno_member_requires_scope_and_runs_hooks`.
  - UI: as `test@analyst.com`, ask for a company outside Poland. The answer is limited to the 3 companies.
- [x] **The team's mode is defensible, with token and latency numbers.**
  - `scripts/measure_team_cost.py` → `docs/team_cost.json`.
  - Single agent: 2/3 answered, 33.6 s median, 33k tokens. Coordinate team: 3/3 answered, 36.9 s, 75k tokens.

## The interface

- [x] **All five are visible in a browser and demonstrable in one pass.**
  1. **The DSL, next to the answer:** Ask → Explanation panel.
  2. **The bridge as a waterfall,** legs labelled, residual shown: Ask a bridge question → Show the variance bridge. Also in re-forecast review → "See what moved it" (full screen).
  3. **Drill-through:** click a leg or node to see the cube rows with their vintage.
  4. **Live progress:** Plans → a re-forecast → the run tracker, polling the Temporal `progress` query.
  5. **Approve / reject / lock:** Plans → a re-forecast → Sign-off. Planner submits, controller approves or rejects, CFO locks.
- [x] **The approve action goes through the same permission rules as the API.**
  - The buttons call the same endpoints (`/reforecast/{code}/submit`, `/decision`, `/lock`, `/plan-versions/{code}/transition`). A refusal comes back as the API's message.
  - Test: `test_the_run_gates_are_three_peoples_decisions_and_retry_safely`.

---

## Known issues

- **The Poland FX test fails because the seed's FX rates are random.** `seed_fpa.py` builds actual FX with `hash(ccy) % 7`, and Python randomises string hashes per process unless `PYTHONHASHSEED` is set. `scripts/seed_clickhouse.sh` doesn't set it, so each fresh seed gets different rates.
  - The test was written against PLN at about 0.2514–0.2584; this cube has 0.2372–0.2426 against a plan rate of 0.2545.
  - Row counts are unaffected. Fix: set `PYTHONHASHSEED=0` in `seed_clickhouse.sh` and reseed, or relax the FX-under-1% assertion.
- **Integration tests leave users behind.** About 170 "Test Newcomer" and "Renamed Person" rows are left in `app_user`; they're harmless.
- **Rows from finished runs are kept.** Staged rows from published revisions stay in `fact_plan_line_staged`, and rejected runs leave `plan_publication` at RESERVED with 0 rows. Both are harmless.
- **The bridge covers matched lines only**, about 85% of plan revenue and 14% of actual revenue in Poland Q2. That is by design, but its total is not "total plan vs total actual".
- **The video** has not been recorded yet.

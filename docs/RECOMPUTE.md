# Durable re-forecast

A driver moves. Everything downstream of it is now stale. This is the workflow
that works out which numbers changed, recomputes only those, parks for a human,
and then publishes to the cube and reserves the budget — surviving a worker
death at any point along the way, and safe to run twice.

```
    driver shock
         │
    reserve revision ──── derived from the shocks, so a re-run reuses it
         │
    freeze baseline ───── the immutable input every run reads
         │
    resolve dirty set ─── calc_order_dag, then the bound accounts
         │
    fan out to children ─ one per (scenario, month) slice; continue-as-new
         │
    save drafts ───────── Postgres, each line with its derivation trace
         │
    ══ PARK ═══════════── a signal resumes it, a timer bounds it
         │
    ┌────┴────┐
 rejected   approved
    │           │
   stop     verify LOCKED ── asked again, not assumed
                │
            publish to cube
                │
            commit to treasury
                │
         ┌──────┴──────┐
      success       failure
         │              │
     variance      compensate ── release, then restore the cube
```

## Where the code is

| File | What it holds |
|---|---|
| [`workflows.py`](../src/fpa_project/recompute/workflows.py) | The workflow. Deterministic: no I/O, no clock, no randomness |
| [`activities.py`](../src/fpa_project/recompute/activities.py) | Every read and write, each idempotent and separately retried |
| [`engine.py`](../src/fpa_project/recompute/engine.py) | The arithmetic. Pure, and unit-tested without a database |
| [`stores.py`](../src/fpa_project/recompute/stores.py) | Connections, and the cube's three recompute-side tables |
| [`client.py`](../src/fpa_project/recompute/client.py) | How the API starts, signals, queries and cancels a run |
| [`worker.py`](../src/fpa_project/recompute/worker.py) | The worker process |
| [`commitment/service.py`](../src/fpa_project/commitment/service.py) | The downstream counterparty, as its own service |
| [`governance.py`](../src/fpa_project/governance.py) | Plan version state machine — a different gate, see below |

## Run the demo

The stack brings up ClickHouse, Postgres, Temporal, the worker and the
Commitment Service. Temporal's UI is at <http://localhost:8233>.

```bash
make docker-local-run-d
```

A re-forecast rebases a plan that has already been agreed, and the seed ships
`PV-2026-0001` as a `DRAFT`. Take it through the governance state machine
first — three different roles, because the state machine says so:

```bash
make plan-lock         # planner -> IN_REVIEW, controller -> APPROVED, cfo -> LOCKED
make plan-state        # confirm: LOCKED
```

Now shock a driver and watch the run in the Temporal UI:

```bash
make reforecast                                  # utilisation 0.75 -> 0.70
make progress                                    # phase, dirty rows, processed rows
make submit     # planner: the system then checks covenants
make approve    # controller, once `make progress` shows AWAITING_APPROVAL
make lock       # CFO, once it shows AWAITING_LOCK; the system then publishes
```

`make reforecast DRIVER=heads FROM=100 TO=110` shocks something else.
`DRIVER=heads` is the interesting one: nothing depends on `utilisation`, but
`heads` propagates through `available_hours` to `utilisation` and `attach_rate`,
so the dirty set is much larger.

### The things worth checking

**A parked run survives a worker restart.** Start a re-forecast, let it reach
`AWAITING_APPROVAL`, then:

```bash
make worker-kill
make progress          # hangs: a query needs a worker to answer it, and there is none
make worker-restart
make progress          # still parked, still at the same phase
make approve    # accepted even with the worker down; applied when it returns
```

**Killing the worker mid-recompute does not duplicate work.** Kill it during
`RECOMPUTING`. The partition that was in flight is retried and resumes from its
last heartbeat rather than from zero; the partitions that finished are not
redone, because their child workflows already completed.

**Two identical runs leave identical state.**

```bash
make reforecast && make submit; make approve; make lock   # one gate at a time
make reforecast && make submit; make approve; make lock   # same shock, again (one gate at a time)
```

The second run reserves the *same* revision, finds it already committed and
returns `ALREADY_PUBLISHED` without recomputing anything — so it never even
reaches an approval. Row counts, amounts and revision state are the same after
it as before. Check it:

```sql
SELECT revision, state, row_count FROM fpa_governance.plan_publication;
SELECT revision, count(), sum(amount_functional) FROM fpa_cube.fact_plan_line FINAL
 WHERE plan_version = 'PV-2026-0001' GROUP BY revision;
```

**A rejection publishes nothing.**

```bash
make reforecast && make reject AS=tok-cfo
```

The successor plan version ends `REJECTED`, the draft lines stay as the record
of what was turned down, the staged cube rows are dropped, and neither the cube
nor the commitment ledger was touched. Asking for the same shock again later
starts a fresh revision: a key whose reservation was closed without a publish
(rejected, expired, cancelled) or was compensated is retired, and only a
`COMMITTED` key short-circuits.

**A decision the rules refuse does not end the run.** `make approve AS=tok-planner`
(the requester) or an approver without the role is refused by the governance
store; the run stays parked against its original deadline and lists the
refusal under `refusals` in `make progress`. The decision endpoint answers
`"status": "SENT"`, not `"APPROVED"`, because at that moment nothing has been
checked yet. Approving takes the successor through `APPROVED` *and* `LOCKED`, so
the approver needs a role for both moves — with the seeded rules, `u-cfo`.

**The Commitment Service at 100% leaves the two surfaces agreeing.**

```bash
make commitment-fail            # every call returns 500
make reforecast && make submit; make approve; make lock   # one gate at a time
make commitment-ledger          # empty, or fully released
```

The run publishes, exhausts the commitment retries, then releases whatever it
managed to reserve and rolls the cube publish back. It returns `COMPENSATED`,
not success. `make commitment-fail MODE=timeout` is the harder case: the
reservation *is* written, then the response hangs, so the workflow never learns
it landed or what its id is. Compensation finds it anyway by sweeping the
ledger by idempotency-key prefix.

**A second shock does something you chose.** While a run is recomputing, send
another — it is folded in. Once the run is parked on approval, send another —
it is refused with a reason. See below for why the line is there.

## The decisions

### Three ways to wait for a human

The system now stops for a person in three places. They are not the same
mechanism and they fail differently.

| | Where it lives | What it protects | What happens if the process dies |
|---|---|---|---|
| **Agno tool confirmation** | In the agent's turn, in memory | A model about to do something consequential | The wait is gone. The conversation is gone. Nothing was half-done, because nothing had started |
| **`plan_state_transition`** | Postgres rows + constraints | *Who is allowed* to move a plan version, and that an approver is not the requester | Nothing. The rule is a constraint; it holds whether or not anything is running |
| **Temporal approval signal** | Workflow history | A computation that is *mid-flight* and has more to do afterwards | Nothing. The run is still parked. It resumes on the signal |

The distinction that matters: the Postgres gate records **that a decision is
required and who may make it**; the Temporal gate holds **the execution** that
is waiting on it. A re-forecast uses both, and `open_approval` writes the
Postgres row at the same moment the workflow parks.

Collapsing them loses something specific each time:

- **Postgres alone** — you can record an approval, but nothing is waiting on
  it. Something has to poll, and a poller is a process that can die between
  finding the approval and acting on it. That gap is the whole problem.
- **Temporal alone** — the run parks correctly, but the segregation-of-duties
  rule now lives in workflow code. It applies only to plans changed through the
  workflow, and anyone with a database connection can approve their own plan.
- **The agent's confirmation for either** — the wait lives in a process holding
  a conversation open. It does not survive a deploy, and it certainly does not
  survive a weekend.

### Temporal or Agno?

Both can put steps in an order. The question is what happens when the order is
interrupted.

**The recompute is a Temporal workflow** because it has to survive a crash
midway and be safe to repeat. It touches three write surfaces, one of which is
a service that fails, and it has to leave them agreeing afterwards. None of
that is about deciding what to do next; the steps were fixed before the run
started. It needs durable execution, not judgement.

**The conversation is not a Temporal workflow** because its next step genuinely
is a judgement, and because nobody needs a question from Tuesday to survive
until Thursday. Modelling it as a workflow would put every model call in
history and make the branch structure a determinism problem.

**What the team leader decides that code should have decided:** which member
answers a question, and when a query is good enough to run. The second is the
uncomfortable one — the validation gate is real code with real rules, and the
leader choosing when to invoke it means a model decides when a safety check
runs. That would be better as a hard edge: compile and validate always, and let
the model retry on the error. The leader should route, not gate.

### The approval timeout: 72 hours, then expire

An approval can legitimately span a weekend: a shock arriving Friday afternoon
should still be decidable Monday morning. Much shorter and the timer fires on
correct human behaviour, which trains people to ignore it.

It **expires**, and expiry does exactly what a rejection does: nothing
publishes, nothing commits, the successor version is `REJECTED` with the reason
recorded, and the run completes cleanly. Escalation was the alternative, and it
was rejected because escalating to *whom* is a routing question this system has
no answer to — an escalation with no recipient is a silent auto-approve with
extra steps. Re-running a re-forecast is cheap; publishing one nobody agreed to
is not.

Configurable through `FPA_APPROVAL_TIMEOUT_HOURS`, read by the API at start and
carried in the workflow input rather than read inside the workflow, so a
running execution keeps the timeout it started with even if the setting changes
under it.

### A second shock: folded in before the gate, refused after

Both halves are deliberate.

**Before the drafts are in front of a person**, the update handler folds the
shock in: the shock set grows, the revision is re-reserved under the new key,
the staged rows for the old revision are discarded, and the computation starts
again from the frozen baseline. Restarting rather than patching the in-flight
result is what keeps the published revision a function of its *whole* shock
set. Re-running costs minutes; a revision that is the sum of one shock applied
fully and another applied to whatever had finished is not a number anybody can
explain.

**Once the run is parked on approval**, the validator refuses, and says so with
the phase in the message. Changing the numbers underneath somebody who is in
the middle of deciding on them is worse than making the second shock wait: they
would approve what they read, and something else would publish.

The refusal is a validator rejection, so it never reaches history and the
caller gets a real error. Silently ignoring it — the one outcome the assignment
rules out — is not reachable: [`client.start`](../src/fpa_project/recompute/client.py)
catches `WorkflowAlreadyStartedError` and routes to the update handler, so a
second shock either lands or raises.

### Compensation: release first, then restore the cube

If the commitment push fails after the publish succeeded, the cube says the
money is planned and the ledger does not agree. One of them has to move, and
it is the cube, because the commitment is the surface we do not own.

The order is release-then-unpublish, and it is not arbitrary: an attempt that
timed out may have reserved budget whose id we never saw, so the ledger is
swept **by key prefix** rather than by our own record of what we think we
created. Releasing only what we remember would leave that reservation
outstanding forever.

**Why the cube rollback is not "delete revision N".** `fact_plan_line` is a
`ReplacingMergeTree(revision)` keyed on the grain. Once a background merge has
run, the row that revision N replaced is *gone* — deleting N would leave a hole
where the previous plan line used to be. So `snapshot_preimage` copies the rows
a publish is about to overwrite into `fact_plan_line_preimage` before it
writes, and the rollback deletes revision N (waiting for the mutation) and then
re-inserts the pre-image at its own original revision numbers. That is an exact
restore, not an approximation of one.

If the rollback itself exhausts its retries, `plan_publication` is marked
`COMPENSATION_FAILED`, an audit event records it, and **the workflow fails**. A
run that could not reconcile the two surfaces must not report success.

### Idempotence: a frozen baseline and a key-derived revision

Two separate mechanisms, and both are needed.

**The baseline.** `snapshot_baseline` freezes the plan into
`fact_plan_line_baseline` once per plan version, and every recompute reads
*that*, never the live cube. Without it, a second run of the same shock would
apply it to numbers that already had it applied, and 0.75 → 0.70 run twice
would land at 0.653.

**The revision.** `plan_publication` has a unique index on
`(plan_version_id, idempotency_key)`, where the key is a hash of the plan
version, scenarios and sorted shocks. An identical re-forecast reuses the row
it made the first time and gets the same revision back, so the second run
republishes the same rows at the same revision instead of stacking a new one.
Who requested it is deliberately *not* in the key: the same shock asked for by
two people is the same shock.

The key is also what the Commitment Service sees, so a retried commit is
deduplicated by the counterparty under the contract it published.

**The second run does not redo the work.** By the time it starts, the successor
version from the first run is `LOCKED`, and rewriting its draft lines would hit
the 004 lock guard. It does not try: `ensure_target_version` returns the
publication state alongside the version, and a publication that is already
`COMMITTED` short-circuits the run to `ALREADY_PUBLISHED` — no recompute, no
second approval, no second commitment. The first run's answer is still the
right one, which is what idempotent means here. A successor that is `LOCKED`
while nothing was published is a different thing entirely: an earlier run
stopped part-way, and that fails with a message saying so rather than with a
trigger error that says nothing useful.

### Why a superseding plan version

The 004 lock guard refuses writes to a `LOCKED` version's lines, and its error
message says what to do instead: *create a superseding version*. So the drafts
land in a new `DRAFT` version, `<code>-R<revision>`, with
`supersedes_plan_version_id` pointing at the source — and it is that successor
the approver takes to `LOCKED`. The source stays exactly as it was approved.

The cube stays keyed on the **original** plan version code, with `revision` as
the overlay, so "the current FY2026 plan" remains one thing to query. Governance
answers who approved which revision; the cube answers what the plan is now.

### Retries: per activity, and split on meaning

The distinction is not how long an activity takes. It is what a failure *means*.

| Policy | Used for | Attempts | Why |
|---|---|---|---|
| `READ` | Context, snapshots, dirty set | 5 | A failed read is usually the network |
| `WRITE` | Drafts, publish, state changes | 8 | Same, with more patience, since each is idempotent |
| `COMMITMENT` | The Commitment Service | 5 | **Bounded on purpose.** Past a handful of attempts the right move is to undo the publish, not to keep knocking |
| `COMPENSATION` | Rollback and release | 30 | The opposite. Leaving the surfaces disagreeing is worse than taking twenty minutes |

Every policy names `PermanentRecomputeError` as non-retryable. The split is
made where the error is raised, in [`errors.py`](../src/fpa_project/recompute/errors.py):
a verdict that will be the same on every attempt (unknown plan version, not
`LOCKED`, a self-approval, HTTP 4xx) fails immediately; anything that is a
statement about the network, the disk or the hour is retried. HTTP 408 and 429
are 4xx but are about timing, so they retry.

### Determinism, and the proof

The workflow reads no database, opens no socket, calls no clock and generates
no randomness. Time comes from `workflow.now()`. The engine functions it calls
are pure, which is why it can call them directly. The activity imports sit
inside `workflow.unsafe.imports_passed_through()` so the sandbox does not
reload SQLAlchemy on every task.

The proof is mechanical, not a claim:
[`test_recompute_replay.py`](../tests/test_recompute_replay.py) runs Temporal's
replayer over four committed histories — approved, rejected, expired and
compensated — against the current code. Adding an activity call, reordering two
of them, or branching on something not in history all fail it. Re-record with
`make replay-record` when the workflow's *intended* shape changes; a replay
failure you did not intend is the test working.

## Tests

```bash
make test-unit    # no stack needed: engine, workflow, replay
make test         # everything, including the Commitment Service against Postgres
```

- [`test_recompute_engine.py`](../tests/test_recompute_engine.py) — the
  arithmetic, with no database.
- [`test_recompute_workflow.py`](../tests/test_recompute_workflow.py) — the
  workflow with every activity faked, on a time-skipping server, so the
  72-hour timer expires in milliseconds while still being the real timer.
  Covers approval, rejection, expiry, cancellation, the unlocked-version
  refusal, compensation, failed compensation, both halves of the second-shock
  decision, the progress query, revision reuse and the already-published
  short-circuit.
- [`test_recompute_replay.py`](../tests/test_recompute_replay.py) — determinism.
- [`test_commitment_service.py`](../tests/test_commitment_service.py) — the
  fixed contract. Skipped, not failed, when Postgres is not up.

## Configuration

| Variable | Default | What it does |
|---|---|---|
| `TEMPORAL_HOST` | `localhost:7233` | gRPC endpoint (compose sets `temporal:7233`) |
| `TEMPORAL_NAMESPACE` | `default` | |
| `TEMPORAL_TASK_QUEUE` | `fpa-recompute` | Must match between API and worker |
| `FPA_APPROVAL_TIMEOUT_HOURS` | `72` | How long a run parks before expiring |
| `FPA_PARTITION_SIZE` | `5000` | Dirty rows per child workflow |
| `FPA_COMMITMENT_URL` | `http://localhost:8100` | |
| `FPA_COMMITMENT_FAILURE_RATE` | `0` | Start-up failure rate; the runtime lever is the endpoint below |
| `FPA_COMMITMENT_SEED` | unset | Seeds the failure RNG so injected failures repeat |

The Commitment Service's failure rate is meant to be turned up on a running
service:

```bash
curl -X POST localhost:8100/admin/failure-rate \
     -H 'content-type: application/json' \
     -d '{"rate": 1.0, "mode": "error"}'      # or "timeout", or "mixed"
```

`error` returns 500 before doing anything. `timeout` does the work — the
reservation or release commits — and *then* hangs past any sane client
deadline, which is the harder case because the caller never learns its write
landed. `mixed` alternates at random, and is the default because the two break
different things.

## API

| Method | Path | |
|---|---|---|
| `GET` | `/api/v1/plan-versions/{code}` | State, and where it may go next |
| `POST` | `/api/v1/plan-versions/{code}/transition` | The governance state machine |
| `POST` | `/api/v1/reforecast` | Shock a driver; starts a run or folds into one |
| `GET` | `/api/v1/reforecast/{code}/progress` | The live query: phase and counters |
| `POST` | `/api/v1/reforecast/{code}/decision` | Approve or reject the parked run |
| `POST` | `/api/v1/reforecast/{code}/cancel` | Stop cleanly |

## Known limits

- **Driver propagation is structural, not numeric.** `calc_order_dag` gives the
  dependency edges, so the *dirty set* is real. But this codebase has no numeric
  form for each dependent driver's formula, so a downstream driver inherits the
  shock's ratio and the strength of the response lives in
  `plan_driver_binding.elasticity`. A production system would evaluate each
  dependent's formula here. The assumption is written into every line's
  derivation trace rather than hidden, so an auditor reads it instead of
  guessing it.
- **`reserve_revision` races on `max(revision) + 1`.** Two *different*
  re-forecasts starting at the same instant can collide; the unique index
  catches it, the activity retries, and the loser takes the next number. It is
  a retry, not a lost update, but it is a retry.
- **Folding in a second shock leaves an orphan successor version.** The new
  shock set gets a new key, so a new revision and a new successor; the old
  successor stays `DRAFT` with its draft lines, never approved and never
  published. That is arguably the honest record of a re-forecast that was
  superseded mid-flight, but nothing cleans it up, and a plan version with many
  folded-in shocks accumulates them.
- **Consecutive re-forecasts do not compose.** Every re-forecast recomputes
  from the frozen baseline with *its own* shocks only. Approve a `utilisation`
  shock, then a `heads` shock whose dirty set overlaps it, and the second
  publish replaces the first on the shared lines with numbers that do not
  include the first shock — while the first revision's commitments stay
  reserved. Found in end-to-end verification; not yet fixed, because the fix
  is a design choice (see the verification report).
- **Variance uses a zero FX leg** because a driver shock does not move rates and
  `plan_fx_rate` is pinned for the version. That is by construction, not by
  omission — but it means the bridge here does not exercise its FX path.

## Covenant check on submission, then approve, then lock

A re-forecast's covenants are checked by the system, never recorded by a
person. The run parks at AWAITING_SUBMISSION so the planner can review the
recomputed lines (`GET /plan-versions/<target_version_code>/lines`, each with
its derivation trace); submitting runs the check on the submitted draft. A
breach rejects the draft there and then (COVENANT_FAILED, no controller asked);
a pass moves it to IN_REVIEW. A controller then approves (APPROVED, nothing
locked or published) and the CFO, not the approver, locks it; only then does
the system publish:

```bash
make submit                  # planner
make approve                 # controller: IN_REVIEW -> APPROVED
make lock                    # CFO: APPROVED -> LOCKED, then publish + commit
```

`PUT /plan-versions/<code>/covenant` is refused on a re-forecast whoever calls
it (migration 023); it remains the controller's review for an original plan.
The browser shows the same steps on the Re-forecasts tab.

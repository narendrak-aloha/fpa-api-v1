"""The durable re-forecast.

Nothing in this module reads a database, opens a socket, looks at the clock or
generates a random number. Those are activities, and the reason is replay:
Temporal reconstructs a running workflow by executing this code again against
the recorded history, so anything that could answer differently the second time
would corrupt the run. Time comes from ``workflow.now()``, which replays; the
engine functions it calls are pure.

The shape of a run
------------------
    reserve a revision -> freeze the baseline -> resolve the dirty set
    -> fan out to children -> save drafts
    -> PARK for the planner to review the recomputed lines and submit them
    -> the system's covenant check: a breach rejects the draft there and then;
       a pass moves it DRAFT -> IN_REVIEW
    -> PARK for a controller to approve (IN_REVIEW -> APPROVED) or reject
    -> PARK for the CFO to lock (APPROVED -> LOCKED)
    -> verify LOCKED -> publish -> commit -> bridge

Three people, three separate parks. Each one computes nothing, stays stopped
until its person signals or its timer runs out, and survives a worker restart,
because it lives in Temporal's history rather than in a process holding a
connection open. Publishing is the system's step, taken only once a lock holds.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, ChildWorkflowError, FailureError

from .errors import PERMANENT, REFUSED
from .models import (
    ApprovalDecision, CovenantInput, DriverShock, PartitionInput, Partition, Progress,
    RecomputeInput, RecomputeResult, ResumeState,
)

with workflow.unsafe.imports_passed_through():
    from . import engine as calc
    from .activities import (
        commit_to_treasury, committed_shocks, compensate_commitments, compute_variance, discard_staged,
        evaluate_covenants, supersede_commitments,
        ensure_target_version, evaluate_partition, load_plan_context, mark_compensation_failed,
        open_review, open_run, publish_to_cube, record_approval, record_lock, record_rejection, record_submission,
        reserve_revision, resolve_dirty_set, snapshot_baseline,
        snapshot_drivers, snapshot_preimage, unpublish_revision, update_run, verify_publishable,
    )

# Retry policies, one per kind of work rather than one for everything. The
# distinction that matters is not how long an activity takes but what a failure
# means: a read that failed is probably the network, a business rule that failed
# is an answer.
READ = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
    non_retryable_error_types=[PERMANENT],
)
WRITE = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    maximum_interval=timedelta(seconds=60),
    maximum_attempts=8,
    non_retryable_error_types=[PERMANENT],
)
# The Commitment Service is deliberately given a *bounded* budget. Retrying it
# forever would be the wrong kind of patience: past a handful of attempts the
# right move is to stop and undo the publish, not to keep knocking.
COMMITMENT = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=20),
    maximum_attempts=5,
    non_retryable_error_types=[PERMANENT],
)
# Compensation gets the opposite treatment. Leaving the cube and the ledger
# disagreeing is worse than taking twenty minutes to reconcile them, so this
# one keeps going long after the others would have given up.
COMPENSATION = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=1.6,
    maximum_interval=timedelta(minutes=2),
    maximum_attempts=30,
)

SHORT = timedelta(seconds=30)
MEDIUM = timedelta(minutes=5)
# One partition of the dirty set. Generous, because the activity heartbeats and
# resumes: the timeout is a liveness check, not a guess at how long the work takes.
LONG = timedelta(minutes=30)
HEARTBEAT = timedelta(seconds=30)

# How many children run at once. Bounded so a large dirty set does not put
# thousands of pending child-workflow commands into one workflow task.
MAX_CONCURRENT_CHILDREN = 8

# The human gates, in the order the run reaches them: the signal that answers
# each one, the phase the run parks in, and the activity that records a yes.
GATES = (
    ("submit", "AWAITING_SUBMISSION", record_submission),
    ("approve", "AWAITING_APPROVAL", record_approval),
    ("lock", "AWAITING_LOCK", record_lock),
)
GATE_PHASES = tuple(phase for _, phase, _ in GATES)


@workflow.defn
class RecomputePartitionWorkflow:
    """One slice of the dirty set.

    A child rather than a plain activity so each partition has its own history
    and its own retry surface: a partition that keeps failing is visible as a
    failing execution in the UI instead of as an attempt counter buried in the
    parent.
    """

    @workflow.run
    async def run(self, payload: PartitionInput) -> int:
        result = await workflow.execute_activity(
            evaluate_partition,
            payload,
            start_to_close_timeout=LONG,
            heartbeat_timeout=HEARTBEAT,
            retry_policy=WRITE,
        )
        return result.rows_written


@workflow.defn
class PlanRecomputeWorkflow:
    """The parent: a driver shock in, a published and committed revision out."""

    def __init__(self) -> None:
        self._input: RecomputeInput | None = None
        self._context = None
        self._phase = "STARTING"
        self._dirty_rows = 0
        self._processed_rows = 0
        self._partitions_total = 0
        self._partitions_done = 0
        self._revision = 0
        self._target_version_code = ""
        # The latest decision sent to each gate, keyed by its signal name.
        self._decisions: dict[str, ApprovalDecision] = {}
        self._cancelled = False
        self._known_drivers: list[str] = []
        # Set by the update handler when a second shock is folded in; the
        # compute phase reads it and starts over with the combined set.
        self._restart_requested = False
        # Set when the reservation finds this re-forecast already published.
        self._settled = ""
        self._continued_runs = 0
        self._target_version_id = ""
        # Decisions the governance store refused while the run was parked.
        self._refusals: list[str] = []
        # The drivers *this run* was asked to move, as opposed to the
        # cumulative set it applies. A second shock is refused only if it
        # names one of these.
        self._requested: set[str] = set()

    # -- signals, queries and updates ------------------------------------
    # One signal per gate, each correlated to this run by its workflow id.
    # Deliberately tolerant of arriving early: a signal that lands before its
    # gate opens is kept and read when the run gets there, and is judged by the
    # governance store then. The API refuses to send one early, so a person
    # never pre-decides a gate they cannot see yet.
    @workflow.signal
    def submit(self, decision: ApprovalDecision) -> None:
        """The planner submits the recomputed draft for review."""
        self._decisions["submit"] = decision

    @workflow.signal
    def approve(self, decision: ApprovalDecision) -> None:
        """A controller approves or rejects the submitted plan."""
        self._decisions["approve"] = decision

    @workflow.signal
    def lock(self, decision: ApprovalDecision) -> None:
        """The CFO locks the approved plan, or declines to."""
        self._decisions["lock"] = decision

    @workflow.signal
    def cancel_run(self) -> None:
        """Ask for a clean stop.

        A separate signal rather than relying only on Temporal's own
        cancellation, so the API can stop a run that is parked on approval
        without the caller needing cancellation permissions on the namespace.
        """
        self._cancelled = True

    @workflow.query
    def progress(self) -> Progress:
        """Read-only, and safe to call at any point during the run.

        A query runs against the workflow's current state without appending to
        history, which is what makes it safe for a UI to poll.
        """
        shocks = self._input.shocks if self._input else []
        return Progress(
            phase=self._phase,
            dirty_rows=self._dirty_rows,
            processed_rows=self._processed_rows,
            partitions_total=self._partitions_total,
            partitions_done=self._partitions_done,
            revision=self._revision,
            target_version_code=self._target_version_code,
            approval_state=(
                "CANCELLED" if self._cancelled else "WAITING" if self._phase in GATE_PHASES
                else "DECIDED" if self._decisions else "NOT_YET"
            ),
            shocks=[s.as_list() for s in shocks],
            # Carried through continue-as-new, not workflow.info().attempt:
            # that is the retry count of the first task, and is 1 whether or
            # not the run has ever continued.
            continued_runs=self._continued_runs,
            refusals=list(self._refusals),
        )

    @workflow.update
    async def add_shock(self, shock: DriverShock) -> str:
        """Fold a second driver change into the run that is already going.

        A second shock is never silently dropped. Before the numbers have been
        put in front of a person, it is folded in: the shock set grows, the
        revision is re-reserved under the new key and the computation starts
        again from the frozen baseline. Restarting rather than patching is what
        keeps the published revision a function of its whole shock set.

        Once the run is parked on approval, the validator refuses instead.
        Changing the numbers underneath somebody who is in the middle of
        deciding on them would be worse than making the second shock wait.
        """
        assert self._input is not None
        self._input.shocks = calc.merge_shocks(self._input.shocks, [shock])
        if shock.request_key() not in self._input.requested_keys:
            self._input.requested_keys = sorted([*self._input.requested_keys, shock.request_key()])
        self._requested.add(shock.driver_code)
        self._restart_requested = True
        return (
            f"{shock.driver_code} {shock.from_value} -> {shock.to_value} folded in; "
            f"recomputing from the baseline with {len(self._input.shocks)} shocks"
        )

    @add_shock.validator
    def _validate_shock(self, shock: DriverShock) -> None:
        """Reject rather than accept-and-ignore.

        A validator that raises rejects the update without it ever reaching
        history, so the caller gets a real error and knows the shock did not
        land. Everything here is a question about the request or the run's
        current phase, both of which are deterministic workflow state.
        """
        if self._input is None or self._phase == "STARTING":
            raise ApplicationError("the run has not finished starting; retry in a moment")
        if self._cancelled:
            raise ApplicationError("this run is cancelling and will not take more shocks")
        if self._input.covenant_gate:
            raise ApplicationError(
                "this run is a controller-approved re-forecast request; its shock cannot change. "
                "Ask for a new re-forecast instead."
            )
        if self._phase in ("COVENANT_CHECK", *GATE_PHASES, "PUBLISHING", "COMMITTING", "COMPENSATING", "VARIANCE", "DONE"):
            raise ApplicationError(
                f"run is at {self._phase}: the draft is already in front of the planner or an approver. "
                "Decide on it, then start a new re-forecast for this shock."
            )
        if shock.from_value == 0:
            raise ApplicationError(f"{shock.driver_code}: a shock cannot start from zero")
        if self._known_drivers and shock.driver_code not in self._known_drivers:
            raise ApplicationError(f"{shock.driver_code!r} is not a driver in this plan's model")
        if shock.driver_code in self._requested:
            raise ApplicationError(
                f"{shock.driver_code!r} already moved in this run; "
                "cancel and start again to change it a second time"
            )

    # -- the run ----------------------------------------------------------
    @workflow.run
    async def run(self, payload: RecomputeInput) -> RecomputeResult:
        self._input = payload
        resume = payload.resume
        if resume:
            self._continued_runs = resume.continued_runs
            self._revision = resume.revision
            self._target_version_code = resume.target_version_code
            self._processed_rows = resume.processed_rows
            self._dirty_rows = resume.dirty_rows

        try:
            return await self._execute(payload, resume)
        except asyncio.CancelledError:
            # Temporal delivers cancellation as CancelledError. The cleanup has
            # to be shielded, because the scope this coroutine is running in is
            # already cancelled and any new command would be cancelled with it.
            # ContinueAsNewError is a BaseException and passes both handlers
            # untouched, which is what lets the fan-out carry on in a new run.
            await asyncio.shield(self._abandon("CANCELLED", "cancelled while running", self._target_version_id))
            raise
        except FailureError as exc:
            # Anything that got past its retry policy. The compensation paths
            # handle their own bookkeeping and never reach here; this is for
            # the rest, so a run that died at PUBLISHING does not sit in the
            # run table claiming to still be publishing.
            if self._phase not in ("DONE", "COMPENSATING"):
                await self._fail(str(exc))
            raise

    async def _fail(self, detail: str) -> None:
        """Close the run out after an unrecoverable failure.

        Best effort, and deliberately so: the run is already failing, and a
        second failure while recording the first would only bury it. Before
        the publish, the successor version is closed as well; otherwise it sits
        at IN_REVIEW with a PENDING approval that nothing will ever answer.
        """
        closes_successor = self._phase in (
            "SNAPSHOT", "RESOLVING_DIRTY_SET", "RECOMPUTING", "SAVING_DRAFT", "COVENANT_CHECK", *GATE_PHASES,
        )
        self._phase = "FAILED"
        once = RetryPolicy(maximum_attempts=3)
        # Each step is tried on its own: one that cannot be done must not stop
        # the others. A single try around all three once left a run mirrored as
        # RUNNING and its successor at DRAFT because the discard had failed.
        # The order and the commands are unchanged, so recorded histories replay.
        if self._context is not None and self._revision:
            try:
                await workflow.execute_activity(
                    discard_staged, args=[self._context.plan_version_code, self._revision],
                    start_to_close_timeout=MEDIUM, retry_policy=once,
                )
            except FailureError as cleanup_failure:
                workflow.logger.warning("could not discard the staged rows: %s", cleanup_failure)
        if closes_successor and self._target_version_id:
            try:
                await workflow.execute_activity(
                    record_rejection,
                    args=[self._target_version_id, "", f"run failed: {detail}"[:500],
                          workflow.info().workflow_id, False],
                    start_to_close_timeout=SHORT, retry_policy=once,
                )
            except FailureError as cleanup_failure:
                workflow.logger.warning("could not close the successor: %s", cleanup_failure)
        try:
            await self._mirror("FAILED", ended=True, detail=detail[:500])
        except FailureError as cleanup_failure:
            workflow.logger.warning("could not record the failure: %s", cleanup_failure)

    async def _execute(self, payload: RecomputeInput, resume: ResumeState | None) -> RecomputeResult:
        context = await workflow.execute_activity(
            load_plan_context, payload.plan_version_code,
            start_to_close_timeout=SHORT, retry_policy=READ,
        )
        # Kept on the instance so the cancellation and failure paths, which are
        # reached from outside _execute, can still clean up after themselves.
        self._context = context
        if context.state not in ("APPROVED", "LOCKED", "SUPERSEDED"):
            # A re-forecast rebases a plan somebody already agreed to. Rebasing
            # a draft is not a re-forecast, it is just editing. SUPERSEDED is
            # the same agreed plan once a re-forecast of it has published: the
            # line of re-forecasts stays keyed on it.
            raise ApplicationError(
                f"{payload.plan_version_code} is {context.state}; a re-forecast needs an APPROVED or LOCKED plan",
                type=PERMANENT, non_retryable=True,
            )

        if not resume:
            await workflow.execute_activity(
                open_run,
                args=[
                    workflow.info().workflow_id, workflow.info().first_execution_run_id,
                    context.plan_version_id, payload.requested_by,
                    [s.as_list() for s in payload.shocks],
                ],
                start_to_close_timeout=SHORT, retry_policy=WRITE,
            )

        # Cumulative: this run applies every driver move already in the
        # published plan, plus its own. Without this, approving a second
        # re-forecast would publish over the first with numbers that do not
        # include it. Merging is idempotent, so a continue-as-new run that
        # repeats this lands on the same set.
        self._requested.update(s.driver_code for s in payload.shocks)
        if not payload.requested_keys:
            payload.requested_keys = sorted(s.request_key() for s in payload.shocks)
        published = await workflow.execute_activity(
            committed_shocks, context.plan_version_id,
            start_to_close_timeout=SHORT, retry_policy=READ,
        )
        payload.shocks = calc.merge_shocks(published, payload.shocks)

        # The compute phase can be asked to start over by the update handler,
        # so it is a loop rather than a straight line.
        while True:
            self._restart_requested = False
            partitions, target_version_id = await self._compute(context, payload, resume)
            resume = None
            if self._settled:
                return await self._already_done()
            if not self._restart_requested:
                break
            self._phase = "RECOMPUTING"
            await workflow.execute_activity(
                discard_staged, args=[context.plan_version_code, self._revision],
                start_to_close_timeout=MEDIUM, retry_policy=WRITE,
            )

        if self._cancelled:
            return await self._abandon("CANCELLED", "cancelled before the approval gate", self._target_version_id)
        if not partitions:
            return await self._finish_empty(context)

        return await self._await_and_publish(context, payload, target_version_id)

    async def _compute(
        self, context, payload: RecomputeInput, resume: ResumeState | None
    ) -> tuple[list[Partition], str]:
        """Everything up to and including the draft lines."""
        self._phase = "SNAPSHOT"
        await self._mirror("RUNNING")

        # Reserved before any work, because the revision names the successor
        # version and tags the staged rows. Derived from the shock set, so
        # re-running the same re-forecast reuses it.
        self._revision = await workflow.execute_activity(
            reserve_revision,
            args=[
                context.plan_version_id, payload.idempotency_key(), workflow.info().workflow_id,
                [s.as_list() for s in payload.shocks],
            ],
            start_to_close_timeout=SHORT, retry_policy=WRITE,
        )
        target = await workflow.execute_activity(
            ensure_target_version, args=[context, self._revision, payload.requested_by],
            start_to_close_timeout=SHORT, retry_policy=WRITE,
        )
        self._target_version_code = target.plan_version_code
        target_version_id = target.plan_version_id
        self._target_version_id = target_version_id

        # This exact re-forecast has run before. Doing it again would mean
        # rewriting the draft lines of a version that is now LOCKED, which the
        # 004 guard refuses -- and rightly, because there is nothing to redo.
        # The already-published revision *is* the idempotent answer.
        if target.publication_state != "RESERVED":
            self._settled = target.publication_state
            return [], target_version_id
        if target.version_state not in ("DRAFT", "IN_REVIEW"):
            raise ApplicationError(
                f"{target.plan_version_code} is {target.version_state} but revision {self._revision} "
                "was never published; an earlier run stopped part-way and needs a look",
                type=PERMANENT, non_retryable=True,
            )

        await workflow.execute_activity(
            snapshot_baseline, context.plan_version_code,
            start_to_close_timeout=LONG, retry_policy=WRITE,
        )

        # workflow.now() replays to the same instant, so the effective date is
        # the same on a retry as it was on the first attempt.
        effective_date = workflow.now().date().isoformat()
        snapshot = await workflow.execute_activity(
            snapshot_drivers,
            args=[context.model_id, [s.driver_code for s in payload.shocks], effective_date],
            start_to_close_timeout=SHORT, retry_policy=READ,
        )
        self._known_drivers = snapshot.driver_codes

        self._phase = "RESOLVING_DIRTY_SET"
        # Formulas come from the snapshot, so each dependent's ratio is
        # evaluated rather than assumed; a history recorded before they were
        # snapshotted replays with none and inherits, as it always did.
        factors, shock_trace = calc.plan_factors(
            context.calc_order_dag, payload.shocks, snapshot.bindings, snapshot.formulas or None,
        )
        # The dirty set is the scope of what this run was asked to move. The
        # factors above still apply every shock, published ones included, so
        # each rewritten line carries all of them; a line that only an earlier
        # committed shock moved is already live at that value and stays.
        requested = [s for s in payload.shocks if s.request_key() in payload.requested_keys] or payload.shocks
        request_factors, _ = calc.plan_factors(
            context.calc_order_dag, requested, snapshot.bindings, snapshot.formulas or None,
        )
        accounts = calc.affected_accounts(request_factors)
        scopes = [list(group) for group in calc.dirty_scopes(request_factors)]

        partitions = await workflow.execute_activity(
            resolve_dirty_set,
            args=[context.plan_version_code, sorted(payload.scenario_codes), accounts, payload.partition_size, scopes],
            start_to_close_timeout=MEDIUM, retry_policy=READ,
        )
        self._partitions_total = len(partitions)
        self._dirty_rows = sum(partition.row_count for partition in partitions)
        self._partitions_done = resume.partitions_done if resume else 0
        self._processed_rows = resume.processed_rows if resume else 0

        self._phase = "RECOMPUTING"
        await self._mirror("RUNNING")

        partitions_this_run = 0
        pending = partitions[self._partitions_done:]
        while pending:
            if self._cancelled or self._restart_requested:
                break
            # A batch never runs past the per-run cap, so the cap is exact:
            # a run never starts more children than it was allowed.
            width = MAX_CONCURRENT_CHILDREN
            if payload.continue_after_partitions > 0:
                width = min(width, payload.continue_after_partitions - partitions_this_run)
            batch, pending = pending[:width], pending[width:]
            results = await asyncio.gather(*[
                workflow.execute_child_workflow(
                    RecomputePartitionWorkflow.run,
                    PartitionInput(
                        plan_version_code=context.plan_version_code,
                        target_version_id=target_version_id,
                        revision=self._revision,
                        partition=partition,
                        factors=factors,
                        shock_trace=shock_trace,
                        scopes=scopes,
                    ),
                    id=f"{workflow.info().workflow_id}-p{self._revision}-{partition.index}",
                    task_queue=workflow.info().task_queue,
                    retry_policy=WRITE,
                )
                for partition in batch
            ])
            self._processed_rows += sum(results)
            self._partitions_done += len(batch)
            partitions_this_run += len(batch)
            await self._mirror("RUNNING")

            # History grows with every child. When Temporal says it is getting
            # large, carry the cursor into a fresh run rather than letting one
            # execution accumulate without limit. Only ever here: continuing as
            # new while parked on approval would throw the wait away.
            bounded = payload.continue_after_partitions > 0 and partitions_this_run >= payload.continue_after_partitions
            if pending and (bounded or workflow.info().is_continue_as_new_suggested()):
                workflow.continue_as_new(
                    RecomputeInput(
                        plan_version_code=payload.plan_version_code,
                        shocks=payload.shocks,
                        requested_by=payload.requested_by,
                        scenario_codes=payload.scenario_codes,
                        approval_timeout_hours=payload.approval_timeout_hours,
                        partition_size=payload.partition_size,
                        continue_after_partitions=payload.continue_after_partitions,
                        covenant_gate=payload.covenant_gate,
                        request_id=payload.request_id,
                        covenant_companies=payload.covenant_companies,
                        covenant_months=payload.covenant_months,
                        requested_keys=payload.requested_keys,
                        resume=ResumeState(
                            revision=self._revision,
                            target_version_code=self._target_version_code,
                            partitions_done=self._partitions_done,
                            processed_rows=self._processed_rows,
                            dirty_rows=self._dirty_rows,
                            continued_runs=self._continued_runs + 1,
                        ),
                    )
                )

        self._phase = "SAVING_DRAFT"
        return partitions, target_version_id

    async def _await_and_publish(self, context, payload: RecomputeInput, target_version_id: str) -> RecomputeResult:
        # The planner reviews the recomputed draft and submits it; the system
        # checks the covenants; a controller approves it; the CFO locks it.
        # Each person is their own park, and a run that ends at one of them
        # never reaches the next.
        decision: ApprovalDecision | None = None
        for gate, phase, record in GATES:
            outcome = await self._await_gate(context, payload, target_version_id, gate, phase, record)
            if isinstance(outcome, RecomputeResult):
                return outcome
            decision = outcome
            if gate == "submit":
                breach = await self._check_covenants(context, payload, target_version_id)
                if breach is not None:
                    return breach
        assert decision is not None

        self._phase = "PUBLISHING"
        await self._mirror("PUBLISHING", decided_by=decision.decided_by, detail=f"locked by {decision.decided_by}")

        # Asked here, not assumed from the step above: between the approval and
        # this line somebody could have moved the version, and the assignment is
        # explicit that the workflow enforces the rule rather than trusting the
        # caller to have checked.
        draft_rows = await workflow.execute_activity(
            verify_publishable, args=[target_version_id, self._revision],
            start_to_close_timeout=SHORT, retry_policy=READ,
        )
        await workflow.execute_activity(
            snapshot_preimage, args=[context.plan_version_code, self._revision],
            start_to_close_timeout=LONG, retry_policy=WRITE,
        )
        published = await workflow.execute_activity(
            publish_to_cube,
            args=[context.plan_version_code, context.plan_version_id, self._revision, draft_rows],
            start_to_close_timeout=LONG, retry_policy=WRITE,
        )

        self._phase = "COMMITTING"
        await self._mirror("PUBLISHING", decided_by=decision.decided_by)
        try:
            commitments = await workflow.execute_activity(
                commit_to_treasury,
                args=[
                    context.plan_version_code, context.plan_version_id, self._revision,
                    payload.idempotency_key(),
                ],
                start_to_close_timeout=MEDIUM, retry_policy=COMMITMENT,
            )
        except ActivityError as exc:
            return await self._compensate(context, payload, decision, published, exc)

        # This revision reserves for everything the earlier ones did, because
        # its shock set is cumulative; release theirs so the ledger holds one
        # set of commitments, matching what the cube now shows. Released
        # *after* the new commit, so a failure here never leaves the plan
        # unfunded -- only, briefly, funded twice -- and retried under the
        # compensation policy until the ledger is clean.
        await workflow.execute_activity(
            supersede_commitments, args=[context.plan_version_id, self._revision],
            start_to_close_timeout=MEDIUM, retry_policy=COMPENSATION,
        )

        self._phase = "VARIANCE"
        await self._mirror("PUBLISHING", decided_by=decision.decided_by)
        await workflow.execute_activity(
            compute_variance,
            args=[target_version_id, context.plan_version_code, self._revision, sorted(payload.scenario_codes)],
            start_to_close_timeout=LONG, retry_policy=WRITE,
        )

        self._phase = "DONE"
        await self._mirror("COMPLETED", decided_by=decision.decided_by, ended=True)
        return RecomputeResult(
            outcome="PUBLISHED",
            revision=self._revision,
            target_version_code=self._target_version_code,
            dirty_rows=self._dirty_rows,
            published_rows=published,
            commitment_ids=commitments,
        )

    async def _check_covenants(self, context, payload: RecomputeInput, target_version_id: str) -> RecomputeResult | None:
        """The system's covenant check on the submitted draft. No person records this verdict.

        A breach rejects the submitted draft on the spot: the controller is
        never asked, and nothing can be approved, locked or published. A pass
        sets covenant_ok (as the service, which the database allows only when
        every check passed) and puts the draft IN_REVIEW for a controller.
        Returns the run's result on a breach, None on a pass.
        """
        self._phase = "COVENANT_CHECK"
        await self._mirror("RUNNING")
        verdict = await workflow.execute_activity(
            evaluate_covenants,
            CovenantInput(
                target_version_id=target_version_id,
                plan_version_code=context.plan_version_code,
                revision=self._revision,
                scenario_codes=sorted(payload.scenario_codes),
                run_id=workflow.info().first_execution_run_id,
                workflow_id=workflow.info().workflow_id,
                request_id=payload.request_id,
                companies=list(payload.covenant_companies),
                months=list(payload.covenant_months),
            ),
            start_to_close_timeout=MEDIUM, retry_policy=WRITE,
        )
        if not verdict.passed:
            return await self._covenant_breach(context, target_version_id, verdict.failures)
        await workflow.execute_activity(
            open_review,
            args=[target_version_id, workflow.info().workflow_id],
            start_to_close_timeout=SHORT, retry_policy=WRITE,
        )
        return None

    async def _await_gate(
        self, context, payload: RecomputeInput, target_version_id: str, gate: str, phase: str, record,
    ) -> ApprovalDecision | RecomputeResult:
        """Park until this gate's person answers, then record the answer.

        Returns the accepted decision, or the run's result when it ended here.

        A timer runs alongside the wait so the run cannot wait forever, and both
        outcomes are first-class: a decision resumes, an expiry ends the run
        cleanly with nothing published. wait_condition costs nothing while it
        waits -- no thread, no connection, just a timer in history.

        It is a loop because a decision can be refused: the requester trying to
        approve their own plan, someone without the role, the approver trying
        to lock too. A refusal is recorded and the run goes back to waiting
        against the gate's *original* deadline -- one bad signal must not be
        able to destroy a re-forecast, and a stream of them must not be able to
        extend the window either.
        """
        self._phase = phase
        await self._mirror(phase)
        deadline = workflow.now() + timedelta(hours=payload.approval_timeout_hours)
        while True:
            remaining = deadline - workflow.now()
            if remaining <= timedelta(0):
                return await self._reject(context, target_version_id, None, expired=True)
            try:
                await workflow.wait_condition(lambda: gate in self._decisions or self._cancelled, timeout=remaining)
            except asyncio.TimeoutError:
                return await self._reject(context, target_version_id, None, expired=True)

            if self._cancelled:
                return await self._abandon("CANCELLED", f"cancelled at {phase}", target_version_id)

            decision = self._decisions[gate]
            if gate == "submit" and not decision.approved:
                # The planner either submits the draft or cancels the run;
                # there is nobody else's decision to reject yet.
                refusal = "a draft is withdrawn by cancelling the run, not by declining to submit it"
            else:
                try:
                    if not decision.approved:
                        return await self._reject(context, target_version_id, decision, expired=False)
                    await workflow.execute_activity(
                        record,
                        args=[target_version_id, decision.decided_by, decision.comment, workflow.info().workflow_id],
                        start_to_close_timeout=SHORT, retry_policy=WRITE,
                    )
                    return decision
                except ActivityError as exc:
                    if not (isinstance(exc.cause, ApplicationError) and exc.cause.type == REFUSED):
                        raise
                    refusal = exc.cause.message
            self._refusals.append(refusal)
            # Only clear the decision we just tried: another signal may have
            # arrived while the activity ran, and that one deserves its own
            # turn rather than being wiped out with this one.
            if self._decisions.get(gate) is decision:
                del self._decisions[gate]
            workflow.logger.warning("decision refused at %s, still parked: %s", phase, refusal)
            await self._mirror(phase, detail=f"refused: {refusal}"[:500])

    async def _compensate(self, context, payload, decision, published: int, cause: Exception) -> RecomputeResult:
        """Undo the publish, because the commitment did not go through.

        The cube says the money is planned and the ledger does not agree. This
        is not a logging opportunity: one of the two has to move, and since the
        commitment is the surface we do not own, the cube is the one that
        rolls back.

        Order matters. Release first, then unpublish: an attempt that timed out
        may have reserved budget we never saw the id for, so the ledger is
        swept by key before the cube is put back.
        """
        self._phase = "COMPENSATING"
        await self._mirror("PUBLISHING", decided_by=decision.decided_by, detail=str(cause)[:500])
        try:
            await workflow.execute_activity(
                compensate_commitments,
                args=[context.plan_version_id, self._revision, payload.idempotency_key()],
                start_to_close_timeout=MEDIUM, retry_policy=COMPENSATION,
            )
            await workflow.execute_activity(
                unpublish_revision,
                args=[context.plan_version_code, context.plan_version_id, self._revision],
                start_to_close_timeout=LONG, retry_policy=COMPENSATION,
            )
        except (ActivityError, ChildWorkflowError) as compensation_failure:
            # Both surfaces are now in a state nobody chose. Write it down
            # where an operator will find it, then fail loudly: a run that
            # could not reconcile must not report success.
            await workflow.execute_activity(
                mark_compensation_failed,
                args=[context.plan_version_id, self._revision, str(compensation_failure)],
                start_to_close_timeout=SHORT, retry_policy=WRITE,
            )
            await self._mirror("FAILED", decided_by=decision.decided_by, ended=True,
                               detail="compensation failed; cube and commitment ledger disagree")
            raise ApplicationError(
                f"revision {self._revision} published but could not be committed or rolled back",
                type=PERMANENT, non_retryable=True,
            ) from compensation_failure

        # Found live: without this a finished, compensated run reported
        # COMPENSATING forever, and the UI's progress panel with it.
        self._phase = "DONE"
        await self._mirror("COMPENSATED", decided_by=decision.decided_by, ended=True,
                           detail=f"commitment service failed; revision {self._revision} rolled back")
        return RecomputeResult(
            outcome="COMPENSATED",
            revision=self._revision,
            target_version_code=self._target_version_code,
            dirty_rows=self._dirty_rows,
            published_rows=0,
            detail=f"published {published} rows, then rolled them back: {cause}",
        )

    async def _reject(self, context, target_version_id: str, decision, expired: bool) -> RecomputeResult:
        """A rejection, or a timer that ran out. Both end the run cleanly.

        Nothing was published and nothing was committed, because this is
        reached before either happens. The draft lines stay in Postgres on
        purpose: they are the record of what was turned down.
        """
        await workflow.execute_activity(
            record_rejection,
            args=[
                target_version_id,
                decision.decided_by if decision else "",
                decision.comment if decision else "",
                workflow.info().workflow_id,
                expired,
            ],
            start_to_close_timeout=SHORT, retry_policy=WRITE,
        )
        await workflow.execute_activity(
            discard_staged, args=[context.plan_version_code, self._revision],
            start_to_close_timeout=MEDIUM, retry_policy=WRITE,
        )
        self._phase = "DONE"
        outcome = "EXPIRED" if expired else "REJECTED"
        await self._mirror(
            outcome, ended=True,
            decided_by=decision.decided_by if decision else None,
            detail="no decision inside the approval window" if expired else (decision.comment if decision else ""),
        )
        return RecomputeResult(
            outcome=outcome,
            revision=self._revision,
            target_version_code=self._target_version_code,
            dirty_rows=self._dirty_rows,
            published_rows=0,
            detail="nothing published, nothing committed",
        )

    async def _covenant_breach(self, context, target_version_id: str, failures: list[str]) -> RecomputeResult:
        """The submitted draft broke a covenant: rejected there and then, never put before a controller.

        The successor is closed as REJECTED by the service identity and the
        staged rows are dropped; nothing is approved, locked, published or
        committed. The covenant_check rows stay as the record of why; the
        request mirrors the run to COVENANT_FAILED.
        """
        detail = ("COVENANT_BREACH: " + "; ".join(failures))[:500]
        await workflow.execute_activity(
            record_rejection,
            args=[target_version_id, "", detail, workflow.info().workflow_id, False],
            start_to_close_timeout=SHORT, retry_policy=WRITE,
        )
        await workflow.execute_activity(
            discard_staged, args=[context.plan_version_code, self._revision],
            start_to_close_timeout=MEDIUM, retry_policy=WRITE,
        )
        self._phase = "DONE"
        await self._mirror("REJECTED", ended=True, detail=detail)
        return RecomputeResult(
            outcome="COVENANT_BREACH",
            revision=self._revision,
            target_version_code=self._target_version_code,
            dirty_rows=self._dirty_rows,
            published_rows=0,
            detail=detail,
        )

    async def _abandon(self, outcome: str, detail: str, target_version_id: str | None = None) -> RecomputeResult:
        """Stop without leaving a half-published revision behind.

        Reached from the cancel signal and from Temporal cancellation. There is
        nothing to roll back in the cube, because cancellation is only accepted
        before the publish; what needs clearing is the staged rows, which would
        otherwise sit there looking like an approved revision waiting to go out.
        """
        if self._context is not None and self._revision:
            await workflow.execute_activity(
                discard_staged, args=[self._context.plan_version_code, self._revision],
                start_to_close_timeout=MEDIUM, retry_policy=WRITE,
            )
        if target_version_id:
            await workflow.execute_activity(
                record_rejection,
                args=[target_version_id, "", detail, workflow.info().workflow_id, False],
                start_to_close_timeout=SHORT, retry_policy=WRITE,
            )
        self._phase = "DONE"
        await self._mirror(outcome, ended=True, detail=detail)
        return RecomputeResult(
            outcome=outcome,
            revision=self._revision,
            target_version_code=self._target_version_code,
            dirty_rows=self._dirty_rows,
            published_rows=0,
            detail=detail,
        )

    async def _already_done(self) -> RecomputeResult:
        """This exact re-forecast has already been published.

        The second of two identical runs takes this path. It is what makes
        "run it twice, get the same state" true rather than merely likely:
        there is no second revision, no second set of draft lines and no second
        commitment, because the first run's answer is still the right one.
        """
        self._phase = "DONE"
        detail = f"revision {self._revision} is already {self._settled}; nothing to redo"
        await self._mirror("COMPLETED", ended=True, detail=detail)
        return RecomputeResult(
            outcome="ALREADY_PUBLISHED" if self._settled == "COMMITTED" else f"ALREADY_{self._settled}",
            revision=self._revision,
            target_version_code=self._target_version_code,
            dirty_rows=self._dirty_rows,
            published_rows=0,
            detail=detail,
        )

    async def _finish_empty(self, context) -> RecomputeResult:
        """No line in the plan responds to these drivers.

        Not an error: it is a real and useful answer, and it arrives without
        anyone having to approve nothing.
        """
        self._phase = "DONE"
        await self._mirror("COMPLETED", ended=True, detail="no plan lines are bound to the shocked drivers")
        return RecomputeResult(
            outcome="NO_OP",
            revision=self._revision,
            target_version_code=self._target_version_code,
            dirty_rows=0,
            published_rows=0,
            detail="no plan lines are bound to the shocked drivers",
        )

    async def _mirror(
        self, state: str, decided_by: str | None = None, detail: str | None = None, ended: bool = False
    ) -> None:
        """Copy the current phase into Postgres. Best effort by design.

        The workflow never reads this back, so a failure here must not fail the
        run. Two attempts, then carry on: the query is still the live truth and
        the history is still the record.
        """
        try:
            await workflow.execute_activity(
                update_run,
                args=[
                    workflow.info().first_execution_run_id, state, self._phase,
                    self._dirty_rows, self._processed_rows, decided_by, detail, ended,
                ],
                start_to_close_timeout=SHORT,
                retry_policy=RetryPolicy(maximum_attempts=2),
            )
        except (ActivityError, ApplicationError) as exc:
            workflow.logger.warning("could not mirror progress for %s: %s", workflow.info().workflow_id, exc)

"""The workflow's behaviour, with every activity replaced by a fake.

The activities are stubbed so these tests say something about the *workflow*:
what it does when a person rejects, when nobody answers, when the Commitment
Service will not take the call. Testing those against a real Postgres would
mostly be testing Postgres.

Time is skipped rather than waited, so the 72-hour approval timer expires in
milliseconds and the test still exercises the real timer rather than a shortened
one.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import uuid
from datetime import timedelta
from dataclasses import dataclass, field

import pytest
from temporalio import activity
from temporalio.client import WorkflowFailureError
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from fpa_project.recompute.errors import PERMANENT, refused
from fpa_project.recompute.models import (
    ApprovalDecision, CovenantInput, CovenantVerdict, DriverShock, Partition, PartitionInput, PartitionResult,
    PlanContext, DriverBinding, DriverSnapshot, RecomputeInput, TargetVersion,
)
from fpa_project.recompute.workflows import PlanRecomputeWorkflow, RecomputePartitionWorkflow
from fpa_project.identities import CFO, CONTROLLER, PLANNER

DAG = [
    {"driver": "heads", "depends_on": []},
    {"driver": "available_hours", "depends_on": ["heads"]},
    {"driver": "utilisation", "depends_on": ["available_hours"]},
]


@dataclass
class World:
    """What the fake activities did, and what they should do next."""

    calls: list[str] = field(default_factory=list)
    revisions: dict[str, int] = field(default_factory=dict)
    next_revision: int = 1
    partitions: int = 2
    plan_state: str = "LOCKED"
    publishable_state: str = "LOCKED"
    commit_fails: bool = False
    compensation_fails: bool = False
    partition_fails: bool = False
    covenant_passes: bool = True
    covenant_inputs: list = field(default_factory=list)
    discard_fails: bool = False
    successor_state: str = "DRAFT"
    publication_state: str = "RESERVED"
    published_shocks: list = field(default_factory=list)
    reserved_shocks: list = field(default_factory=list)
    gate: asyncio.Event | None = None
    approved_by: str = ""
    dirty_scopes: list = field(default_factory=list)
    partition_inputs: list = field(default_factory=list)

    def record(self, name: str) -> None:
        self.calls.append(name)

    def count(self, name: str) -> int:
        return self.calls.count(name)


def build_activities(world: World) -> list:
    """Fakes registered under the real activity names."""

    @activity.defn(name="load_plan_context")
    def load_plan_context(plan_version_code: str) -> PlanContext:
        world.record("load_plan_context")
        return PlanContext(
            plan_version_id="11111111-1111-1111-1111-111111111111",
            plan_version_code=plan_version_code,
            model_id="22222222-2222-2222-2222-222222222222",
            plan_year=2026,
            state=world.plan_state,
            requested_by=PLANNER,
            calc_order_dag=DAG,
        )

    @activity.defn(name="snapshot_drivers")
    def snapshot_drivers(model_id: str, driver_codes: list[str], effective_date: str) -> DriverSnapshot:
        world.record("snapshot_drivers")
        return DriverSnapshot(
            effective_date=effective_date,
            driver_codes=["heads", "available_hours", "utilisation", "bill_rate"],
            bindings=[DriverBinding("utilisation", "41000", "quantity", 1.0)],
        )

    @activity.defn(name="committed_shocks")
    def committed_shocks(plan_version_id: str) -> list[DriverShock]:
        world.record("committed_shocks")
        return list(world.published_shocks)

    @activity.defn(name="supersede_commitments")
    def supersede_commitments(plan_version_id: str, revision: int) -> list[str]:
        world.record("supersede_commitments")
        return []

    @activity.defn(name="reserve_revision")
    def reserve_revision(plan_version_id: str, idempotency_key: str, workflow_id: str, shocks: list | None = None) -> int:
        world.reserved_shocks.append(shocks)
        world.record("reserve_revision")
        # The real one is a unique index; this is the same promise in a dict.
        if idempotency_key not in world.revisions:
            world.next_revision += 1
            world.revisions[idempotency_key] = world.next_revision
        return world.revisions[idempotency_key]

    @activity.defn(name="ensure_target_version")
    def ensure_target_version(
        plan_context: PlanContext, revision: int, requested_by: str
    ) -> TargetVersion:
        world.record("ensure_target_version")
        return TargetVersion(
            plan_version_code=f"{plan_context.plan_version_code}-R{revision}",
            plan_version_id="33333333-3333-3333-3333-333333333333",
            version_state=world.successor_state,
            publication_state=world.publication_state,
            published_by="",
        )

    @activity.defn(name="snapshot_baseline")
    def snapshot_baseline(plan_version_code: str) -> int:
        world.record("snapshot_baseline")
        return 1_000

    @activity.defn(name="resolve_dirty_set")
    def resolve_dirty_set(
        plan_version_code: str, scenario_codes: list[str], accounts: list[str], target_size: int,
        scopes: list | None = None,
    ) -> list[Partition]:
        world.record("resolve_dirty_set")
        world.dirty_scopes.append(scopes)
        return [
            Partition(index=i, scenario_code="base", period_months=[f"2026-0{i + 1}-01"], row_count=100)
            for i in range(world.partitions)
        ]

    @activity.defn(name="evaluate_partition")
    async def evaluate_partition(payload: PartitionInput) -> PartitionResult:
        world.record("evaluate_partition")
        world.partition_inputs.append(payload)
        if world.partition_fails:
            raise ApplicationError("partition cannot be computed", type=PERMANENT, non_retryable=True)
        if world.gate is not None:
            await world.gate.wait()
        return PartitionResult(index=payload.partition.index, rows_written=100)

    @activity.defn(name="record_submission")
    def record_submission(target_version_id: str, submitted_by: str, comment: str, workflow_id: str) -> None:
        # The real one asks plan_state_transition: DRAFT -> IN_REVIEW is the planner's.
        if submitted_by != PLANNER:
            world.record("submission_refused")
            raise refused(f"{submitted_by} may not move a plan version from DRAFT to IN_REVIEW")
        world.record("record_submission")

    @activity.defn(name="open_review")
    def open_review(target_version_id: str, workflow_id: str) -> None:
        world.record("open_review")

    @activity.defn(name="record_approval")
    def record_approval(target_version_id: str, decided_by: str, comment: str, workflow_id: str) -> None:
        # The real one asks plan_state_transition and the plan_approval trigger;
        # the requester is the case that matters here.
        if decided_by == PLANNER:
            world.record("approval_refused")
            raise refused(f"segregation of duties: {decided_by} requested this re-forecast")
        world.approved_by = decided_by
        world.record("record_approval")

    @activity.defn(name="record_lock")
    def record_lock(target_version_id: str, locked_by: str, comment: str, workflow_id: str) -> None:
        if locked_by == world.approved_by:
            world.record("lock_refused")
            raise refused(f"segregation of duties: {locked_by} approved this plan and cannot also lock it")
        world.record("record_lock")

    @activity.defn(name="record_rejection")
    def record_rejection(
        target_version_id: str, decided_by: str, comment: str, workflow_id: str, expired: bool
    ) -> None:
        world.record("record_rejection_expired" if expired else "record_rejection")

    @activity.defn(name="verify_publishable")
    def verify_publishable(target_version_id: str, revision: int) -> int:
        world.record("verify_publishable")
        if world.publishable_state != "LOCKED":
            raise ApplicationError(
                f"refusing to publish: plan version is {world.publishable_state!r}, not LOCKED",
                type=PERMANENT, non_retryable=True,
            )
        return 200

    @activity.defn(name="snapshot_preimage")
    def snapshot_preimage(plan_version_code: str, revision: int) -> int:
        world.record("snapshot_preimage")
        return 200

    @activity.defn(name="publish_to_cube")
    def publish_to_cube(plan_version_code: str, plan_version_id: str, revision: int, expected_rows: int) -> int:
        world.record("publish_to_cube")
        return 200

    @activity.defn(name="unpublish_revision")
    def unpublish_revision(plan_version_code: str, plan_version_id: str, revision: int) -> int:
        world.record("unpublish_revision")
        if world.compensation_fails:
            raise ApplicationError("cube rollback failed")
        return 200

    @activity.defn(name="commit_to_treasury")
    def commit_to_treasury(
        plan_version_code: str, plan_version_id: str, revision: int, idempotency_key: str
    ) -> list[str]:
        world.record("commit_to_treasury")
        if world.commit_fails:
            raise ApplicationError("commitment service failed: 500")
        return ["cmt_one", "cmt_two"]

    @activity.defn(name="compensate_commitments")
    def compensate_commitments(plan_version_id: str, revision: int, idempotency_key: str) -> list[str]:
        world.record("compensate_commitments")
        if world.compensation_fails:
            raise ApplicationError("release failed")
        return []

    @activity.defn(name="mark_compensation_failed")
    def mark_compensation_failed(plan_version_id: str, revision: int, detail: str) -> None:
        world.record("mark_compensation_failed")

    @activity.defn(name="compute_variance")
    def compute_variance(
        plan_version_id: str, source_version_code: str, revision: int, scenario_codes: list[str]
    ) -> int:
        world.record("compute_variance")
        return len(scenario_codes)

    @activity.defn(name="open_run")
    def open_run(workflow_id: str, run_id: str, plan_version_id: str, requested_by: str, shocks: list) -> None:
        world.record("open_run")

    @activity.defn(name="update_run")
    def update_run(
        run_id: str, state: str, phase: str, dirty_rows: int, processed_rows: int,
        decided_by: str | None = None, detail: str | None = None, ended: bool = False,
    ) -> None:
        if ended:
            world.record(f"ended:{state}")

    @activity.defn(name="evaluate_covenants")
    def evaluate_covenants(payload: CovenantInput) -> CovenantVerdict:
        world.record("evaluate_covenants")
        world.covenant_inputs.append(payload)
        if world.covenant_passes:
            return CovenantVerdict(passed=True, checks=9, failures=[])
        return CovenantVerdict(passed=False, checks=9, failures=["GM_PCT_FLOOR (base): 26.02, needs >= 30"])

    @activity.defn(name="discard_staged")
    def discard_staged(plan_version_code: str, revision: int) -> None:
        world.record("discard_staged")
        if world.discard_fails:
            raise ApplicationError("cube unavailable", non_retryable=True)

    return [
        load_plan_context, snapshot_drivers, committed_shocks, supersede_commitments, reserve_revision,
        ensure_target_version, snapshot_baseline, resolve_dirty_set, evaluate_partition,
        record_submission, open_review, record_approval, record_lock, record_rejection, verify_publishable, snapshot_preimage,
        publish_to_cube, unpublish_revision, commit_to_treasury, compensate_commitments,
        mark_compensation_failed, compute_variance, open_run, update_run, discard_staged, evaluate_covenants,
    ]


def make_input(**overrides) -> RecomputeInput:
    payload = RecomputeInput(
        plan_version_code="PV-2026-0001",
        shocks=[DriverShock("utilisation", 0.75, 0.70)],
        requested_by=PLANNER,
        scenario_codes=["base"],
    )
    for key, value in overrides.items():
        setattr(payload, key, value)
    return payload


class Harness:
    """A time-skipping environment with a worker already running."""

    def __init__(self, world: World, cache_workflows: bool = True):
        self.world = world
        self.task_queue = f"test-{uuid.uuid4()}"
        # Without a cache no sticky queue is involved, so a restarted worker
        # takes the next task at once and rebuilds the run from history alone.
        self.cache = {} if cache_workflows else {"max_cached_workflows": 0}

    async def __aenter__(self):
        self.env = await WorkflowEnvironment.start_time_skipping()
        # The real activities are sync because they block on database drivers,
        # so the worker needs an executor here exactly as it does in production.
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=8)
        self.worker = Worker(
            self.env.client,
            task_queue=self.task_queue,
            workflows=[PlanRecomputeWorkflow, RecomputePartitionWorkflow],
            activities=build_activities(self.world),
            activity_executor=self.pool,
            **self.cache,
        )
        await self.worker.__aenter__()
        return self

    async def __aexit__(self, *exc):
        await self.worker.__aexit__(*exc)
        await self.env.shutdown()
        self.pool.shutdown(wait=False)

    async def restart_worker(self) -> None:
        """Stop the worker and start a fresh one on the same queue, as a crash and redeploy would."""
        await self.worker.__aexit__(None, None, None)
        self.worker = Worker(
            self.env.client,
            task_queue=self.task_queue,
            workflows=[PlanRecomputeWorkflow, RecomputePartitionWorkflow],
            activities=build_activities(self.world),
            activity_executor=self.pool,
            **self.cache,
        )
        await self.worker.__aenter__()

    async def start(self, payload: RecomputeInput | None = None, workflow_id: str | None = None):
        # The id matters beyond uniqueness: children are named after their
        # parent, so a history recorded under one id only replays under that
        # same id. record_history.py passes stable ones for exactly that reason.
        return await self.env.client.start_workflow(
            PlanRecomputeWorkflow.run,
            payload or make_input(),
            id=workflow_id or f"recompute-{uuid.uuid4()}",
            task_queue=self.task_queue,
        )


# --------------------------------------------------------------------------
# The happy path
# --------------------------------------------------------------------------
async def test_approved_run_publishes_commits_and_bridges():
    world = World()
    async with Harness(world) as harness:
        handle = await harness.start()
        await through_the_gates(handle)
        result = await handle.result()

    assert result.outcome == "PUBLISHED"
    assert result.published_rows == 200
    assert result.commitment_ids == ["cmt_one", "cmt_two"]
    # The order these happened in is the guarantee, not just that they happened:
    # recompute, then submit, approve and lock by three people, then publish.
    published = world.calls.index("publish_to_cube")
    assert (world.calls.index("evaluate_partition") < world.calls.index("record_submission")
            < world.calls.index("evaluate_covenants") < world.calls.index("open_review")
            < world.calls.index("record_approval") < world.calls.index("record_lock") < published)
    assert world.calls.index("verify_publishable") < published
    assert world.calls.index("snapshot_preimage") < published
    assert published < world.calls.index("commit_to_treasury")
    # The earlier revision's commitments go only after this one's are in.
    assert world.calls.index("commit_to_treasury") < world.calls.index("supersede_commitments")
    assert "unpublish_revision" not in world.calls


async def test_nothing_is_published_before_a_human_answers():
    world = World()
    async with Harness(world) as harness:
        handle = await harness.start()
        await _wait_for_phase(handle, "AWAITING_SUBMISSION")
        # The drafts are computed and the run is parked for the planner to
        # review them. Nothing downstream has run, which is the entire point.
        assert world.count("evaluate_partition") == 2
        assert "publish_to_cube" not in world.calls
        assert "commit_to_treasury" not in world.calls
        await through_the_gates(handle)
        await handle.result()


async def test_approval_neither_locks_nor_publishes():
    """APPROVED is not LOCKED: the run waits for the CFO, and publishes nothing meanwhile."""
    world = World()
    async with Harness(world) as harness:
        handle = await harness.start()
        await _wait_for_phase(handle, "AWAITING_SUBMISSION")
        await handle.signal(PlanRecomputeWorkflow.submit, ApprovalDecision(True, PLANNER))
        await _wait_for_phase(handle, "AWAITING_APPROVAL")
        await handle.signal(PlanRecomputeWorkflow.approve, ApprovalDecision(True, CONTROLLER))
        await _wait_for_phase(handle, "AWAITING_LOCK")
        assert "record_approval" in world.calls
        assert not {"record_lock", "verify_publishable", "publish_to_cube", "commit_to_treasury"} & set(world.calls)
        await handle.signal(PlanRecomputeWorkflow.lock, ApprovalDecision(True, CFO))
        result = await handle.result()
    assert result.outcome == "PUBLISHED"
    assert world.calls.index("record_lock") < world.calls.index("verify_publishable") < world.calls.index("publish_to_cube")


async def test_the_approver_cannot_also_lock():
    """The CFO holds the controller role too; approving and locking stay two people's decisions."""
    world = World()
    async with Harness(world) as harness:
        handle = await harness.start()
        await through_the_gates(handle, controller=CFO, cfo=CFO)
        progress = None
        for _ in range(100):
            progress = await handle.query(PlanRecomputeWorkflow.progress)
            if progress.refusals:
                break
            await asyncio.sleep(0.05)
        assert progress.phase == "AWAITING_LOCK"
        assert "cannot also lock" in progress.refusals[0]
        assert "publish_to_cube" not in world.calls
        await handle.signal(PlanRecomputeWorkflow.cancel_run)
        result = await handle.result()
    assert result.outcome == "CANCELLED"


async def test_declining_to_submit_is_refused_and_the_draft_stays_parked():
    world = World()
    async with Harness(world) as harness:
        handle = await harness.start()
        await _wait_for_phase(handle, "AWAITING_SUBMISSION")
        await handle.signal(PlanRecomputeWorkflow.submit, ApprovalDecision(False, PLANNER))
        progress = None
        for _ in range(100):
            progress = await handle.query(PlanRecomputeWorkflow.progress)
            if progress.refusals:
                break
            await asyncio.sleep(0.05)
        assert progress.phase == "AWAITING_SUBMISSION"
        assert "cancelling the run" in progress.refusals[0]
        await through_the_gates(handle)
        result = await handle.result()
    assert result.outcome == "PUBLISHED"


async def test_the_cfo_declining_to_lock_publishes_nothing():
    world = World()
    async with Harness(world) as harness:
        handle = await harness.start()
        for phase, signal, decision in (
            ("AWAITING_SUBMISSION", PlanRecomputeWorkflow.submit, ApprovalDecision(True, PLANNER)),
            ("AWAITING_APPROVAL", PlanRecomputeWorkflow.approve, ApprovalDecision(True, CONTROLLER)),
            ("AWAITING_LOCK", PlanRecomputeWorkflow.lock, ApprovalDecision(False, CFO, "not this quarter")),
        ):
            await _wait_for_phase(handle, phase)
            await handle.signal(signal, decision)
        result = await handle.result()
    assert result.outcome == "REJECTED"
    assert "record_lock" not in world.calls and "publish_to_cube" not in world.calls
    assert "record_rejection" in world.calls and "discard_staged" in world.calls


@pytest.mark.parametrize("gate", ["AWAITING_SUBMISSION", "AWAITING_APPROVAL", "AWAITING_LOCK"])
async def test_a_worker_restart_while_parked_resumes_at_the_same_gate(gate):
    """The worker dies while a person is deciding; a new one picks the run up where it stood."""
    world = World()
    async with Harness(world, cache_workflows=False) as harness:
        handle = await harness.start()
        steps = [
            ("AWAITING_SUBMISSION", PlanRecomputeWorkflow.submit, PLANNER),
            ("AWAITING_APPROVAL", PlanRecomputeWorkflow.approve, CONTROLLER),
            ("AWAITING_LOCK", PlanRecomputeWorkflow.lock, CFO),
        ]
        for phase, signal, who in steps:
            await _wait_for_phase(handle, phase)
            if phase == gate:
                # The decision is sent to the restarted worker. Reaching the
                # next gate and publishing is the proof it resumed from here.
                await harness.restart_worker()
            await handle.signal(signal, ApprovalDecision(True, who))
        result = await handle.result()
    assert result.outcome == "PUBLISHED"
    # Nothing ran twice because of the restart: one recompute, one of each gate, one publish.
    assert world.count("evaluate_partition") == 2
    for name in ("record_submission", "record_approval", "record_lock", "publish_to_cube"):
        assert world.count(name) == 1, name


# --------------------------------------------------------------------------
# Rejection, expiry and cancellation
# --------------------------------------------------------------------------
async def test_rejection_publishes_nothing_and_ends_cleanly():
    world = World()
    async with Harness(world) as harness:
        handle = await harness.start()
        await _wait_for_phase(handle, "AWAITING_SUBMISSION")
        await handle.signal(PlanRecomputeWorkflow.submit, ApprovalDecision(True, PLANNER))
        await _wait_for_phase(handle, "AWAITING_APPROVAL")
        await handle.signal(
            PlanRecomputeWorkflow.approve,
            ApprovalDecision(approved=False, decided_by=CONTROLLER, comment="margin is wrong"),
        )
        result = await handle.result()

    # A rejection is an outcome, not an error: the workflow completes.
    assert result.outcome == "REJECTED"
    assert result.published_rows == 0
    assert "publish_to_cube" not in world.calls
    assert "commit_to_treasury" not in world.calls
    assert "record_rejection" in world.calls
    assert "discard_staged" in world.calls
    assert "ended:REJECTED" in world.calls


async def test_the_approval_timer_expires_the_run():
    world = World()
    async with Harness(world) as harness:
        # Nobody signals. The environment skips the 72 hours rather than
        # waiting them, so the real timer is what fires.
        handle = await harness.start(make_input(approval_timeout_hours=72))
        result = await handle.result()

    assert result.outcome == "EXPIRED"
    assert "publish_to_cube" not in world.calls
    assert "record_rejection_expired" in world.calls
    assert "ended:EXPIRED" in world.calls


async def test_cancel_while_parked_leaves_nothing_published():
    world = World()
    async with Harness(world) as harness:
        handle = await harness.start()
        await _wait_for_phase(handle, "AWAITING_SUBMISSION")
        await handle.signal(PlanRecomputeWorkflow.cancel_run)
        result = await handle.result()

    assert result.outcome == "CANCELLED"
    assert "publish_to_cube" not in world.calls
    assert "commit_to_treasury" not in world.calls
    # The staged rows go, so nothing is left looking like an approved revision.
    assert "discard_staged" in world.calls


async def test_a_refused_approval_keeps_the_run_parked():
    """A self-approval is refused, and the run waits for a valid decision.

    Found against the real stack: before the fix, the refusal was a permanent
    activity failure and it failed the whole workflow, so anyone could destroy
    a parked re-forecast by sending one bad signal.
    """
    world = World()
    async with Harness(world) as harness:
        handle = await harness.start()
        await _wait_for_phase(handle, "AWAITING_SUBMISSION")
        await handle.signal(PlanRecomputeWorkflow.submit, ApprovalDecision(True, PLANNER))
        await _wait_for_phase(handle, "AWAITING_APPROVAL")
        await handle.signal(PlanRecomputeWorkflow.approve, ApprovalDecision(True, PLANNER))

        progress = None
        for _ in range(100):
            progress = await handle.query(PlanRecomputeWorkflow.progress)
            if progress.refusals:
                break
            await asyncio.sleep(0.05)
        assert progress.phase == "AWAITING_APPROVAL"
        assert progress.approval_state == "WAITING"
        assert "segregation of duties" in progress.refusals[0]
        assert "publish_to_cube" not in world.calls

        await handle.signal(PlanRecomputeWorkflow.approve, ApprovalDecision(True, CONTROLLER))
        await _wait_for_phase(handle, "AWAITING_LOCK")
        await handle.signal(PlanRecomputeWorkflow.lock, ApprovalDecision(True, CFO))
        result = await handle.result()

    assert result.outcome == "PUBLISHED"
    assert world.calls.index("approval_refused") < world.calls.index("record_approval")


async def test_cancel_while_recomputing_closes_the_successor():
    """Found against the real stack: a pre-gate cancel left the successor at
    DRAFT, an orphan nothing would ever close."""
    world = World(partitions=1)
    world.gate = asyncio.Event()
    async with Harness(world) as harness:
        handle = await harness.start()
        await _wait_for_phase(handle, "RECOMPUTING")
        await handle.signal(PlanRecomputeWorkflow.cancel_run)
        world.gate.set()
        result = await handle.result()

    assert result.outcome == "CANCELLED"
    assert "record_submission" not in world.calls
    assert "record_rejection" in world.calls
    assert "discard_staged" in world.calls
    assert "publish_to_cube" not in world.calls


# --------------------------------------------------------------------------
# Only a locked version publishes
# --------------------------------------------------------------------------
async def test_an_unlocked_version_is_refused_at_the_gate():
    world = World(publishable_state="IN_REVIEW")
    async with Harness(world) as harness:
        handle = await harness.start()
        await _wait_for_phase(handle, "AWAITING_SUBMISSION")
        await through_the_gates(handle)
        with pytest.raises(WorkflowFailureError):
            await handle.result()

    # The check is the workflow's own, and it runs even though the approval
    # step just moved the version itself.
    assert "verify_publishable" in world.calls
    assert "publish_to_cube" not in world.calls
    assert "commit_to_treasury" not in world.calls
    # The run does not sit in the run table claiming to still be publishing,
    # and the staged rows do not sit there looking ready to go out.
    assert "ended:FAILED" in world.calls
    assert "discard_staged" in world.calls


async def test_a_draft_source_plan_is_refused_before_any_work():
    world = World(plan_state="DRAFT")
    async with Harness(world) as harness:
        handle = await harness.start()
        with pytest.raises(WorkflowFailureError):
            await handle.result()
    assert "snapshot_baseline" not in world.calls


# --------------------------------------------------------------------------
# The counterparty that is not correct
# --------------------------------------------------------------------------
async def test_a_failing_commitment_rolls_the_publish_back():
    world = World(commit_fails=True)
    async with Harness(world) as harness:
        handle = await harness.start()
        await _wait_for_phase(handle, "AWAITING_SUBMISSION")
        await through_the_gates(handle)
        result = await handle.result()
        # A finished run says so; it must not keep reporting COMPENSATING.
        assert (await handle.query(PlanRecomputeWorkflow.progress)).phase == "DONE"

    assert result.outcome == "COMPENSATED"
    assert result.published_rows == 0
    # The ledger is swept before the cube is restored: an attempt that timed
    # out may have reserved budget whose id we never saw.
    assert world.calls.index("compensate_commitments") < world.calls.index("unpublish_revision")
    assert "ended:COMPENSATED" in world.calls
    # The commitment was retried under its own bounded policy before giving up.
    assert world.count("commit_to_treasury") > 1


async def test_a_failing_compensation_fails_the_run_loudly():
    world = World(commit_fails=True, compensation_fails=True)
    async with Harness(world) as harness:
        handle = await harness.start()
        await _wait_for_phase(handle, "AWAITING_SUBMISSION")
        await through_the_gates(handle)
        with pytest.raises(WorkflowFailureError):
            await handle.result()

    # A run that could not reconcile the two surfaces must not report success,
    # and the disagreement is written where an operator will find it.
    assert "mark_compensation_failed" in world.calls
    assert "ended:FAILED" in world.calls


async def test_a_failed_discard_does_not_skip_the_rest_of_the_cleanup():
    world = World(partition_fails=True, discard_fails=True)
    async with Harness(world) as harness:
        handle = await harness.start()
        with pytest.raises(WorkflowFailureError):
            await handle.result()

    # Each cleanup step stands alone: the discard failing must still leave the
    # successor closed and the run recorded as FAILED, not RUNNING and DRAFT.
    assert "discard_staged" in world.calls
    assert "record_rejection" in world.calls
    assert "ended:FAILED" in world.calls


# --------------------------------------------------------------------------
# The covenant gate of a question-driven request
# --------------------------------------------------------------------------
PL_H2 = dict(
    covenant_gate=True, request_id="44444444-4444-4444-4444-444444444444",
    covenant_companies=["RTPL1", "RTPL2", "RTPL3"],
    covenant_months=["2026-07-01", "2026-08-01", "2026-09-01", "2026-10-01", "2026-11-01", "2026-12-01"],
)


async def test_a_passing_covenant_check_on_submission_goes_to_the_controller_and_publishes():
    world = World()
    async with Harness(world) as harness:
        handle = await harness.start(make_input(**PL_H2))
        await _wait_for_phase(handle, "AWAITING_SUBMISSION")
        # The planner reviews the recomputed draft before any covenant is measured
        assert "evaluate_covenants" not in world.calls
        await through_the_gates(handle)
        result = await handle.result()

    assert result.outcome == "PUBLISHED"
    # Measured by the system once submitted, and only a pass reaches the controller
    order = [world.calls.index(name) for name in
             ("evaluate_partition", "record_submission", "evaluate_covenants", "open_review", "record_approval")]
    assert order == sorted(order)
    checked = world.covenant_inputs[0]
    assert checked.companies == PL_H2["covenant_companies"] and checked.request_id == PL_H2["request_id"]


async def test_a_covenant_breach_rejects_the_submitted_draft_before_any_controller_sees_it():
    world = World(covenant_passes=False)
    async with Harness(world) as harness:
        handle = await harness.start(make_input(**PL_H2))
        await _wait_for_phase(handle, "AWAITING_SUBMISSION")
        await handle.signal(PlanRecomputeWorkflow.submit, ApprovalDecision(True, PLANNER))
        result = await handle.result()

    assert result.outcome == "COVENANT_BREACH"
    assert result.published_rows == 0 and "GM_PCT_FLOOR" in result.detail
    # Submitted, then rejected by the system: never in review, nothing published or committed
    assert world.calls.index("record_submission") < world.calls.index("evaluate_covenants")
    assert not {"open_review", "record_approval", "record_lock", "publish_to_cube", "commit_to_treasury"} & set(world.calls)
    assert "record_rejection" in world.calls and "discard_staged" in world.calls
    assert "ended:REJECTED" in world.calls


async def test_every_run_has_its_covenants_checked_by_the_system():
    """A manual run too: no person records the covenant verdict; the whole plan is measured."""
    world = World()
    async with Harness(world) as harness:
        handle = await harness.start()
        await through_the_gates(handle)
        await handle.result()
    assert world.count("evaluate_covenants") == 1
    assert world.covenant_inputs[0].companies == [] and world.covenant_inputs[0].request_id == ""


async def test_a_request_run_takes_no_second_shock():
    world = World(partitions=1)
    world.gate = asyncio.Event()
    async with Harness(world) as harness:
        handle = await harness.start(make_input(**PL_H2))
        await _wait_for_phase(handle, "RECOMPUTING")
        with pytest.raises(Exception) as refused_update:
            await handle.execute_update(PlanRecomputeWorkflow.add_shock, DriverShock("heads", 100, 98))
        world.gate.set()
        await handle.cancel()
    assert "re-forecast request" in str(refused_update.value.__cause__ or refused_update.value)


# --------------------------------------------------------------------------
# A second shock arriving mid-run
# --------------------------------------------------------------------------
async def test_a_second_shock_is_folded_in_while_recomputing():
    world = World(partitions=1)
    world.gate = asyncio.Event()
    async with Harness(world) as harness:
        handle = await harness.start()
        await _wait_for_phase(handle, "RECOMPUTING")

        message = await handle.execute_update(
            PlanRecomputeWorkflow.add_shock, DriverShock("bill_rate", 200.0, 210.0)
        )
        assert "folded in" in message
        world.gate.set()

        await _wait_for_phase(handle, "AWAITING_SUBMISSION")
        await through_the_gates(handle)
        await handle.result()

    # Folding in restarts the computation from the baseline under a new key,
    # so the published revision is a function of its whole shock set.
    assert world.count("discard_staged") >= 1
    assert len(world.revisions) == 2


async def test_a_second_shock_is_refused_once_a_human_is_deciding():
    world = World()
    async with Harness(world) as harness:
        handle = await harness.start()
        await _wait_for_phase(handle, "AWAITING_SUBMISSION")

        # Refused, not ignored: the caller gets an error naming the reason.
        with pytest.raises(Exception) as caught:
            await handle.execute_update(
                PlanRecomputeWorkflow.add_shock, DriverShock("bill_rate", 200.0, 210.0)
            )
        assert "approver" in str(caught.value.cause)

        await through_the_gates(handle)
        result = await handle.result()
    # The run published the shock set the approver actually saw.
    assert result.outcome == "PUBLISHED"


async def test_a_shock_naming_an_unknown_driver_is_refused():
    world = World()
    world.gate = asyncio.Event()
    async with Harness(world) as harness:
        handle = await harness.start()
        await _wait_for_phase(handle, "RECOMPUTING")
        with pytest.raises(Exception) as caught:
            await handle.execute_update(
                PlanRecomputeWorkflow.add_shock, DriverShock("not_a_driver", 1.0, 2.0)
            )
        assert "not a driver" in str(caught.value.cause)
        world.gate.set()
        await through_the_gates(handle)
        await handle.result()


async def test_reshocking_the_same_driver_is_refused():
    world = World()
    world.gate = asyncio.Event()
    async with Harness(world) as harness:
        handle = await harness.start()
        await _wait_for_phase(handle, "RECOMPUTING")
        with pytest.raises(Exception) as caught:
            await handle.execute_update(
                PlanRecomputeWorkflow.add_shock, DriverShock("utilisation", 0.70, 0.65)
            )
        assert "already moved" in str(caught.value.cause)
        world.gate.set()
        await through_the_gates(handle)
        await handle.result()


# --------------------------------------------------------------------------
# Progress and idempotence
# --------------------------------------------------------------------------
async def test_progress_reports_the_phase_and_the_counters_while_running():
    world = World(partitions=3)
    async with Harness(world) as harness:
        handle = await harness.start()
        await _wait_for_phase(handle, "AWAITING_SUBMISSION")
        progress = await handle.query(PlanRecomputeWorkflow.progress)

        assert progress.phase == "AWAITING_SUBMISSION"
        assert progress.dirty_rows == 300
        assert progress.processed_rows == 300
        assert progress.partitions_total == 3
        assert progress.approval_state == "WAITING"
        assert progress.shocks == [["utilisation", 0.75, 0.70]]

        await through_the_gates(handle)
        await handle.result()


async def test_the_same_shock_twice_reuses_one_revision():
    """The idempotence guarantee, at the level the workflow owns it.

    Two separate executions with identical inputs derive the same key, so the
    reservation hands back the same revision and the second run republishes the
    same rows instead of stacking a new revision on top.
    """
    world = World()
    async with Harness(world) as harness:
        for _ in range(2):
            handle = await harness.start()
            await _wait_for_phase(handle, "AWAITING_SUBMISSION")
            await through_the_gates(handle)
            result = await handle.result()
            assert result.revision == 2

    assert len(world.revisions) == 1
    assert world.count("publish_to_cube") == 2


async def test_a_reforecast_applies_the_published_shocks_as_well_as_its_own():
    """Found in end-to-end verification: approving heads after utilisation
    published a heads-only plan over the utilisation one."""
    world = World(published_shocks=[DriverShock("utilisation", 0.75, 0.70)])
    async with Harness(world) as harness:
        handle = await harness.start(make_input(shocks=[DriverShock("heads", 100, 110)]))
        await _wait_for_phase(handle, "AWAITING_SUBMISSION")
        progress = await handle.query(PlanRecomputeWorkflow.progress)
        assert progress.shocks == [["heads", 100, 110], ["utilisation", 0.75, 0.70]]
        await through_the_gates(handle)
        await handle.result()
    assert world.reserved_shocks[0] == [["heads", 100, 110], ["utilisation", 0.75, 0.70]]


async def test_the_dirty_set_is_what_was_asked_for_and_the_values_carry_everything():
    """Found live: after an unscoped utilisation move was committed, a
    Poland-only request rewrote every utilisation line in the plan. Those lines
    are already live at the committed value; only the requested slice is
    dirty, and each of its lines still carries both moves."""
    world = World(published_shocks=[DriverShock("utilisation", 0.75, 0.70)])
    poland = DriverShock("utilisation", 0.70, 0.65, ["RTPL1"], ["2026-07-01"])
    async with Harness(world) as harness:
        handle = await harness.start(make_input(shocks=[poland]))
        await _wait_for_phase(handle, "AWAITING_SUBMISSION")
        await through_the_gates(handle)
        await handle.result()
    assert world.dirty_scopes[0] == [[["41000"], ["RTPL1"], ["2026-07-01"]]]
    # The children still get the factors of both shocks, scoped and unscoped.
    factors = world.partition_inputs[0].factors
    assert {(tuple(f.companies), tuple(f.months)) for f in factors} == {((), ()), (("RTPL1",), ("2026-07-01",))}
    assert world.partition_inputs[0].scopes == [[["41000"], ["RTPL1"], ["2026-07-01"]]]


async def test_a_committed_driver_moved_again_keeps_its_baseline():
    world = World(published_shocks=[DriverShock("utilisation", 0.75, 0.70)])
    async with Harness(world) as harness:
        handle = await harness.start(make_input(shocks=[DriverShock("utilisation", 0.70, 0.65)]))
        await _wait_for_phase(handle, "AWAITING_SUBMISSION")
        progress = await handle.query(PlanRecomputeWorkflow.progress)
        # One move, baseline to the new target: 0.75 -> 0.65, not 0.70 -> 0.65 on top.
        assert progress.shocks == [["utilisation", 0.75, 0.65]]
        await handle.signal(PlanRecomputeWorkflow.cancel_run)
        await handle.result()


async def test_an_already_published_reforecast_does_not_redo_itself():
    """The second of two identical runs short-circuits.

    The successor version is LOCKED by now, so rewriting its draft lines would
    hit the 004 lock guard -- and there is nothing to rewrite anyway. The
    already-published revision is the idempotent answer.
    """
    world = World(successor_state="LOCKED", publication_state="COMMITTED")
    async with Harness(world) as harness:
        handle = await harness.start()
        result = await handle.result()

    assert result.outcome == "ALREADY_PUBLISHED"
    assert result.revision == 2
    # No recompute, no second approval, no second publish, no second commitment.
    assert "evaluate_partition" not in world.calls
    assert "record_submission" not in world.calls
    assert "publish_to_cube" not in world.calls
    assert "commit_to_treasury" not in world.calls


async def test_a_successor_locked_without_a_publish_is_refused():
    """An earlier run stopped somewhere it should not have.

    Redoing the drafts would fail against the lock guard with a trigger message
    that says nothing useful, so this refuses with one that does.
    """
    world = World(successor_state="LOCKED", publication_state="RESERVED")
    async with Harness(world) as harness:
        handle = await harness.start()
        with pytest.raises(WorkflowFailureError) as caught:
            await handle.result()
    assert "stopped part-way" in str(caught.value.cause)
    assert "evaluate_partition" not in world.calls


def test_the_idempotency_key_ignores_ordering_but_not_values():
    first = RecomputeInput(
        plan_version_code="PV-2026-0001",
        shocks=[DriverShock("utilisation", 0.75, 0.70), DriverShock("heads", 100, 110)],
        requested_by=PLANNER,
        scenario_codes=["base", "stretch"],
    )
    reordered = RecomputeInput(
        plan_version_code="PV-2026-0001",
        shocks=[DriverShock("heads", 100, 110), DriverShock("utilisation", 0.75, 0.70)],
        requested_by=CONTROLLER,  # who asked is not part of the identity
        scenario_codes=["stretch", "base"],
    )
    different = RecomputeInput(
        plan_version_code="PV-2026-0001",
        shocks=[DriverShock("utilisation", 0.75, 0.65)],
        requested_by=PLANNER,
        scenario_codes=["base", "stretch"],
    )
    assert first.idempotency_key() == reordered.idempotency_key()
    assert first.idempotency_key() != different.idempotency_key()


async def through_the_gates(handle, planner=PLANNER, controller=CONTROLLER, cfo=CFO) -> None:
    """The three people, in order: the planner submits, a controller approves, the CFO locks."""
    for phase, signal, who in (
        ("AWAITING_SUBMISSION", PlanRecomputeWorkflow.submit, planner),
        ("AWAITING_APPROVAL", PlanRecomputeWorkflow.approve, controller),
        ("AWAITING_LOCK", PlanRecomputeWorkflow.lock, cfo),
    ):
        await _wait_for_phase(handle, phase)
        await handle.signal(signal, ApprovalDecision(True, who))


async def _wait_for_phase(handle, phase: str, timeout: float = 30.0) -> None:
    """Poll the progress query until the run reaches a phase.

    Polling the query rather than sleeping is what keeps these tests honest:
    it is the same read-only mechanism the UI uses.
    """
    from temporalio.service import RPCError

    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        try:
            progress = await handle.query(PlanRecomputeWorkflow.progress, rpc_timeout=timedelta(seconds=2))
        except RPCError:
            # A query that lands while one execution closes and its
            # continue-as-new successor starts can miss both. The UI poller
            # just asks again, and so does this.
            await asyncio.sleep(0.05)
            continue
        if progress.phase == phase:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"workflow never reached {phase}")


async def test_forced_continue_as_new_preserves_partition_cursor():
    world = World(partitions=20)
    async with Harness(world) as harness:
        handle = await harness.start(make_input(continue_after_partitions=5))
        await _wait_for_phase(handle, "AWAITING_SUBMISSION")
        progress = await handle.query(PlanRecomputeWorkflow.progress)
        assert progress.partitions_done == 20
        # 20 partitions at 5 per run: four executions, three continuations.
        assert progress.continued_runs == 3
        # The history of the execution that parked holds only its own five
        # children, not all twenty: that is the bound continue-as-new buys.
        history = await handle.fetch_history()
        started = [e for e in history.events if e.HasField("start_child_workflow_execution_initiated_event_attributes")]
        assert len(started) == 5
        await through_the_gates(handle)
        result = await handle.result()
    assert result.outcome == "PUBLISHED"
    assert world.count("evaluate_partition") == 20

"""Record workflow histories for the replay test.

    python -m tests.record_history

Runs the workflow against the time-skipping test server with the same fakes
the workflow tests use, then writes each execution's history to
``tests/histories/``. Those files are committed, and ``test_recompute_replay``
runs the current code against them in CI.

Each file is named after the workflow id it was recorded under, and that is
load-bearing rather than tidy: child workflows are named after their parent, so
a history replayed under a different id fails as a nondeterminism error that
has nothing to do with the code being wrong.

Re-record when the workflow's *intended* shape changes. If a replay fails and
you did not mean to change the shape, that is the test doing its job: the fix
belongs in the code, not in the recording.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from temporalio.client import WorkflowFailureError

from fpa_project.recompute.models import ApprovalDecision
from fpa_project.recompute.workflows import PlanRecomputeWorkflow

from .test_recompute_workflow import Harness, World, _wait_for_phase, make_input, through_the_gates
from fpa_project.identities import CONTROLLER, PLANNER

HISTORIES = Path(__file__).parent / "histories"


async def record_approved(workflow_id: str) -> dict:
    """The full path: submit, approve, lock, publish, commit, bridge."""
    async with Harness(World()) as harness:
        handle = await harness.start(workflow_id=workflow_id)
        await through_the_gates(handle)
        await handle.result()
        return await _history(handle)


async def record_rejected(workflow_id: str) -> dict:
    """A controller rejects the submitted plan, which is its own branch through the code."""
    async with Harness(World()) as harness:
        handle = await harness.start(workflow_id=workflow_id)
        await _wait_for_phase(handle, "AWAITING_SUBMISSION")
        await handle.signal(PlanRecomputeWorkflow.submit, ApprovalDecision(True, PLANNER))
        await _wait_for_phase(handle, "AWAITING_APPROVAL")
        await handle.signal(
            PlanRecomputeWorkflow.approve,
            ApprovalDecision(approved=False, decided_by=CONTROLLER, comment="margin is wrong"),
        )
        await handle.result()
        return await _history(handle)


async def record_expired(workflow_id: str) -> dict:
    """The timer firing, which is the branch nobody exercises by hand."""
    async with Harness(World()) as harness:
        handle = await harness.start(make_input(approval_timeout_hours=72), workflow_id=workflow_id)
        await handle.result()
        return await _history(handle)


async def record_compensated(workflow_id: str) -> dict:
    """Publish, fail the commitment, roll the publish back."""
    async with Harness(World(commit_fails=True)) as harness:
        handle = await harness.start(workflow_id=workflow_id)
        await through_the_gates(handle)
        try:
            await handle.result()
        except WorkflowFailureError:
            pass
        return await _history(handle)


async def record_covenant_passed(workflow_id: str) -> dict:
    """A question-driven request: covenants pass, submitted, approved, locked, published."""
    from .test_recompute_workflow import PL_H2

    async with Harness(World()) as harness:
        handle = await harness.start(make_input(**PL_H2), workflow_id=workflow_id)
        await through_the_gates(handle)
        await handle.result()
        return await _history(handle)


async def record_covenant_breach(workflow_id: str) -> dict:
    """A question-driven request whose submitted draft breaks a covenant: rejected there, no controller asked."""
    from .test_recompute_workflow import PL_H2

    async with Harness(World(covenant_passes=False)) as harness:
        handle = await harness.start(make_input(**PL_H2), workflow_id=workflow_id)
        await _wait_for_phase(handle, "AWAITING_SUBMISSION")
        await handle.signal(PlanRecomputeWorkflow.submit, ApprovalDecision(True, PLANNER))
        await handle.result()
        return await _history(handle)


RECORDERS = {
    "recompute-replay-approved": record_approved,
    "recompute-replay-rejected": record_rejected,
    "recompute-replay-expired": record_expired,
    "recompute-replay-compensated": record_compensated,
    "recompute-replay-covenant-passed": record_covenant_passed,
    "recompute-replay-covenant-breach": record_covenant_breach,
}


async def _history(handle) -> dict:
    return (await handle.fetch_history()).to_json_dict()


async def main() -> None:
    """Record every history, or only the ones named: python -m tests.record_history NAME..."""
    import sys

    HISTORIES.mkdir(exist_ok=True)
    wanted = set(sys.argv[1:])
    for workflow_id, recorder in RECORDERS.items():
        if wanted and workflow_id not in wanted:
            continue
        history = await recorder(workflow_id)
        path = HISTORIES / f"{workflow_id}.json"
        path.write_text(json.dumps(history, indent=2, sort_keys=True))
        print(f"  recorded {path.name} ({len(history.get('events', []))} events)")


if __name__ == "__main__":
    asyncio.run(main())

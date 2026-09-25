"""How the API talks to a running re-forecast.

One workflow id per plan version, ``recompute-<plan version code>``. That is
the whole coordination mechanism: a second shock arriving while a re-forecast
is already going cannot start a competing run, because Temporal will not let
two executions share an id. It reaches the running one through the update
handler instead, which is what the assignment asks for and what stops two
workflows racing to publish the same plan.
"""

from __future__ import annotations

import os
from dataclasses import asdict
from typing import Any

from temporalio.client import Client, WorkflowHandle
from temporalio.service import RPCError

from fpa_project.config import approval_timeout_hours, partition_size, temporal
from .models import ApprovalDecision, DriverShock, Progress, RecomputeInput
from .workflows import PlanRecomputeWorkflow

_client: Client | None = None


def workflow_id(plan_version_code: str) -> str:
    return f"recompute-{plan_version_code}"


async def connect() -> Client:
    """One client per process, reconnected lazily if it is ever dropped."""
    global _client
    if _client is None:
        settings = temporal()
        _client = await Client.connect(settings.host, namespace=settings.namespace)
    return _client


async def start(
    plan_version_code: str,
    shocks: list[DriverShock],
    requested_by: str,
    scenario_codes: list[str] | None = None,
) -> dict[str, Any]:
    """Start a re-forecast, or fold the shock into the one already running.

    The caller does not have to know which of those happened before calling.
    Silently ignoring the second shock is the one outcome the assignment rules
    out, so both paths report which they took.
    """
    if scenario_codes is not None and set(scenario_codes) != {"base", "stretch", "downside"}:
        raise ValueError("scenario-subset reforecast is unsupported; supply all plan scenarios")
    client = await connect()
    settings = temporal()
    payload = RecomputeInput(
        plan_version_code=plan_version_code,
        shocks=shocks,
        requested_by=requested_by,
        scenario_codes=scenario_codes or ["base", "stretch", "downside"],
        approval_timeout_hours=approval_timeout_hours(),
        partition_size=partition_size(),
        continue_after_partitions=int(os.getenv("FPA_CONTINUE_AFTER_PARTITIONS", "1000")),
    )
    identifier = workflow_id(plan_version_code)
    try:
        handle = await client.start_workflow(
            PlanRecomputeWorkflow.run,
            payload,
            id=identifier,
            task_queue=settings.task_queue,
        )
        return {"workflow_id": handle.id, "run_id": handle.result_run_id, "action": "STARTED"}
    except Exception as exc:  # WorkflowAlreadyStartedError, by any import path
        if type(exc).__name__ != "WorkflowAlreadyStartedError":
            raise
        return await fold_in(plan_version_code, shocks)


class RunInProgress(RuntimeError):
    """Another re-forecast of this plan version is still running."""


async def start_request(
    plan_version_code: str,
    shock: DriverShock,
    requested_by: str,
    request_id: str,
) -> dict[str, Any]:
    """Start the run a controller approved for a re-forecast request.

    Unlike ``start``, this never folds into a run that is already going: the
    controller approved this shock on its own, so merging it into somebody
    else's run would publish numbers nobody approved together. The run checks
    covenants on the request's own scope before anyone is asked to approve.
    """
    client = await connect()
    settings = temporal()
    payload = RecomputeInput(
        plan_version_code=plan_version_code,
        shocks=[shock],
        requested_by=requested_by,
        scenario_codes=["base", "stretch", "downside"],
        approval_timeout_hours=approval_timeout_hours(),
        partition_size=partition_size(),
        continue_after_partitions=int(os.getenv("FPA_CONTINUE_AFTER_PARTITIONS", "1000")),
        covenant_gate=True,
        request_id=request_id,
        covenant_companies=list(shock.companies),
        covenant_months=list(shock.months),
    )
    try:
        handle = await client.start_workflow(
            PlanRecomputeWorkflow.run, payload, id=workflow_id(plan_version_code), task_queue=settings.task_queue,
        )
    except Exception as exc:  # WorkflowAlreadyStartedError, by any import path
        if type(exc).__name__ != "WorkflowAlreadyStartedError":
            raise
        raise RunInProgress(
            f"another re-forecast of {plan_version_code} is still running; decide or cancel it first"
        ) from exc
    return {"workflow_id": handle.id, "run_id": handle.result_run_id, "action": "STARTED"}


async def fold_in(plan_version_code: str, shocks: list[DriverShock]) -> dict[str, Any]:
    """Send shocks to a run that is already going, through the update handler.

    The update is synchronous on purpose: the validator either accepts the
    shock or rejects it, and the caller gets that answer rather than a
    fire-and-forget acknowledgement of something that may have been refused.
    """
    handle = (await connect()).get_workflow_handle(workflow_id(plan_version_code))
    messages = []
    for shock in shocks:
        messages.append(await handle.execute_update(PlanRecomputeWorkflow.add_shock, shock))
    return {"workflow_id": handle.id, "action": "FOLDED_IN", "detail": messages}


# The run's human gates: the signal for each, and the phase the run parks in
# while it waits for it.
GATE_SIGNALS = {
    "submit": (PlanRecomputeWorkflow.submit, "AWAITING_SUBMISSION"),
    "approve": (PlanRecomputeWorkflow.approve, "AWAITING_APPROVAL"),
    "lock": (PlanRecomputeWorkflow.lock, "AWAITING_LOCK"),
}


async def decide(plan_version_code: str, approved: bool, decided_by: str, comment: str = "", gate: str = "approve") -> None:
    """Send a human decision to one of the run's gates.

    A signal rather than an update: the decision is a fact that has happened,
    not a request that can be refused. Whether it is *allowed* -- segregation
    of duties -- is settled by the database when the workflow acts on it, which
    is the only place that can answer it truthfully.
    """
    signal, _ = GATE_SIGNALS[gate]
    handle = (await connect()).get_workflow_handle(workflow_id(plan_version_code))
    await handle.signal(signal, ApprovalDecision(approved=approved, decided_by=decided_by, comment=comment))


async def cancel(plan_version_code: str) -> None:
    """Ask the run to stop cleanly.

    The signal, not ``handle.cancel()``: the workflow catches both, but the
    signal lets it reach its own cleanup path at a point it chose rather than
    wherever the cancellation happened to land.
    """
    handle = (await connect()).get_workflow_handle(workflow_id(plan_version_code))
    await handle.signal(PlanRecomputeWorkflow.cancel_run)


async def progress(plan_version_code: str) -> dict[str, Any] | None:
    """The live phase and counters, or None when there is no such run.

    Queries do not append to history, so a UI can poll this while the run is
    still going without making the history it is reporting on any larger.
    """
    handle: WorkflowHandle = (await connect()).get_workflow_handle(workflow_id(plan_version_code))
    try:
        result: Progress = await handle.query(PlanRecomputeWorkflow.progress)
    except RPCError as exc:
        if "not found" in str(exc).lower():
            return None
        raise
    return asdict(result)

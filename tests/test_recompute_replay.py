"""Replay a recorded history against the current workflow code.

This is the only mechanical proof that the workflow is deterministic. "I was
careful not to call the clock" is not one: the replayer takes a history that a
real execution produced and runs today's code against it, and any command that
comes out in a different order than the history says fails the test.

It is also a regression test against future edits. Reordering two activity
calls, adding one before an existing one, or making a branch depend on
something that is not in history will all fail here, which is exactly when you
want to find out -- rather than when a production run is mid-flight and the
worker deploys underneath it.

The histories live in ``tests/histories/`` and are committed. Re-record them
with::

    python -m tests.record_history
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from temporalio.client import WorkflowHistory
from temporalio.worker import Replayer

from fpa_project.recompute.workflows import PlanRecomputeWorkflow, RecomputePartitionWorkflow

HISTORIES = Path(__file__).parent / "histories"


def history_files() -> list[Path]:
    return sorted(HISTORIES.glob("*.json"))


@pytest.mark.parametrize("path", history_files(), ids=lambda p: p.stem)
async def test_recorded_history_replays_against_current_code(path: Path):
    replayer = Replayer(workflows=[PlanRecomputeWorkflow, RecomputePartitionWorkflow])
    await replayer.replay_workflow(
        WorkflowHistory.from_json(path.stem, json.loads(path.read_text()))
    )


def test_histories_are_committed():
    """A replay suite with no histories in it proves nothing.

    Without this, deleting the recordings would turn the test above into zero
    parametrised cases and the suite would still go green.
    """
    assert history_files(), (
        "no recorded histories in tests/histories/; "
        "run `python -m tests.record_history` to record them"
    )

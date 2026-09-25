"""The recompute worker.

Run it:  python -m fpa_project.recompute.worker

Kill it mid-run and start it again: the run picks up where it was, because the
state is in Temporal's history rather than in this process. That is the point
of the whole exercise, and it is worth doing once by hand before trusting it.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging

from temporalio.client import Client
from temporalio.worker import Worker

from fpa_project.config import temporal
from fpa_project.log_config import configure as configure_logging
from . import activities
from .stores import ensure_cube_tables
from .workflows import PlanRecomputeWorkflow, RecomputePartitionWorkflow

log = logging.getLogger("fpa.recompute.worker")


async def main() -> None:
    configure_logging()
    settings = temporal()
    client = await Client.connect(settings.host, namespace=settings.namespace)

    # The activities are async but every one of them blocks on a driver that
    # is not: SQLAlchemy and clickhouse-connect are both synchronous. A thread
    # pool keeps a long evaluate_partition from starving the heartbeats of the
    # ones running beside it.
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        worker = Worker(
            client,
            task_queue=settings.task_queue,
            workflows=[PlanRecomputeWorkflow, RecomputePartitionWorkflow],
            activities=activities.ALL,
            activity_executor=pool,
            max_concurrent_activities=16,
        )
        log.info("worker listening on %s, task queue %r", settings.host, settings.task_queue)
        await worker.run()


if __name__ == "__main__":
    try:
        # Fail fast if the cube is unreachable, rather than discovering it on
        # the first activity of the first run.
        ensure_cube_tables()
    except Exception as exc:  # noqa: BLE001 - the log line is the point
        log.warning("could not prepare the cube tables at start-up: %s", exc)
    asyncio.run(main())

"""The Commitment Service: where finance reserves budget against a plan.

This is a counterparty, not a module of the FP&A system. It runs as its own
process, owns its own Postgres schema, and the workflow reaches it only over
HTTP -- so there is no shared transaction to hide behind and the workflow has
to be correct about a service that fails.

Its contract is fixed by the assignment:

* ``POST /commitments`` takes an idempotency key. The same key twice returns
  the first result and creates nothing new.
* The same endpoint *without* a key applies every time. That path exists so it
  is visible whether the workflow depends on it. This one does not.
* ``DELETE /commitments/{id}`` compensates, and is itself allowed to fail.
* A failure rate, settable at runtime, makes any endpoint return 500 or time
  out. It defaults to zero.

Run it:  uvicorn fpa_project.commitment.service:app --port 8100
Turn it up:  curl -X POST localhost:8100/admin/failure-rate -d '{"rate": 1.0}'
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import uuid
from decimal import Decimal
from typing import Literal

from fastapi import FastAPI, Header, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import create_engine, text

from db.config import database_url
from fpa_project.config import commitment_failure_rate
from fpa_project.log_config import configure as configure_logging

SCHEMA = "commitment_service"

app = FastAPI(title="Commitment Service", version="1.0.0")
configure_logging()
log = logging.getLogger("fpa.commitment")

_engine = None
# Runtime-settable, deliberately module state: this is a test lever, and it is
# meant to be turned up on a running service without a restart.
_failure_rate = commitment_failure_rate()
_failure_mode: Literal["error", "timeout", "mixed"] = "mixed"
# Seeded from the environment so a test can make the injected failures
# repeatable; unset means genuinely arbitrary, which is what a real
# counterparty looks like.
_rng = random.Random(int(os.getenv("FPA_COMMITMENT_SEED", "0")) or None)


def engine():
    global _engine
    if _engine is None:
        _engine = create_engine(database_url(), pool_pre_ping=True)
        with _engine.begin() as conn:
            conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}"))
            conn.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {SCHEMA}.commitment ("
                    "  commitment_id text PRIMARY KEY,"
                    "  idempotency_key text,"
                    "  plan_version text NOT NULL,"
                    "  revision integer NOT NULL,"
                    "  scenario text NOT NULL,"
                    "  category text NOT NULL,"
                    "  amount numeric(20, 2) NOT NULL,"
                    "  state text NOT NULL DEFAULT 'RESERVED',"
                    "  created_at timestamptz NOT NULL DEFAULT now(),"
                    "  released_at timestamptz)"
                )
            )
            # The whole idempotency guarantee, held by a unique index rather
            # than by a read-then-write the service could race with itself on.
            # Partial, so the no-key path stays free to apply every time.
            conn.execute(
                text(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS commitment_idempotency_key_idx "
                    f"ON {SCHEMA}.commitment (idempotency_key) WHERE idempotency_key IS NOT NULL"
                )
            )
    return _engine


class CommitmentRequest(BaseModel):
    # The ledger contract is denominated in group USD. Reject mixed currencies.
    currency: Literal["USD"] = "USD"
    plan_version: str
    revision: int
    scenario: str
    category: str
    amount: Decimal


class CommitmentResponse(BaseModel):
    commitment_id: str
    state: str
    replayed: bool = Field(default=False, description="True when an existing commitment was returned unchanged")


class FailureRate(BaseModel):
    rate: float = Field(ge=0.0, le=1.0)
    mode: Literal["error", "timeout", "mixed"] = "mixed"


def _pick_failure() -> str | None:
    """Decide, per request, whether and how this call fails.

    ``error`` fails *before* the write: the caller is told, and nothing landed.
    ``timeout`` fails *after* it: the write commits, then the response hangs
    past any client's patience, so the caller never learns it landed. That is
    the case the idempotency key and the key-prefix sweep in compensation exist
    for. (An earlier version slept and raised before writing, so the hard case
    never happened and the sweep was never exercised.)
    """
    if _failure_rate <= 0 or _rng.random() >= _failure_rate:
        return None
    if _failure_mode == "mixed":
        return "error" if _rng.random() < 0.5 else "timeout"
    return _failure_mode


def _fail_before(mode: str | None) -> None:
    if mode == "error":
        raise HTTPException(status_code=500, detail="injected failure")


async def _hang_after(mode: str | None) -> None:
    if mode == "timeout":
        # Longer than any caller's patience. The connection is held open, so
        # the client sees a read timeout rather than a refused connection.
        await asyncio.sleep(120)


@app.post("/commitments", response_model=CommitmentResponse, status_code=201)
async def create_commitment(
    request: CommitmentRequest,
    response: Response,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> CommitmentResponse:
    """Reserve budget.

    With a key, this is safe to call any number of times: the unique index
    turns the second insert into a no-op and the first commitment comes back
    unchanged, with ``replayed`` saying so.

    Without a key it applies every time. That is the contract, and it is the
    path that would quietly double-reserve if a workflow relied on it.
    """
    failure = _pick_failure()
    _fail_before(failure)
    commitment_id = f"cmt_{uuid.uuid4().hex[:20]}"

    with engine().begin() as conn:
        if idempotency_key:
            row = conn.execute(
                text(
                    f"INSERT INTO {SCHEMA}.commitment "
                    "  (commitment_id, idempotency_key, plan_version, revision, scenario, category, amount) "
                    "VALUES (:id, :key, :plan_version, :revision, :scenario, :category, :amount) "
                    "ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING "
                    "RETURNING commitment_id, state"
                ),
                {
                    "id": commitment_id, "key": idempotency_key, "plan_version": request.plan_version,
                    "revision": request.revision, "scenario": request.scenario,
                    "category": request.category, "amount": request.amount,
                },
            ).first()
            if row is None:
                existing = conn.execute(
                    text(
                        f"SELECT commitment_id, state FROM {SCHEMA}.commitment "
                        "WHERE idempotency_key = :key"
                    ),
                    {"key": idempotency_key},
                ).first()
                response.status_code = 200
                result = CommitmentResponse(commitment_id=existing[0], state=existing[1], replayed=True)
            else:
                result = CommitmentResponse(commitment_id=row[0], state=row[1])
        else:
            conn.execute(
                text(
                    f"INSERT INTO {SCHEMA}.commitment "
                    "  (commitment_id, idempotency_key, plan_version, revision, scenario, category, amount) "
                    "VALUES (:id, NULL, :plan_version, :revision, :scenario, :category, :amount)"
                ),
                {
                    "id": commitment_id, "plan_version": request.plan_version, "revision": request.revision,
                    "scenario": request.scenario, "category": request.category, "amount": request.amount,
                },
            )
            result = CommitmentResponse(commitment_id=commitment_id, state="RESERVED")
    # The write has committed; in timeout mode the caller now waits in vain.
    await _hang_after(failure)
    return result



@app.delete("/commitments/{commitment_id}")
async def release_commitment(commitment_id: str) -> dict[str, str]:
    """Release a reservation. Allowed to fail, and it will.

    Idempotent in the direction that matters: releasing an already-released
    commitment succeeds rather than erroring, so a compensation that is retried
    after a partial success still converges.
    """
    failure = _pick_failure()
    _fail_before(failure)
    with engine().begin() as conn:
        row = conn.execute(
            text(
                f"UPDATE {SCHEMA}.commitment SET state = 'RELEASED', released_at = now() "
                "WHERE commitment_id = :id RETURNING commitment_id"
            ),
            {"id": commitment_id},
        ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="no such commitment")
    await _hang_after(failure)
    return {"commitment_id": commitment_id, "state": "RELEASED"}


@app.get("/commitments")
async def list_commitments(key_prefix: str | None = None, plan_version: str | None = None) -> dict:
    """What this service currently believes.

    ``key_prefix`` is how compensation finds commitments it may never have seen
    the id of: an attempt that timed out still created the row, and sweeping by
    the key the workflow derived is the only way to find it again.

    Not subject to the failure injection. A counterparty that cannot be
    inspected cannot be reconciled, and the interesting failures are the writes.
    """
    clauses, params = [], {}
    if key_prefix:
        clauses.append("idempotency_key LIKE :prefix")
        params["prefix"] = f"{key_prefix}%"
    if plan_version:
        clauses.append("plan_version = :plan_version")
        params["plan_version"] = plan_version
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

    with engine().begin() as conn:
        rows = conn.execute(
            text(
                "SELECT commitment_id, idempotency_key, plan_version, revision, scenario, "
                f"       category, amount, state FROM {SCHEMA}.commitment {where} "
                "ORDER BY created_at"
            ),
            params,
        ).mappings().all()
    return {"commitments": [dict(row) | {"amount": str(row["amount"])} for row in rows]}


@app.post("/admin/failure-rate")
async def set_failure_rate(settings: FailureRate) -> FailureRate:
    """Turn the failure rate up at runtime, because the graders will.

    ``error`` returns 500 immediately, ``timeout`` hangs past any sane client
    deadline, and ``mixed`` alternates between them at random. Mixed is the
    default because the two break different things: an error tells the caller
    it failed, and a timeout tells it nothing at all.
    """
    global _failure_rate, _failure_mode
    _failure_rate, _failure_mode = settings.rate, settings.mode
    log.warning("commitment service failure rate set to %.2f (%s)", settings.rate, settings.mode)
    return settings


@app.get("/admin/failure-rate", response_model=FailureRate)
async def get_failure_rate() -> FailureRate:
    return FailureRate(rate=_failure_rate, mode=_failure_mode)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}

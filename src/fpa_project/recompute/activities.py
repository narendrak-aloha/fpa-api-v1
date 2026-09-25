"""Every read and write the recompute performs.

The workflow calls these and does no I/O of its own. Each one is written to be
safe to run twice, because Temporal will run it twice: an activity that times
out after its work committed is retried, and the second attempt has to be a
no-op rather than a second application.

The idempotence technique differs per surface and is noted on each activity.
Broadly: Postgres uses ``ON CONFLICT``, the cube uses ReplacingMergeTree sort
keys, and the Commitment Service uses the idempotency key the assignment fixed
in its contract.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from temporalio import activity
from temporalio.exceptions import ApplicationError

from fpa_project.config import commitment_base_url
from . import engine as calc
from .errors import classify_http, permanent, refused, transient
from .models import (
    AccountFactor, CovenantInput, CovenantVerdict, DriverBinding, DriverShock, DriverSnapshot, PartitionInput,
    PartitionResult, PlanContext, Partition, TargetVersion,
)
from .stores import (
    BASELINE_TABLE, DIM_COLUMNS, PLAN_COLUMNS, PLAN_TABLE, PREIMAGE_TABLE, STAGED_TABLE,
    cube, ensure_cube_tables, postgres,
)
from fpa_project.identities import SERVICE

log = logging.getLogger("fpa.recompute.activities")

SCHEMA = "fpa_governance"
# The identity the workflow writes as. Seeded in db/seed.yaml with the
# `service` role, so its writes are attributable to the workflow and not to
# whichever person happened to trigger it.
SERVICE_USER = SERVICE
# The scenario whose lines the governance store holds. Stretch and downside
# are branches of it (scenario_driver_override), never stored copies: their
# cube rows are staged next to the base rows, but no plan_version_line is
# written for them, and migration 016 refuses one if anything tries.
BASE_SCENARIO = "base"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _audit(conn: Any, actor: str | None, entity_type: str, entity_id: str, action: str, payload: dict) -> None:
    """Append to the hash-linked audit log.

    The database computes the chain (migration 011): the row's key from its
    facts, its hash from the previous row's hash. The unique index on that
    key is what makes a retried activity land once -- the payload carries the
    workflow id and revision, so the same logical event has the same key and
    the second insert is discarded.
    """
    conn.execute(
        text(
            f"INSERT INTO {SCHEMA}.audit_event (actor_user_id, entity_type, entity_id, action, payload) "
            "VALUES (:actor, :entity_type, :entity_id, :action, CAST(:payload AS jsonb)) "
            "ON CONFLICT (event_key) DO NOTHING"
        ),
        {
            "actor": actor, "entity_type": entity_type, "entity_id": entity_id, "action": action,
            "payload": json.dumps(payload, separators=(",", ":"), sort_keys=True, default=str),
        },
    )


def _quote(value: str) -> str:
    """Escape a literal for ClickHouse. Only ever applied to codes that came
    out of the governance store, never to anything a caller typed."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _in_list(values: list[str]) -> str:
    return "(" + ", ".join(_quote(value) for value in values) + ")"


def _dirty_predicate(scopes: list[list[list[str]]]) -> str:
    """The dirty set as a ClickHouse predicate: a line some factor applies to.

    One clause per scope group: its accounts, and, when the shock was scoped,
    its companies and months. An unscoped group is account-only, exactly the
    predicate the dirty set used before scoping, so an unscoped shock reads
    the same rows it always did.
    """
    clauses = []
    for accounts, companies, months in scopes:
        parts = [f"account IN {_in_list(accounts)}"]
        if companies:
            parts.append(f"company IN {_in_list(companies)}")
        if months:
            parts.append(f"toString(period_month) IN {_in_list([m[:10] for m in months])}")
        clauses.append("(" + " AND ".join(parts) + ")")
    return "(" + " OR ".join(clauses) + ")" if clauses else "0"


# --------------------------------------------------------------------------
# Governance reads
# --------------------------------------------------------------------------
@activity.defn
def load_plan_context(plan_version_code: str) -> PlanContext:
    """Resolve the plan version and its model. Nothing is recomputed without it.

    A missing or unusable version is permanent: no number of retries will make
    the caller's plan version code exist.
    """
    with postgres().begin() as conn:
        row = conn.execute(
            text(
                "SELECT pv.plan_version_id, pv.plan_version_code, pv.model_id, pv.plan_year, "
                "       pv.state, pv.requested_by, pm.calc_order_dag "
                f"FROM {SCHEMA}.plan_version pv "
                f"JOIN {SCHEMA}.planning_model pm ON pm.model_id = pv.model_id "
                "WHERE pv.plan_version_code = :code"
            ),
            {"code": plan_version_code},
        ).mappings().first()
    if row is None:
        raise permanent(f"no plan version {plan_version_code!r}")
    return PlanContext(
        plan_version_id=str(row["plan_version_id"]),
        plan_version_code=row["plan_version_code"],
        model_id=str(row["model_id"]),
        plan_year=row["plan_year"],
        state=row["state"],
        requested_by=row["requested_by"],
        calc_order_dag=list(row["calc_order_dag"] or []),
    )


@activity.defn
def snapshot_drivers(model_id: str, driver_codes: list[str], effective_date: str) -> DriverSnapshot:
    """Freeze the driver library and its bindings at the effective date.

    Reading these once and passing them through the workflow is what stops a
    mid-run edit to the driver library from changing the answer halfway. A
    shock naming a driver that is not in the model is permanent, because it is
    a statement about the request rather than about the system.
    """
    with postgres().begin() as conn:
        known = {
            row[0]
            for row in conn.execute(
                text(
                    f"SELECT driver_code FROM {SCHEMA}.plan_driver "
                    "WHERE model_id = CAST(:model AS uuid) AND status = 'ACTIVE' "
                    "  AND effective_from <= CAST(:asof AS date) "
                    "  AND (effective_to IS NULL OR effective_to > CAST(:asof AS date))"
                ),
                {"model": model_id, "asof": effective_date},
            )
        }
        unknown = sorted(set(driver_codes) - known)
        if unknown:
            raise permanent(f"unknown or inactive driver(s) for this model: {', '.join(unknown)}")

        formulas = {
            row[0]: row[1]
            for row in conn.execute(
                text(
                    f"SELECT driver_code, formula FROM {SCHEMA}.plan_driver "
                    "WHERE model_id = CAST(:model AS uuid) AND status = 'ACTIVE' "
                    "  AND effective_from <= CAST(:asof AS date) "
                    "  AND (effective_to IS NULL OR effective_to > CAST(:asof AS date))"
                ),
                {"model": model_id, "asof": effective_date},
            )
        }
        bindings = [
            DriverBinding(
                driver_code=row[0], account_code=row[1], target=row[2], elasticity=float(row[3]),
            )
            for row in conn.execute(
                text(
                    "SELECT d.driver_code, b.account_code, b.target, b.elasticity "
                    f"FROM {SCHEMA}.plan_driver_binding b "
                    f"JOIN {SCHEMA}.plan_driver d ON d.driver_id = b.driver_id "
                    "WHERE d.model_id = CAST(:model AS uuid) "
                    "ORDER BY d.driver_code, b.account_code, b.target"
                ),
                {"model": model_id},
            )
        ]
    return DriverSnapshot(effective_date=effective_date, driver_codes=sorted(known), bindings=bindings, formulas=formulas)


# --------------------------------------------------------------------------
# Revision allocation and the successor plan version
# --------------------------------------------------------------------------
@activity.defn
def reserve_revision(plan_version_id: str, idempotency_key: str, workflow_id: str, shocks: list[list[Any]] | None = None) -> int:
    """Claim the cube revision this run will publish, or reuse the one it already has.

    The unique index on ``(plan_version_id, idempotency_key)`` is the whole
    mechanism: an identical re-forecast, run again tomorrow or retried after a
    crash, lands on the row it made the first time and gets the same revision
    back. That is why running the same shock twice leaves the same row counts,
    the same amounts and the same revision state rather than stacking a second
    revision on top.

    Two *different* re-forecasts racing for ``max(revision) + 1`` can collide
    on the other unique index. That raises, Temporal retries, and the loser
    picks up the next number.
    """
    with postgres().begin() as conn:
        # A key whose earlier reservation ended without a publish -- rejected,
        # expired, cancelled -- or was rolled back by compensation is retired
        # here, so asking for the same shock again gets a fresh revision and a
        # fresh successor. Without this the key pinned the shock to its closed
        # revision forever, and a re-forecast that timed out or hit a failing
        # Commitment Service could never be asked for again (found against the
        # real stack). A COMMITTED key is left alone: that is the idempotent
        # answer, and the workflow short-circuits on it.
        conn.execute(
            text(
                f"UPDATE {SCHEMA}.plan_publication p "
                "SET idempotency_key = p.idempotency_key || ':closed:' || p.revision "
                "WHERE p.plan_version_id = CAST(:pv AS uuid) AND p.idempotency_key = :key "
                "  AND (p.state IN ('COMPENSATED', 'SUPERSEDED') OR (p.state = 'RESERVED' AND EXISTS ("
                f"       SELECT 1 FROM {SCHEMA}.plan_version v "
                "       WHERE v.supersedes_plan_version_id = p.plan_version_id "
                "         AND v.revision = p.revision AND v.state = 'REJECTED')))"
            ),
            {"pv": plan_version_id, "key": idempotency_key},
        )
        row = conn.execute(
            text(
                f"INSERT INTO {SCHEMA}.plan_publication "
                "  (plan_version_id, revision, workflow_id, idempotency_key, shocks) "
                "SELECT CAST(:pv AS uuid), "
                f"       COALESCE((SELECT MAX(revision) FROM {SCHEMA}.plan_publication "
                "                  WHERE plan_version_id = CAST(:pv AS uuid)), 1) + 1, "
                "       :wf, :key, CAST(:shocks AS jsonb) "
                "ON CONFLICT (plan_version_id, idempotency_key) "
                # A no-op update rather than DO NOTHING, so RETURNING always
                # yields the row. The workflow id is deliberately left alone:
                # the run that reserved it keeps the claim.
                "  DO UPDATE SET idempotency_key = EXCLUDED.idempotency_key "
                "RETURNING revision, workflow_id"
            ),
            {"pv": plan_version_id, "wf": workflow_id, "key": idempotency_key, "shocks": json.dumps(shocks or [])},
        ).first()
    revision, owner = int(row[0]), row[1]
    if owner != workflow_id:
        log.info("revision %s was reserved by %s; %s is recomputing the same inputs", revision, owner, workflow_id)
    return revision


@activity.defn
def committed_shocks(plan_version_id: str) -> list[DriverShock]:
    """The cumulative shock set of the revision currently in the cube.

    That is the latest COMMITTED publication: a compensated one rolled
    itself back, a superseded one is no longer what the cube shows, and a
    reserved one never got there.
    """
    with postgres().begin() as conn:
        shocks = conn.execute(
            text(
                f"SELECT shocks FROM {SCHEMA}.plan_publication "
                "WHERE plan_version_id = CAST(:pv AS uuid) AND state = 'COMMITTED' "
                "ORDER BY revision DESC LIMIT 1"
            ),
            {"pv": plan_version_id},
        ).scalar()
    return [DriverShock.from_list(item) for item in (shocks or [])]


@activity.defn
def supersede_commitments(plan_version_id: str, revision: int) -> list[str]:
    """Release the commitments of every earlier committed revision.

    The new revision's dirty set covers every driver the earlier ones moved,
    because its shock set is cumulative, so its commitments reserve for
    everything theirs did. Leaving theirs in place would reserve the same
    budget twice. Released by key prefix, like compensation, and marked
    SUPERSEDED only once nothing of theirs is still reserved; a retry after a
    partial release converges because releasing twice is a no-op.
    """
    with postgres().begin() as conn:
        earlier = conn.execute(
            text(
                f"SELECT revision, idempotency_key FROM {SCHEMA}.plan_publication "
                "WHERE plan_version_id = CAST(:pv AS uuid) AND state = 'COMMITTED' AND revision < :rev ORDER BY revision"
            ),
            {"pv": plan_version_id, "rev": revision},
        ).all()
    released: list[str] = []
    with httpx.Client(base_url=commitment_base_url(), timeout=10.0) as http:
        for old_revision, key in earlier:
            try:
                listing = http.get("/commitments", params={"key_prefix": key})
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise transient(f"commitment service unreachable while superseding: {type(exc).__name__}") from None
            if listing.status_code >= 400:
                raise classify_http(listing.status_code, listing.text)
            outstanding = []
            for commitment in listing.json()["commitments"]:
                if commitment["state"] == "RELEASED":
                    continue
                try:
                    response = http.delete(f"/commitments/{commitment['commitment_id']}")
                except (httpx.TimeoutException, httpx.TransportError):
                    outstanding.append(commitment["commitment_id"])
                    continue
                if response.status_code >= 400 and response.status_code != 404:
                    outstanding.append(commitment["commitment_id"])
                else:
                    released.append(commitment["commitment_id"])
            if outstanding:
                raise transient(f"revision {old_revision}: {len(outstanding)} commitment(s) still reserved")
            with postgres().begin() as conn:
                conn.execute(
                    text(
                        f"UPDATE {SCHEMA}.plan_publication SET state = 'SUPERSEDED', settled_at = now() "
                        "WHERE plan_version_id = CAST(:pv AS uuid) AND revision = :rev AND state = 'COMMITTED'"
                    ),
                    {"pv": plan_version_id, "rev": old_revision},
                )
                _audit(conn, SERVICE_USER, "plan_version", plan_version_id, "REVISION_SUPERSEDED",
                       {"revision": old_revision, "superseded_by": revision})
    _supersede_versions(plan_version_id, revision)
    return released


def _supersede_versions(source_version_id: str, revision: int) -> None:
    """Mark what this revision replaced as SUPERSEDED, now that it is committed.

    The source plan and every earlier LOCKED successor in its line stop being
    the current plan the moment this revision is live and funded. Migration
    016 allows exactly this move on a locked row (LOCKED -> SUPERSEDED, nothing
    else changing, and only once a later locked successor exists), so the
    chain between versions is navigable by state as well as by
    ``supersedes_plan_version_id``. Idempotent: a retry finds nothing LOCKED.
    """
    with postgres().begin() as conn:
        conn.execute(text("SELECT set_config('fpa.actor', :who, true)"), {"who": SERVICE_USER})
        rows = conn.execute(
            text(
                f"UPDATE {SCHEMA}.plan_version v SET state = 'SUPERSEDED', updated_at = now() "
                "WHERE v.state = 'LOCKED' "
                "  AND (v.plan_version_id = CAST(:source AS uuid) "
                "       OR (v.supersedes_plan_version_id = CAST(:source AS uuid) AND v.revision < :rev)) "
                f"  AND EXISTS (SELECT 1 FROM {SCHEMA}.plan_version s "
                "              WHERE s.supersedes_plan_version_id = CAST(:source AS uuid) "
                "                AND s.revision = :rev AND s.state = 'LOCKED') "
                "RETURNING v.plan_version_code"
            ),
            {"source": source_version_id, "rev": revision},
        ).scalars().all()
        for code in rows:
            _audit(conn, SERVICE_USER, "plan_version", code, "STATE_SUPERSEDED",
                   {"superseded_by_revision": revision})


@activity.defn
def ensure_target_version(plan_context: PlanContext, revision: int, requested_by: str) -> TargetVersion:
    """Create, or find again, the superseding plan version this run drafts into.

    A re-forecast rebases a plan that has already been agreed, and the 004
    lock guard refuses writes to a LOCKED version's lines -- the trigger even
    says what to do instead: *create a superseding version*. So the drafts land
    in a new DRAFT version that supersedes the source, and it is that successor
    the approver locks. The source stays exactly as it was approved.

    The code is derived from the revision, which is derived from the
    idempotency key, so a re-run finds its own successor rather than making a
    second one -- and then has to notice that it did, which is why this returns
    both states rather than just the code.
    """
    code = f"{plan_context.plan_version_code}-R{revision}"
    with postgres().begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO {SCHEMA}.plan_version "
                "  (plan_version_code, model_id, plan_year, state, covenant_ok, covenant_note, "
                "   requested_by, supersedes_plan_version_id, revision) "
                "VALUES (:code, CAST(:model AS uuid), :year, 'DRAFT', false, "
                "        :note, :requested_by, CAST(:supersedes AS uuid), :revision) "
                "ON CONFLICT (plan_version_code) DO NOTHING"
            ),
            {
                "code": code, "model": plan_context.model_id, "year": plan_context.plan_year,
                "note": f"re-forecast of {plan_context.plan_version_code}; the system checks its covenants when it is submitted",
                "requested_by": requested_by, "supersedes": plan_context.plan_version_id,
                "revision": revision,
            },
        )
        row = conn.execute(
            text(
                "SELECT v.plan_version_id, v.state, p.state, p.workflow_id "
                f"FROM {SCHEMA}.plan_version v "
                f"LEFT JOIN {SCHEMA}.plan_publication p "
                "  ON p.plan_version_id = CAST(:source AS uuid) AND p.revision = :revision "
                "WHERE v.plan_version_code = :code"
            ),
            {"code": code, "source": plan_context.plan_version_id, "revision": revision},
        ).first()
    if row is None:
        raise transient(f"successor version {code!r} is not visible yet")
    return TargetVersion(
        plan_version_code=code,
        plan_version_id=str(row[0]),
        version_state=row[1],
        publication_state=row[2] or "RESERVED",
        published_by=row[3] or "",
    )


# --------------------------------------------------------------------------
# Baseline and dirty set
# --------------------------------------------------------------------------
@activity.defn
def snapshot_baseline(plan_version_code: str) -> int:
    """Freeze the plan as it stands, once per plan version.

    Every recompute reads the baseline rather than the live cube. Without this,
    a second run of the same shock would apply it to numbers that already had
    it applied, and "run it twice, get the same answer" would be false.

    Idempotent by an existence check rather than by a conditional insert: once
    a plan version has a baseline it is never rewritten, because rewriting it
    is precisely the thing that would break the guarantee.
    """
    ensure_cube_tables()
    client = cube()
    version = _quote(plan_version_code)
    existing = client.query(
        f"SELECT count() FROM {BASELINE_TABLE} WHERE plan_version = {version}"
    ).result_rows[0][0]
    if existing:
        return int(existing)
    columns = ", ".join(PLAN_COLUMNS)
    client.command(
        f"INSERT INTO {BASELINE_TABLE} ({columns}) "
        f"SELECT {columns} FROM {PLAN_TABLE} FINAL WHERE plan_version = {version}"
    )
    rows = client.query(
        f"SELECT count() FROM {BASELINE_TABLE} WHERE plan_version = {version}"
    ).result_rows[0][0]
    if rows == 0:
        raise permanent(f"the cube holds no plan lines for {plan_version_code!r}; nothing to recompute")
    log.info("baseline frozen for %s: %s rows", plan_version_code, rows)
    return int(rows)


@activity.defn
def resolve_dirty_set(
    plan_version_code: str,
    scenario_codes: list[str],
    accounts: list[str],
    target_size: int,
    scopes: list[list[list[str]]] | None = None,
) -> list[Partition]:
    """Count the dirty rows and cut them into partitions for the children.

    The cut runs along (scenario, month) because a plan line belongs to exactly
    one of those, so no two children can ever write the same row. The counting
    happens here, in an activity, so the workflow receives a plain list and
    stays deterministic.
    """
    ensure_cube_tables()
    if not accounts:
        return []
    client = cube()
    available = {r[0] for r in client.query(
        f"SELECT DISTINCT scenario_id FROM {BASELINE_TABLE} WHERE plan_version = {_quote(plan_version_code)}"
    ).result_rows}
    if set(scenario_codes) != available:
        raise permanent("scenario-subset reforecast is unsupported: cumulative shocks require every plan scenario")
    # Scoped: only lines inside a shock's companies and months are dirty.
    # Older histories pass no scopes and keep the account-only dirty set.
    dirty = _dirty_predicate(scopes) if scopes else f"account IN {_in_list(accounts)}"
    rows = client.query(
        f"SELECT scenario_id, toString(period_month), count() FROM {BASELINE_TABLE} "
        f"WHERE plan_version = {_quote(plan_version_code)} "
        f"  AND scenario_id IN {_in_list(scenario_codes)} "
        f"  AND {dirty} "
        "GROUP BY scenario_id, period_month ORDER BY scenario_id, period_month"
    ).result_rows

    counts = {(scenario, month): int(count) for scenario, month, count in rows}
    months = sorted({month for _, month, _ in rows})
    plan = calc.partition_plan(sorted(scenario_codes), months, counts, target_size)
    return [
        Partition(index=index, scenario_code=scenario, period_months=list(cell_months), row_count=count)
        for index, (scenario, cell_months, count) in enumerate(plan)
    ]


# --------------------------------------------------------------------------
# The long one: evaluate a partition
# --------------------------------------------------------------------------
@activity.defn
def evaluate_partition(payload: PartitionInput) -> PartitionResult:
    """Recompute one partition, writing the drafts and staging the cube rows.

    This is the activity that can run for minutes, so it heartbeats a cursor
    and resumes from it. A retry after a worker death picks up at the last
    checkpoint instead of starting the partition again -- and even if it did
    start again, both writes are upserts, so the result would be the same.

    The arithmetic happens once, here, and the same ``Decimal`` values go to
    Postgres and to the cube staging table. Letting each surface multiply and
    round for itself is how a plan and its published copy end up a cent apart.
    """
    ensure_cube_tables()
    client = cube()
    factors = payload.factors
    accounts = calc.affected_accounts(factors)
    if not accounts:
        return PartitionResult(index=payload.partition.index, rows_written=0)

    # A retry resumes from the last heartbeat. The ORDER BY is what makes the
    # offset mean the same thing on the second attempt as on the first.
    offset = 0
    written = 0
    if activity.info().heartbeat_details:
        offset, written = activity.info().heartbeat_details[0]
        log.info("partition %s resuming at offset %s", payload.partition.index, offset)

    batch_size = 2_000
    dim_columns = [column for column in PLAN_COLUMNS if column not in {
        "plan_version", "scenario_id", "revision", "quantity", "unit_price", "amount_functional",
    }]
    select_columns = ", ".join(dim_columns)
    where = (
        f"plan_version = {_quote(payload.plan_version_code)} "
        f"AND scenario_id = {_quote(payload.partition.scenario_code)} "
        f"AND {_dirty_predicate(payload.scopes or [list(g) for g in calc.dirty_scopes(factors)])} "
        f"AND period_month IN {_in_list(payload.partition.period_months)}"
    )
    # Only the base scenario is the governed record. A stretch or downside
    # partition stages its cube rows (the published projection of that branch)
    # and writes no plan_version_line.
    governed = payload.partition.scenario_code == BASE_SCENARIO

    while True:
        activity.heartbeat((offset, written))
        rows = client.query(
            f"SELECT {select_columns}, quantity, unit_price FROM {BASELINE_TABLE} FINAL "
            f"WHERE {where} "
            "ORDER BY company, period_month, account, dim_signature_hash "
            f"LIMIT {batch_size} OFFSET {offset}"
        ).result_rows
        if not rows:
            break

        draft_rows: list[dict] = []
        cube_rows: list[list[Any]] = []
        for row in rows:
            record = dict(zip(dim_columns, row[:-2]))
            quantity, unit_price = float(row[-2]), float(row[-1])
            account = record["account"]
            month = str(record["period_month"])[:10]
            new_quantity, new_price, amount = calc.recompute_line(
                quantity, unit_price, factors, account, record["company"], month,
            )
            if governed:
                trace = calc.derivation_trace(
                    account, factors, payload.shock_trace, record["company"], month,
                    baseline=(quantity, unit_price), result=(new_quantity, new_price, amount),
                )
                draft_rows.append({
                    "plan_version_id": payload.target_version_id,
                    "scenario_code": payload.partition.scenario_code,
                    "company_code": record["company"],
                    "period_month": record["period_month"],
                    "account_code": account,
                    "dim_signature_hash": record["dim_signature_hash"],
                    "quantity": new_quantity,
                    "unit_price": new_price,
                    "amount_functional": amount,
                    "functional_currency": record["functional_currency"],
                    "trace": json.dumps(trace, separators=(",", ":"), sort_keys=True),
                    "source_revision": payload.revision,
                })
            cube_rows.append([
                payload.plan_version_code, payload.partition.scenario_code, payload.revision,
                record["company"], record["period_month"], account,
                *[record[column] for column in DIM_COLUMNS],
                record["dim_signature_hash"],
                float(new_quantity), float(new_price), amount,
                record["functional_currency"], record["plan_line_type"],
            ])

        _write_drafts(draft_rows)
        client.insert(STAGED_TABLE, cube_rows, column_names=list(PLAN_COLUMNS))

        offset += len(rows)
        written += len(rows)
        if len(rows) < batch_size:
            break

    activity.heartbeat((offset, written))
    return PartitionResult(index=payload.partition.index, rows_written=written)


def _write_drafts(rows: list[dict]) -> None:
    """Upsert draft lines on the grain's unique constraint.

    ``ON CONFLICT ... DO UPDATE`` is what makes a retried batch land once. The
    amount is stored rather than derived, and the 004 check constraint insists
    it equals ``round(quantity * unit_price, 2)``, which is why the engine
    rounds the inputs before multiplying.
    """
    if not rows:
        return
    statement = text(
        f"INSERT INTO {SCHEMA}.plan_version_line "
        "  (plan_version_id, scenario_code, company_code, period_month, account_code, "
        "   dim_signature_hash, quantity, unit_price, amount_functional, functional_currency, "
        "   driver_derivation_trace, source_revision) "
        "VALUES (CAST(:plan_version_id AS uuid), :scenario_code, :company_code, :period_month, "
        "        :account_code, :dim_signature_hash, :quantity, :unit_price, :amount_functional, "
        "        :functional_currency, CAST(:trace AS jsonb), :source_revision) "
        "ON CONFLICT (plan_version_id, scenario_code, company_code, period_month, account_code, dim_signature_hash) "
        "DO UPDATE SET quantity = EXCLUDED.quantity, unit_price = EXCLUDED.unit_price, "
        "              amount_functional = EXCLUDED.amount_functional, "
        "              driver_derivation_trace = EXCLUDED.driver_derivation_trace, "
        "              source_revision = EXCLUDED.source_revision"
    )
    with postgres().begin() as conn:
        conn.execute(statement, rows)


# --------------------------------------------------------------------------
# The approval gate
# --------------------------------------------------------------------------
@activity.defn
def record_submission(target_version_id: str, submitted_by: str, comment: str, workflow_id: str) -> None:
    """The planner submits the recomputed draft they have reviewed.

    Submitting needs a role for DRAFT -> IN_REVIEW. The draft stays DRAFT
    here: the system's covenant check comes next, and only a pass moves it
    into review (``open_review``); a breach rejects it without a controller
    ever being asked.
    """
    try:
        with postgres().begin() as conn:
            _require_roles(conn, submitted_by, [("DRAFT", "IN_REVIEW")])
            state = conn.execute(
                text(f"SELECT state FROM {SCHEMA}.plan_version WHERE plan_version_id = CAST(:pv AS uuid)"),
                {"pv": target_version_id},
            ).scalar()
            if state is None:
                raise permanent(f"no successor version {target_version_id}")
            if state != "DRAFT":
                raise refused(f"the draft is {state}; only a DRAFT can be submitted")
            _audit(conn, submitted_by, "plan_version", target_version_id, "SUBMITTED",
                   {"workflow_id": workflow_id, "comment": comment})
    except DBAPIError as exc:
        if not _is_governance_refusal(exc):
            raise
        raise _refuse_on_constraint(exc, submitted_by) from exc


@activity.defn
def open_review(target_version_id: str, workflow_id: str) -> None:
    """The submitted draft passed the system's covenant check: DRAFT -> IN_REVIEW, and a PENDING approval opens.

    Refused unless covenant_ok is true and no check failed, so nothing but a
    passing automated verdict puts a re-forecast before a controller. The
    approval row is the *governance* half of waiting for the controller:
    Postgres records that a decision is outstanding and who asked for it. The
    *execution* half is the run parked on its signal.
    """
    with postgres().begin() as conn:
        conn.execute(text("SELECT set_config('fpa.actor', :who, true)"), {"who": SERVICE_USER})
        state, requested_by, covenant_ok = conn.execute(
            text(f"SELECT state, requested_by, covenant_ok FROM {SCHEMA}.plan_version "
                 "WHERE plan_version_id = CAST(:pv AS uuid) FOR UPDATE"),
            {"pv": target_version_id},
        ).one()
        failed = conn.execute(
            text(f"SELECT count(*) FROM {SCHEMA}.covenant_check WHERE plan_version_id = CAST(:pv AS uuid) AND NOT passed"),
            {"pv": target_version_id},
        ).scalar()
        if failed or covenant_ok is not True:
            raise permanent(f"the draft has no passing covenant verdict ({failed} checks failed); it cannot go to review")
        if state == "IN_REVIEW":
            return  # a retry after the commit landed
        conn.execute(
            text(f"UPDATE {SCHEMA}.plan_version SET state = 'IN_REVIEW', updated_at = now() "
                 "WHERE plan_version_id = CAST(:pv AS uuid) AND state = 'DRAFT'"),
            {"pv": target_version_id},
        )
        conn.execute(
            text(
                f"INSERT INTO {SCHEMA}.plan_approval (plan_version_id, requested_by, decision, covenant_ok) "
                f"SELECT CAST(:pv AS uuid), :requested_by, 'PENDING', true "
                f"WHERE NOT EXISTS (SELECT 1 FROM {SCHEMA}.plan_approval "
                "                   WHERE plan_version_id = CAST(:pv AS uuid) AND decision = 'PENDING')"
            ),
            {"pv": target_version_id, "requested_by": requested_by},
        )
        _audit(conn, SERVICE_USER, "plan_version", target_version_id, "IN_REVIEW",
               {"workflow_id": workflow_id, "reason": "automated covenant check passed"})


def _require_roles(conn: Any, who: str, moves: list[tuple[str, str]]) -> None:
    """The decider holds a role ``plan_state_transition`` allows for each move."""
    roles = {
        row[0] for row in conn.execute(
            text(f"SELECT role_code FROM {SCHEMA}.user_role WHERE user_id = :who"), {"who": who},
        )
    }
    if not roles:
        raise refused(f"{who!r} is not a known user with any role")
    for from_state, to_state in moves:
        allowed = {
            row[0] for row in conn.execute(
                text(
                    f"SELECT role_code FROM {SCHEMA}.plan_state_transition "
                    "WHERE from_state = :from_state AND to_state = :to_state"
                ),
                {"from_state": from_state, "to_state": to_state},
            )
        }
        if not roles & allowed:
            raise refused(
                f"{who} may not move a plan version from {from_state} to {to_state}; "
                f"that needs one of: {', '.join(sorted(allowed)) or 'nobody'}"
            )


def _may_decide(conn: Any, target_version_id: str, decided_by: str, moves: list[tuple[str, str]]) -> None:
    """Refuse a decision the governance rules do not allow, before touching anything.

    The same rules ``governance.transition`` applies to any plan version: the
    move has to be declared in ``plan_state_transition`` and the decider has to
    hold one of its roles. The requester check is also enforced by
    ``plan_approval``'s trigger and check constraint; it is asked here too so
    the refusal says something a person can act on rather than a trigger name.
    """
    requester = conn.execute(
        text(f"SELECT requested_by FROM {SCHEMA}.plan_version WHERE plan_version_id = CAST(:pv AS uuid)"),
        {"pv": target_version_id},
    ).scalar()
    if decided_by == requester:
        raise refused(f"segregation of duties: {decided_by} requested this re-forecast and cannot decide on it")
    # A question-driven request has two gates held by two people: the
    # controller who started it and the approver who publishes it. The CFO
    # also holds the controller role, so without this one person could do
    # both. Migration 016 enforces the same rule on plan_approval.
    starter = conn.execute(
        text(
            f"SELECT r.controller_decided_by FROM {SCHEMA}.covenant_check c "
            f"JOIN {SCHEMA}.reforecast_request r ON r.request_id = c.request_id "
            "WHERE c.plan_version_id = CAST(:pv AS uuid) LIMIT 1"
        ),
        {"pv": target_version_id},
    ).scalar()
    if starter is not None and decided_by == starter:
        raise refused(f"segregation of duties: {decided_by} started this re-forecast as controller "
                      "and cannot also approve it")
    _require_roles(conn, decided_by, moves)


def _is_governance_refusal(exc: DBAPIError) -> bool:
    """A constraint or a trigger said no, which is a verdict and not an outage.

    Decided by SQLSTATE rather than by exception class, because the classes do
    not line up with the meaning: psycopg's RaiseException -- what the
    plan_approval trigger raises -- subclasses ProgrammingError, the same class
    as a typo in the SQL. Class 23 is an integrity violation; P0001 is a
    trigger's RAISE EXCEPTION.
    """
    sqlstate = getattr(exc.orig, "sqlstate", None) or ""
    return sqlstate.startswith("23") or sqlstate == "P0001"


def _refuse_on_constraint(exc: DBAPIError, decided_by: str) -> ApplicationError:
    detail = str(exc.orig).splitlines()[0] if exc.orig is not None else str(exc)
    return refused(f"the governance store refused {decided_by}'s decision: {detail}")


@activity.defn
def record_approval(target_version_id: str, decided_by: str, comment: str, workflow_id: str) -> None:
    """A controller approves the submitted plan: IN_REVIEW -> APPROVED, and nothing more.

    Approval is not locking and not publishing: the plan waits for the CFO's
    lock after this. The approver needs a role for IN_REVIEW -> APPROVED, must
    not be the requester, and the stored covenant flag must be true. Anything
    the rules or the database refuse comes back as a refusal, which the
    workflow treats as "not this decision" and keeps waiting for a valid one --
    a bad approval must not be able to fail the run.
    """
    try:
        with postgres().begin() as conn:
            # The covenant flag is a controller-only field (migration 011):
            # the approver, not the service identity, is the actor here.
            conn.execute(text("SELECT set_config('fpa.actor', :who, true)"), {"who": decided_by})
            _may_decide(conn, target_version_id, decided_by, [("IN_REVIEW", "APPROVED")])
            covenant_ok = conn.execute(
                text(f"SELECT covenant_ok FROM {SCHEMA}.plan_version WHERE plan_version_id = CAST(:pv AS uuid) FOR UPDATE"),
                {"pv": target_version_id},
            ).scalar()
            if covenant_ok is not True:
                raise refused("covenant review must pass before approval; ask a controller to record the verdict")
            conn.execute(
                text(
                    f"UPDATE {SCHEMA}.plan_approval "
                    "SET decision = 'APPROVED', decided_by = :who, decided_at = now(), "
                    "    covenant_ok = true, comment = :comment "
                    "WHERE plan_version_id = CAST(:pv AS uuid) AND decision = 'PENDING'"
                ),
                {"pv": target_version_id, "who": decided_by, "comment": comment or None},
            )
            conn.execute(
                text(
                    f"UPDATE {SCHEMA}.plan_version "
                    "SET state = 'APPROVED', approved_by = :who, updated_at = now() "
                    "WHERE plan_version_id = CAST(:pv AS uuid) AND state = 'IN_REVIEW'"
                ),
                {"pv": target_version_id, "who": decided_by},
            )
            _audit(conn, decided_by, "plan_version", target_version_id, "APPROVED",
                   {"workflow_id": workflow_id, "comment": comment})
    except DBAPIError as exc:
        # A dropped connection is still worth a retry; only a verdict is not.
        if not _is_governance_refusal(exc):
            raise
        raise _refuse_on_constraint(exc, decided_by) from exc


@activity.defn
def record_lock(target_version_id: str, locked_by: str, comment: str, workflow_id: str) -> None:
    """The CFO locks the approved plan: APPROVED -> LOCKED.

    The human governance action that makes the version final. Publishing is
    the system's next step, and it re-checks LOCKED for itself. The locker
    needs a role for APPROVED -> LOCKED, is not the requester, and is not the
    controller who approved it: approve and lock are two people's decisions,
    and the CFO also holds the controller role, so without this one person
    could make both.
    """
    try:
        with postgres().begin() as conn:
            conn.execute(text("SELECT set_config('fpa.actor', :who, true)"), {"who": locked_by})
            _may_decide(conn, target_version_id, locked_by, [("APPROVED", "LOCKED")])
            state, approved_by = conn.execute(
                text(f"SELECT state, approved_by FROM {SCHEMA}.plan_version "
                     "WHERE plan_version_id = CAST(:pv AS uuid) FOR UPDATE"),
                {"pv": target_version_id},
            ).one()
            if state == "LOCKED":
                return  # a retry after the commit landed
            if state != "APPROVED":
                raise refused(f"the plan is {state}; only an APPROVED plan can be locked")
            if approved_by == locked_by:
                raise refused(f"segregation of duties: {locked_by} approved this plan and cannot also lock it")
            conn.execute(
                text(f"UPDATE {SCHEMA}.plan_version SET state = 'LOCKED', updated_at = now() "
                     "WHERE plan_version_id = CAST(:pv AS uuid) AND state = 'APPROVED'"),
                {"pv": target_version_id},
            )
            _audit(conn, locked_by, "plan_version", target_version_id, "LOCKED",
                   {"workflow_id": workflow_id, "comment": comment})
    except DBAPIError as exc:
        if not _is_governance_refusal(exc):
            raise
        raise _refuse_on_constraint(exc, locked_by) from exc


@activity.defn
def record_rejection(
    target_version_id: str, decided_by: str, comment: str, workflow_id: str, expired: bool
) -> None:
    """End the run cleanly on a rejection, an expiry, a cancel or a failure.

    All four leave the same trace: the successor is REJECTED, the approval row
    says who closed it and when, nothing was published and nothing was
    committed. The draft lines stay where they are on purpose -- they are the
    evidence of what was turned down.

    An empty ``decided_by`` means nobody decided: the timer ran out, the run
    was cancelled, or it failed. The service identity closes the row then,
    which is the honest account and, unlike an empty string, a real user the
    foreign key accepts. A person rejecting is held to the same rules as a
    person approving.
    """
    human = bool(decided_by) and not expired
    actor = decided_by if human else SERVICE_USER
    decision_comment = comment or ("no decision inside the approval window" if expired else "")
    try:
        with postgres().begin() as conn:
            if human:
                # A controller rejects a submitted plan (IN_REVIEW); the CFO
                # declines to lock an approved one (APPROVED). Either way the
                # move must be declared for the decider's role.
                state = conn.execute(
                    text(f"SELECT state FROM {SCHEMA}.plan_version WHERE plan_version_id = CAST(:pv AS uuid)"),
                    {"pv": target_version_id},
                ).scalar()
                _may_decide(conn, target_version_id, decided_by, [(state, "REJECTED")])
            conn.execute(
                text(
                    f"UPDATE {SCHEMA}.plan_approval "
                    "SET decision = 'REJECTED', decided_by = :who, decided_at = now(), comment = :comment "
                    "WHERE plan_version_id = CAST(:pv AS uuid) AND decision = 'PENDING'"
                ),
                {"pv": target_version_id, "who": actor, "comment": decision_comment or None},
            )
            conn.execute(
                text(
                    f"UPDATE {SCHEMA}.plan_version SET state = 'REJECTED', updated_at = now() "
                    "WHERE plan_version_id = CAST(:pv AS uuid) AND state IN ('DRAFT', 'IN_REVIEW', 'APPROVED')"
                ),
                {"pv": target_version_id},
            )
            _audit(conn, actor, "plan_version", target_version_id,
                   "EXPIRED" if expired else ("REJECTED" if human else "CLOSED"),
                   {"workflow_id": workflow_id, "comment": decision_comment})
    except DBAPIError as exc:
        if not _is_governance_refusal(exc):
            raise
        raise _refuse_on_constraint(exc, actor) from exc


@activity.defn
def verify_publishable(target_version_id: str, revision: int) -> int:
    """Re-read the version's state and refuse to publish unless it is LOCKED.

    The CFO's lock has just taken it to LOCKED, and the API checked before
    that. This asks anyway, because the assignment is explicit that the
    workflow enforces the rule rather than assuming the caller did -- and
    because between the lock and here, someone could have moved it.

    Returns the draft row count so the publish can check it got all of them.
    """
    with postgres().begin() as conn:
        state = conn.execute(
            text(f"SELECT state FROM {SCHEMA}.plan_version WHERE plan_version_id = CAST(:pv AS uuid)"),
            {"pv": target_version_id},
        ).scalar()
        if state != "LOCKED":
            raise permanent(f"refusing to publish revision {revision}: plan version is {state!r}, not LOCKED")
        return int(conn.execute(
            text(f"SELECT count(*) FROM {SCHEMA}.plan_version_line WHERE plan_version_id = CAST(:pv AS uuid)"),
            {"pv": target_version_id},
        ).scalar() or 0)


# --------------------------------------------------------------------------
# Cube publication and its compensation
# --------------------------------------------------------------------------
@activity.defn
def snapshot_preimage(plan_version_code: str, revision: int) -> int:
    """Copy the rows this publish is about to overwrite.

    Compensation cannot be "delete revision N". ``fact_plan_line`` is a
    ReplacingMergeTree keyed on the grain, so once a merge has run, the row
    that revision N replaced is gone and deleting N leaves a hole. Keeping the
    pre-image is what makes the rollback an exact restore.

    Idempotent through an existence check: the snapshot is taken once per
    revision and never refreshed, because refreshing it after the publish would
    snapshot the publish itself.
    """
    ensure_cube_tables()
    client = cube()
    version = _quote(plan_version_code)
    existing = client.query(
        f"SELECT count() FROM {PREIMAGE_TABLE} WHERE plan_version = {version} "
        f"AND preimage_for_revision = {revision}"
    ).result_rows[0][0]
    if existing:
        return int(existing)

    columns = ", ".join(PLAN_COLUMNS)
    client.command(
        f"INSERT INTO {PREIMAGE_TABLE} (preimage_for_revision, {columns}) "
        f"SELECT {revision}, {columns} FROM {PLAN_TABLE} FINAL "
        f"WHERE plan_version = {version} "
        "  AND (scenario_id, company, period_month, account, dim_signature_hash) IN ("
        f"      SELECT scenario_id, company, period_month, account, dim_signature_hash "
        f"      FROM {STAGED_TABLE} FINAL WHERE plan_version = {version} AND revision = {revision})"
    )
    rows = client.query(
        f"SELECT count() FROM {PREIMAGE_TABLE} WHERE plan_version = {version} "
        f"AND preimage_for_revision = {revision}"
    ).result_rows[0][0]
    return int(rows)


@activity.defn
def publish_to_cube(plan_version_code: str, plan_version_id: str, revision: int, expected_rows: int) -> int:
    """Copy the staged rows into ``fact_plan_line`` at this revision.

    Idempotent by construction rather than by a guard: the staged rows carry
    the revision, the target's sort key is the grain, and its engine keeps the
    highest revision per key. Running this twice inserts the same rows twice
    and the table collapses them. The values themselves were computed once, in
    the evaluate activity, so publishing cannot disagree with the drafts.
    """
    ensure_cube_tables()
    client = cube()
    version = _quote(plan_version_code)
    per_scenario = dict(client.query(
        f"SELECT scenario_id, count() FROM {STAGED_TABLE} FINAL WHERE plan_version = {version} AND revision = {revision} "
        "GROUP BY scenario_id"
    ).result_rows)
    staged = int(sum(per_scenario.values()))
    if staged == 0:
        raise permanent(f"nothing staged for {plan_version_code} revision {revision}")
    # The approved draft is the base branch; every branch shares its keys, so
    # each scenario must have staged exactly as many rows as were approved.
    mismatched = {scenario: int(count) for scenario, count in per_scenario.items() if int(count) != expected_rows}
    if BASE_SCENARIO not in per_scenario or mismatched:
        raise permanent(
            f"staged rows per scenario {dict(per_scenario)} do not match the approved draft lines ({expected_rows}); "
            "refusing to publish"
        )

    columns = ", ".join(PLAN_COLUMNS)
    client.command(
        f"INSERT INTO {PLAN_TABLE} ({columns}) SELECT {columns} FROM {STAGED_TABLE} FINAL "
        f"WHERE plan_version = {version} AND revision = {revision}"
    )
    with postgres().begin() as conn:
        conn.execute(
            text(
                f"UPDATE {SCHEMA}.plan_publication SET state = 'PUBLISHED', row_count = :rows "
                "WHERE plan_version_id = CAST(:pv AS uuid) AND revision = :revision AND state = 'RESERVED'"
            ),
            {"rows": staged, "pv": plan_version_id, "revision": revision},
        )
        _audit(conn, SERVICE_USER, "plan_version", plan_version_id, "PUBLISHED_TO_CUBE",
               {"revision": revision, "rows": staged})
    log.info("published %s rows to the cube for %s revision %s", staged, plan_version_code, revision)
    return staged


@activity.defn
def unpublish_revision(plan_version_code: str, plan_version_id: str, revision: int) -> int:
    """Put the cube back exactly as it was before this revision.

    Two steps, in this order: delete the rows this revision wrote, waiting for
    the mutation to finish, then re-insert the pre-image at its own original
    revision numbers. Doing it the other way round would let the delete take
    the restored rows with it.

    Idempotent: a second run finds no rows at this revision and re-inserts a
    pre-image that is already there, which the sort key collapses.
    """
    ensure_cube_tables()
    client = cube()
    version = _quote(plan_version_code)
    client.command(
        f"ALTER TABLE {PLAN_TABLE} DELETE WHERE plan_version = {version} AND revision = {revision}",
        settings={"mutations_sync": 2},
    )
    columns = ", ".join(PLAN_COLUMNS)
    client.command(
        f"INSERT INTO {PLAN_TABLE} ({columns}) SELECT {columns} FROM {PREIMAGE_TABLE} FINAL "
        f"WHERE plan_version = {version} AND preimage_for_revision = {revision}"
    )
    restored = int(client.query(
        f"SELECT count() FROM {PREIMAGE_TABLE} FINAL WHERE plan_version = {version} "
        f"AND preimage_for_revision = {revision}"
    ).result_rows[0][0])
    with postgres().begin() as conn:
        conn.execute(
            text(
                f"UPDATE {SCHEMA}.plan_publication SET state = 'COMPENSATED', settled_at = now() "
                "WHERE plan_version_id = CAST(:pv AS uuid) AND revision = :revision"
            ),
            {"pv": plan_version_id, "revision": revision},
        )
        _audit(conn, SERVICE_USER, "plan_version", plan_version_id, "CUBE_PUBLICATION_ROLLED_BACK",
               {"revision": revision, "restored_rows": restored})
    log.warning("rolled back cube revision %s for %s, restoring %s rows", revision, plan_version_code, restored)
    return restored


# --------------------------------------------------------------------------
# The Commitment Service: the counterparty that is not correct
# --------------------------------------------------------------------------
@activity.defn
def commit_to_treasury(
    plan_version_code: str, plan_version_id: str, revision: int, idempotency_key: str
) -> list[str]:
    """Reserve budget for the newly published revision.

    The key is derived from the plan version and the revision, both of which
    are fixed long before this runs, so every retry of this activity -- and
    every retry of the whole workflow -- sends the same key. The service's
    contract says the same key twice returns the first result and creates
    nothing new, which is what makes a timeout here safe: we do not know
    whether the first attempt landed, and we do not need to.

    The commitments themselves are one per (scenario, account type) rollup of
    the published revision, which is what finance reserves against.
    """
    ensure_cube_tables()
    client = cube()
    # Reserve the complete current plan, not only dirty rows. Translate each
    # functional amount before aggregating; adding PLN to USD is meaningless.
    version = _quote(plan_version_code)
    translated = (
        f"FROM (SELECT * FROM {PLAN_TABLE} FINAL WHERE plan_version = {version}) p "
        "LEFT JOIN (SELECT period_month, from_currency, any(rate) AS rate, count() AS n "
        f"FROM fpa_cube.dim_fx_plan WHERE plan_version = {version} "
        "GROUP BY period_month, from_currency) fx "
        "ON fx.period_month = p.period_month AND fx.from_currency = p.functional_currency "
    )
    invalid = client.query("SELECT count() " + translated + "WHERE fx.n != 1 OR fx.rate <= 0").result_rows[0][0]
    if invalid:
        raise permanent("missing, duplicate or nonpositive plan FX rate; treasury commitment refused")
    rollup = client.query(
        "SELECT p.scenario_id, p.plan_line_type, toString(round(sum(p.amount_functional * fx.rate), 2)) "
        + translated + "GROUP BY p.scenario_id, p.plan_line_type ORDER BY p.scenario_id, p.plan_line_type"
    ).result_rows

    created: list[str] = []
    with httpx.Client(base_url=commitment_base_url(), timeout=10.0) as http:
        for scenario, line_type, amount in rollup:
            key = f"{idempotency_key}:{scenario}:{line_type}"
            body = {
                "plan_version": plan_version_code,
                "revision": revision,
                "scenario": scenario,
                "category": line_type,
                "amount": str(amount),
                "currency": "USD",
            }
            try:
                response = http.post("/commitments", json=body, headers={"Idempotency-Key": key})
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                # from None: the message already names the error, and chaining
                # httpx's causes made the failure too big for Temporal to keep --
                # the UI showed "Failure exceeds size limit" instead of the reason.
                raise transient(f"commitment service unreachable: {type(exc).__name__}: {exc}") from None
            if response.status_code >= 400:
                raise classify_http(response.status_code, response.text)
            created.append(response.json()["commitment_id"])

    with postgres().begin() as conn:
        conn.execute(
            text(
                f"UPDATE {SCHEMA}.plan_publication "
                "SET state = 'COMMITTED', commitment_ids = :ids, settled_at = now() "
                "WHERE plan_version_id = CAST(:pv AS uuid) AND revision = :revision"
            ),
            {"ids": created, "pv": plan_version_id, "revision": revision},
        )
        _audit(conn, SERVICE_USER, "plan_version", plan_version_id, "COMMITTED_TO_TREASURY",
               {"revision": revision, "commitments": created})
    return created


@activity.defn
def compensate_commitments(plan_version_id: str, revision: int, idempotency_key: str) -> list[str]:
    """Release whatever commitments this revision managed to create.

    The ids come from the service, by key, rather than from our own record of
    what we think we created: an attempt that timed out may have created a
    commitment we never saw the id for, and releasing only what we remember
    would leave that one reserved forever.

    DELETE is itself allowed to fail. Anything still outstanding is returned to
    the caller, which raises, and Temporal retries this activity under its own
    policy until the ledger is clean.
    """
    outstanding: list[str] = []
    with httpx.Client(base_url=commitment_base_url(), timeout=10.0) as http:
        try:
            listing = http.get("/commitments", params={"key_prefix": idempotency_key})
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise transient(f"commitment service unreachable while compensating: {exc}") from None
        if listing.status_code >= 400:
            raise classify_http(listing.status_code, listing.text)

        for commitment in listing.json()["commitments"]:
            if commitment["state"] == "RELEASED":
                continue
            try:
                response = http.delete(f"/commitments/{commitment['commitment_id']}")
            except (httpx.TimeoutException, httpx.TransportError):
                outstanding.append(commitment["commitment_id"])
                continue
            if response.status_code >= 400 and response.status_code != 404:
                outstanding.append(commitment["commitment_id"])

    if outstanding:
        raise transient(
            f"{len(outstanding)} commitment(s) still reserved after compensation: {', '.join(outstanding[:5])}"
        )
    with postgres().begin() as conn:
        _audit(conn, SERVICE_USER, "plan_version", plan_version_id, "COMMITMENTS_RELEASED",
               {"revision": revision})
    return outstanding


@activity.defn
def mark_compensation_failed(plan_version_id: str, revision: int, detail: str) -> None:
    """Record that the two surfaces could not be reconciled automatically.

    Reached only when the rollback itself has exhausted its retries. The run
    still fails loudly; this exists so the disagreement is written down where
    an operator will find it rather than living only in workflow history.
    """
    with postgres().begin() as conn:
        conn.execute(
            text(
                f"UPDATE {SCHEMA}.plan_publication "
                "SET state = 'COMPENSATION_FAILED', settled_at = now() "
                "WHERE plan_version_id = CAST(:pv AS uuid) AND revision = :revision"
            ),
            {"pv": plan_version_id, "revision": revision},
        )
        _audit(conn, SERVICE_USER, "plan_version", plan_version_id, "COMPENSATION_FAILED",
               {"revision": revision, "detail": detail[:2000]})
    log.error("compensation failed for revision %s: %s", revision, detail)


# --------------------------------------------------------------------------
# Variance and run bookkeeping
# --------------------------------------------------------------------------
@activity.defn
def compute_variance(
    plan_version_id: str, source_version_code: str, revision: int, scenario_codes: list[str]
) -> int:
    """Bridge the baseline to the published revision, one report per scenario.

    "Plan" is the frozen baseline and "actual" is what the re-forecast now
    says, so the bridge answers the question a re-forecast actually raises:
    which of price and volume moved the number, and by how much. The
    decomposition itself is ``fpa_project.dsl.bridge`` and stays a pure
    function; this activity is only the part that has to touch a database.

    FX is the same on both sides because a driver shock does not move rates and
    ``plan_fx_rate`` is pinned for the version. The bridge's FX leg is
    therefore zero here by construction, not by omission.
    """
    import uuid
    from fpa_project.dsl.bridge import BridgeLine, decompose
    from fpa_project.bridge_service import BridgeReport, _persist, DEFAULT_MATERIALITY

    ensure_cube_tables()
    client = cube()
    version = _quote(source_version_code)
    with postgres().connect() as conn:
        target_code = conn.execute(text(f"SELECT plan_version_code FROM {SCHEMA}.plan_version WHERE plan_version_id=CAST(:pv AS uuid)"), {"pv": plan_version_id}).scalar_one()
    reports = 0
    for scenario in scenario_codes:
        rows = client.query(
            "SELECT b.account, b.company, toString(b.period_month), b.dim_signature_hash, "
            "b.quantity, b.unit_price, b.amount_functional, s.quantity, s.unit_price, s.amount_functional, fx.rate, d.account_type "
            f"FROM (SELECT * FROM {BASELINE_TABLE} FINAL WHERE plan_version={version} AND scenario_id={_quote(scenario)}) b "
            f"INNER JOIN (SELECT * FROM {STAGED_TABLE} FINAL WHERE plan_version={version} AND revision={revision} AND scenario_id={_quote(scenario)}) s "
            "ON b.company=s.company AND b.period_month=s.period_month AND b.account=s.account AND b.dim_signature_hash=s.dim_signature_hash "
            f"LEFT JOIN (SELECT * FROM fpa_cube.dim_fx_plan WHERE plan_version={version}) fx ON fx.period_month=b.period_month AND fx.from_currency=b.functional_currency "
            "INNER JOIN fpa_cube.dim_account d ON d.account=b.account "
            "ORDER BY b.account,b.company,b.period_month,b.dim_signature_hash"
        ).result_rows
        if not rows:
            continue
        lines, citations = [], []
        for account, company, month, signature, pq, pp, pa, aq, ap, aa, fx, kind in rows:
            if Decimal(str(fx)) <= 0:
                raise permanent("missing or nonpositive plan FX rate in forecast bridge")
            signature = signature.decode() if isinstance(signature, bytes) else signature
            path = (account, company, month)
            key = (company, month, account, signature)
            lines.append(BridgeLine(path, Decimal(str(pq)), Decimal(str(aq)), Decimal(str(pp)), Decimal(str(ap)),
                                    Decimal(str(fx)), Decimal(str(fx)), account_type=kind, key=key,
                                    plan_amount=Decimal(str(pa)), actual_amount=Decimal(str(aa))))
            citations.append(dict(company_code=company,period_month=month,account_code=account,
                                  dim_signature_hash=signature,plan_amount=pa,actual_amount=aa,path=path,
                                  plan_quantity=Decimal(str(pq)), actual_quantity=Decimal(str(aq)),
                                  plan_unit_price=Decimal(str(pp)), actual_unit_price=Decimal(str(ap)),
                                  plan_fx=Decimal(str(fx)), actual_fx=Decimal(str(fx)), account_type=kind))
        result = decompose(lines, levels=("account", "company", "period_month"))
        report = BridgeReport(
            report_id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"fpa:forecast:{plan_version_id}:{revision}:{scenario}")),
            result=result, vintage=None, vintage_closed_at=None, vintage_note="Baseline versus reforecast; not a ledger vintage",
            dsl="", measure="forecast_margin_change", plan_version=target_code, scenario=scenario,
            status="ESCALATED" if abs(result.root.gap) >= DEFAULT_MATERIALITY else "OPEN",
            materiality_threshold=DEFAULT_MATERIALITY, citations=citations,
        )
        _persist(report, SERVICE_USER)
        reports += 1
    return reports


def _safe_divide(value: Any, quantity: Any) -> Decimal:
    """A weighted average price, or zero when nothing was planned.

    Zero quantity has no meaningful price. Returning zero keeps the leg
    explicit and deterministic instead of raising or producing a NaN that
    would propagate silently into the report.
    """
    quantity_decimal = Decimal(str(quantity or 0))
    if quantity_decimal == 0:
        return Decimal(0)
    return Decimal(str(value or 0)) / quantity_decimal


def _round2(value: Decimal) -> Decimal:
    from decimal import ROUND_HALF_UP

    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _variance_legs(result: Any, plan_amount: Decimal, actual_amount: Decimal) -> dict[str, Decimal]:
    """Shape a bridge result for ``variance_report_line``.

    The table has a check constraint that the legs sum to the gap. The
    decomposition already ties algebraically, but each leg has to be rounded to
    store it, and rounding six numbers independently breaks a tie by a cent
    often enough to matter. So the residual is recomputed from the *rounded*
    amounts: whatever is left after the explained legs lands there, which is
    what a residual is for.
    """
    price = _round2(result.price)
    volume = _round2(result.volume)
    mix = _round2(result.mix)
    fx = _round2(result.fx)
    residual = (actual_amount - plan_amount) - (price + volume + mix + fx)
    return {
        "price": price, "volume": volume, "mix": mix, "fx": fx,
        "rate": Decimal("0"), "efficiency": Decimal("0"),
        "residual": residual,
    }


@activity.defn
def open_run(
    workflow_id: str, run_id: str, plan_version_id: str, requested_by: str, shocks: list[list[Any]]
) -> None:
    """Record the run so it survives Temporal's retention and lists in the UI."""
    with postgres().begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO {SCHEMA}.recompute_run "
                "  (run_id, workflow_id, plan_version_id, requested_by, shocks, state, phase) "
                "VALUES (:run, :wf, CAST(:pv AS uuid), :who, CAST(:shocks AS jsonb), 'RUNNING', 'STARTING') "
                "ON CONFLICT (run_id) DO NOTHING"
            ),
            {
                "run": run_id, "wf": workflow_id, "pv": plan_version_id, "who": requested_by,
                "shocks": json.dumps(shocks, separators=(",", ":")),
            },
        )


@activity.defn
def update_run(
    run_id: str, state: str, phase: str, dirty_rows: int, processed_rows: int,
    decided_by: str | None = None, detail: str | None = None, ended: bool = False,
) -> None:
    """Mirror the workflow's progress into Postgres.

    The live truth while a run is going is the workflow's own query; this is
    the durable copy. It is deliberately a mirror and never a source: nothing
    in the workflow reads its way back out of here to decide what to do next.
    """
    with postgres().begin() as conn:
        conn.execute(
            text(
                f"UPDATE {SCHEMA}.recompute_run SET state = :state, phase = :phase, "
                "    dirty_rows = :dirty, processed_rows = :processed, "
                "    decided_by = COALESCE(:decided_by, decided_by), "
                "    detail = COALESCE(:detail, detail), "
                "    ended_at = CASE WHEN :ended THEN now() ELSE ended_at END "
                "WHERE run_id = :run"
            ),
            {
                "state": state, "phase": phase, "dirty": dirty_rows, "processed": processed_rows,
                "decided_by": decided_by, "detail": detail, "ended": ended, "run": run_id,
            },
        )


# --------------------------------------------------------------------------
# The covenant check on the recomputed lines (question-driven requests)
# --------------------------------------------------------------------------
@activity.defn
def evaluate_covenants(payload: CovenantInput) -> CovenantVerdict:
    """Measure every active covenant rule on the staged revision.

    "Before" is the plan currently in the cube; "after" is the same plan with
    this revision's staged rows in place of the rows they replace, so a check
    measures what this re-forecast would change. Every result is written to
    ``covenant_check`` (append-only; a retry finds its rows already there and
    decides from them), and only when all of them pass does the successor get
    ``covenant_ok``, set by this service identity under the 015 trigger that
    allows exactly that and nothing else.
    """
    from fpa_project.dsl.schema import Schema
    from . import covenants

    ensure_cube_tables()
    with postgres().begin() as conn:
        rules = [
            covenants.Rule(r[0], r[1], r[2], r[3], float(r[4]), r[5], tuple(r[6] or ()))
            for r in conn.execute(text(
                f"SELECT rule_code, metric, measure, comparator, threshold, scope, scenarios "
                f"FROM {SCHEMA}.covenant_rule WHERE active ORDER BY rule_code"
            ))
        ]
    if not rules:
        raise permanent("no active covenant rules; a re-forecast cannot be shown to meet its covenants")

    metrics = Schema().metrics
    revenue_accounts = metrics["services_revenue"]["accounts"]
    cost_accounts = metrics["delivery_cost"]["accounts"]
    in_scope = (
        "((length({companies:Array(String)}) = 0 OR has({companies:Array(String)}, l.company)) "
        " AND (length({months:Array(String)}) = 0 OR has({months:Array(String)}, toString(l.period_month))))"
    )
    after_amount = "toFloat64(if(s.revision = {revision:UInt32}, s.amount_functional, l.amount_functional))"
    rows = cube().query(
        f"SELECT l.scenario_id, {in_scope} AS in_scope, "
        "  sumIf(toFloat64(l.amount_functional) * fx.rate, l.account IN {rev:Array(String)}), "
        "  sumIf(toFloat64(l.amount_functional) * fx.rate, l.account IN {cost:Array(String)}), "
        f"  sumIf({after_amount} * fx.rate, l.account IN {{rev:Array(String)}}), "
        f"  sumIf({after_amount} * fx.rate, l.account IN {{cost:Array(String)}}) "
        f"FROM (SELECT * FROM {PLAN_TABLE} FINAL WHERE plan_version = {{pv:String}}) l "
        "LEFT JOIN (SELECT scenario_id, company, period_month, account, dim_signature_hash, amount_functional, revision "
        f"           FROM {STAGED_TABLE} FINAL WHERE plan_version = {{pv:String}} AND revision = {{revision:UInt32}}) s "
        "  ON s.scenario_id = l.scenario_id AND s.company = l.company AND s.period_month = l.period_month "
        "  AND s.account = l.account AND s.dim_signature_hash = l.dim_signature_hash "
        "LEFT JOIN (SELECT period_month, from_currency, any(rate) AS rate FROM fpa_cube.dim_fx_plan "
        "           WHERE plan_version = {pv:String} GROUP BY period_month, from_currency) fx "
        "  ON fx.period_month = l.period_month AND fx.from_currency = l.functional_currency "
        "WHERE l.scenario_id IN {scenarios:Array(String)} "
        "GROUP BY l.scenario_id, in_scope",
        parameters={
            "pv": payload.plan_version_code, "revision": payload.revision,
            "companies": list(payload.companies), "months": [m[:10] for m in payload.months],
            "rev": revenue_accounts, "cost": cost_accounts, "scenarios": list(payload.scenario_codes),
        },
    ).result_rows

    totals: dict[tuple[str, str], list[float]] = {}
    for scenario, inside, b_rev, b_cost, a_rev, a_cost in rows:
        scopes = ("PLAN", "REQUEST") if inside else ("PLAN",)
        for scope in scopes:
            acc = totals.setdefault((scenario, scope), [0.0, 0.0, 0.0, 0.0])
            for i, value in enumerate((b_rev, b_cost, a_rev, a_cost)):
                acc[i] += float(value or 0)
    figures = {key: covenants.Figures(*values) for key, values in totals.items()}
    checks = covenants.evaluate(rules, figures)
    if not checks:
        raise permanent("the re-forecast touches no line any covenant rule measures")

    with postgres().begin() as conn:
        conn.execute(text("SELECT set_config('fpa.actor', :who, true)"), {"who": SERVICE_USER})
        conn.execute(
            text(
                f"INSERT INTO {SCHEMA}.covenant_check "
                "  (run_id, workflow_id, request_id, plan_version_id, revision, rule_code, scenario_code, scope, "
                "   metric, measure, before_value, after_value, measured_value, comparator, threshold, passed) "
                "VALUES (:run, :wf, CAST(NULLIF(:request, '') AS uuid), CAST(:pv AS uuid), :revision, :rule, :scenario, "
                "        :scope, :metric, :measure, :before, :after, :measured, :comparator, :threshold, :passed) "
                "ON CONFLICT (run_id, revision, rule_code, scenario_code) DO NOTHING"
            ),
            [
                {"run": payload.run_id, "wf": payload.workflow_id, "request": payload.request_id,
                 "pv": payload.target_version_id, "revision": payload.revision, "rule": c.rule_code,
                 "scenario": c.scenario_code, "scope": c.scope, "metric": c.metric, "measure": c.measure,
                 "before": c.before_value, "after": c.after_value, "measured": c.measured_value,
                 "comparator": c.comparator, "threshold": c.threshold, "passed": c.passed}
                for c in checks
            ],
        )
        # Decide from what is recorded, so a retry after a crash reaches the
        # same verdict the first attempt wrote down.
        recorded = conn.execute(
            text(
                f"SELECT rule_code, scenario_code, measured_value, comparator, threshold, passed "
                f"FROM {SCHEMA}.covenant_check WHERE run_id = :run AND revision = :revision ORDER BY rule_code, scenario_code"
            ),
            {"run": payload.run_id, "revision": payload.revision},
        ).all()
        failures = covenants.describe_failures([
            covenants.Check(r[0], r[1], "", "", "", None, None, None if r[2] is None else float(r[2]),
                            r[3], float(r[4]), bool(r[5]))
            for r in recorded
        ])
        passed = not failures
        if passed:
            conn.execute(
                text(
                    f"UPDATE {SCHEMA}.plan_version SET covenant_ok = true, covenant_note = :note, updated_at = now() "
                    "WHERE plan_version_id = CAST(:pv AS uuid) AND covenant_ok IS DISTINCT FROM true"
                ),
                {"pv": payload.target_version_id,
                 "note": f"automated covenant check passed: {len(recorded)} checks on revision {payload.revision}"},
            )
        _audit(conn, SERVICE_USER, "plan_version", payload.target_version_id,
               "COVENANT_PASSED" if passed else "COVENANT_BREACH",
               {"revision": payload.revision, "checks": len(recorded), "failures": failures,
                "request_id": payload.request_id or None})
    return CovenantVerdict(passed=passed, checks=len(recorded), failures=failures)


@activity.defn
def discard_staged(plan_version_code: str, revision: int) -> None:
    """Drop the staged rows for a revision that will never be published.

    Called on a rejection, an expiry and a cancellation. The draft lines in
    Postgres stay, because they are the record of what was proposed; the
    staged cube rows go, because they are only ever a step on the way to a
    publish that is not going to happen.
    """
    ensure_cube_tables()
    cube().command(
        f"ALTER TABLE {STAGED_TABLE} DELETE WHERE plan_version = {_quote(plan_version_code)} "
        f"AND revision = {revision}",
        # Wait for the delete. Asynchronous, it left staged rows visible for
        # seconds after the run reported CANCELLED or REJECTED, and a quick
        # re-run that reused the revision could have its fresh rows deleted
        # by the old mutation landing late.
        settings={"mutations_sync": 2},
    )


ALL = [
    load_plan_context, snapshot_drivers, reserve_revision, committed_shocks, supersede_commitments, ensure_target_version,
    snapshot_baseline, resolve_dirty_set, evaluate_partition,
    record_submission, open_review, record_approval, record_lock, record_rejection, verify_publishable, snapshot_preimage,
    publish_to_cube, unpublish_revision, commit_to_treasury, compensate_commitments,
    mark_compensation_failed, compute_variance, open_run, update_run, discard_staged, evaluate_covenants,
]

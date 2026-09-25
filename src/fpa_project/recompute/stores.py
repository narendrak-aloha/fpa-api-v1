"""Connections and the cube's recompute-side tables.

Activities run on a worker that outlives any one of them, so connections are
built once and reused: the Postgres engine per process (it pools), the
ClickHouse client per thread (it must not be shared). Nothing here is imported by
``workflows.py``: a workflow that can reach a connection pool is a workflow
that can break determinism by accident.

Three tables sit beside ``fact_plan_line``, and each exists for one reason:

``fact_plan_line_baseline``
    The frozen input. Written once per plan version and never updated, so the
    same shock asked for twice reads the same starting numbers instead of
    compounding onto its own last answer.

``fact_plan_line_staged``
    The computed output, tagged with the revision it belongs to, written before
    the approval wait and published unchanged afterwards. The arithmetic
    happens once, in Python, and both Postgres and the cube receive those exact
    values -- rather than each rounding for itself and disagreeing by a cent.

``fact_plan_line_preimage``
    What a publish is about to overwrite. This is what lets compensation put
    the cube back exactly, instead of deleting a revision and losing whatever
    was underneath it.
"""

from __future__ import annotations

import threading
from typing import Any

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

from db.config import database_url
from fpa_project.config import clickhouse as clickhouse_settings

CUBE = "fpa_cube"
PLAN_TABLE = f"{CUBE}.fact_plan_line"
BASELINE_TABLE = f"{CUBE}.fact_plan_line_baseline"
STAGED_TABLE = f"{CUBE}.fact_plan_line_staged"
PREIMAGE_TABLE = f"{CUBE}.fact_plan_line_preimage"
_RECOMPUTE_TABLES = (BASELINE_TABLE, STAGED_TABLE, PREIMAGE_TABLE)

# The 19-dimension planning grain, in the cube's own column order. Copied from
# data/seed_fpa.py because the two have to agree exactly or dim_signature_hash
# stops tying plan to actual.
DIM_COLUMNS: tuple[str, ...] = (
    "billing_type", "business_unit", "channel", "contract", "cost_center", "cost_pool",
    "customer", "delivery_shore", "engine", "funding_source", "geo_country", "geo_region",
    "grade", "intercompany_flag", "practice", "product", "project", "resource_employee",
    "revenue_type",
)
# High cardinality, so plain String rather than LowCardinality.
_HIGH_CARDINALITY = frozenset({"project", "customer", "resource_employee", "contract"})

PLAN_COLUMNS: tuple[str, ...] = (
    "plan_version", "scenario_id", "revision", "company", "period_month", "account",
    *DIM_COLUMNS, "dim_signature_hash", "quantity", "unit_price", "amount_functional",
    "functional_currency", "plan_line_type",
)

_SORT_KEY = "plan_version, scenario_id, company, period_month, account, dim_signature_hash"

_lock = threading.Lock()
_engine: Engine | None = None
_cube_local = threading.local()


def _plan_line_columns_ddl() -> str:
    dims = ",\n    ".join(
        f"{column} {'String' if column in _HIGH_CARDINALITY else 'LowCardinality(String)'}"
        for column in DIM_COLUMNS
    )
    return f"""plan_version        LowCardinality(String),
    scenario_id         LowCardinality(String),
    revision            UInt32,
    company             LowCardinality(String),
    period_month        Date,
    account             LowCardinality(String),
    {dims},
    dim_signature_hash  FixedString(16),
    quantity            Float64,
    unit_price          Float64,
    amount_functional   Decimal(18, 2),
    functional_currency LowCardinality(String),
    plan_line_type      LowCardinality(String)"""


def postgres() -> Engine:
    """The governance store. ``pool_pre_ping`` because a worker can sit idle
    across an approval wait far longer than Postgres keeps a connection open."""
    global _engine
    with _lock:
        if _engine is None:
            # Sized to the worker's 16 activity threads, so a busy fan-out never
            # waits on the pool and times out into a spurious retry.
            _engine = create_engine(database_url(), pool_pre_ping=True, pool_size=8, max_overflow=8)
        return _engine


def cube() -> Any:
    """A ClickHouse client for the calling thread.

    One per thread, not one per process: clickhouse-connect refuses concurrent
    queries on the same session ("Attempt to execute concurrent queries within
    the same session"), and the worker runs activities on a thread pool. A
    single shared client made parallel partitions fail and succeed only on a
    later retry -- found against the real stack, where it cost 111 failed
    attempts across four runs.
    """
    client = getattr(_cube_local, "client", None)
    if client is None:
        import clickhouse_connect
        from clickhouse_connect.datatypes.format import set_default_formats

        # dim_signature_hash is a FixedString(16), and clickhouse-connect
        # hands those back as *bytes* by default. That would go straight
        # into a CHAR(16) column whose check constraint is '^[0-9a-f]{16}$'
        # and fail there, a long way from the cause. Ask for str instead.
        set_default_formats("FixedString", "string")

        settings = clickhouse_settings()
        client = clickhouse_connect.get_client(
            host=settings.host, port=settings.port,
            username=settings.user, password=settings.password,
        )
        _cube_local.client = client
    return client


def ensure_cube_tables() -> None:
    """Create the three recompute tables if they are not there yet.

    They belong to the recompute rather than to the seeder, so the cube can be
    seeded, dropped and reseeded without knowing the workflow exists. That is
    also why this asks ClickHouse on every call instead of remembering it once
    did the work: a reseed drops the whole database under a worker that keeps
    running, and a process-wide "done" flag then sends every activity at a
    table that is gone, retry after retry. The check is one metadata query;
    the DDL, ``IF NOT EXISTS`` throughout, runs only when something is missing.
    """
    client = cube()
    present = client.query(
        "SELECT count() FROM system.tables WHERE database = {db:String} AND name IN {names:Array(String)}",
        parameters={"db": CUBE, "names": [table.split(".", 1)[1] for table in _RECOMPUTE_TABLES]},
    ).result_rows[0][0]
    if present == len(_RECOMPUTE_TABLES):
        return
    columns = _plan_line_columns_ddl()
    client.command(
        f"CREATE TABLE IF NOT EXISTS {BASELINE_TABLE} ({columns}) "
        f"ENGINE = ReplacingMergeTree(revision) "
        f"PARTITION BY toYYYYMM(period_month) ORDER BY ({_SORT_KEY})"
    )
    # revision leads the sort key so one revision's staged rows can be read,
    # published and dropped without touching another's.
    client.command(
        f"CREATE TABLE IF NOT EXISTS {STAGED_TABLE} ({columns}) "
        f"ENGINE = ReplacingMergeTree "
        f"PARTITION BY toYYYYMM(period_month) "
        f"ORDER BY (plan_version, revision, scenario_id, company, period_month, account, dim_signature_hash)"
    )
    # preimage_for_revision is the publish this snapshot protects; revision
    # stays the row's own, so a restore puts back exactly what was there.
    client.command(
        f"CREATE TABLE IF NOT EXISTS {PREIMAGE_TABLE} ("
        f"    preimage_for_revision UInt32,\n    {columns}) "
        f"ENGINE = ReplacingMergeTree "
        f"PARTITION BY toYYYYMM(period_month) "
        f"ORDER BY (plan_version, preimage_for_revision, scenario_id, company, period_month, account, dim_signature_hash)"
    )


def reset_for_tests() -> None:
    """Drop the cached handles so a test can point the module somewhere else."""
    global _engine
    with _lock:
        _engine = None
        _cube_local.__dict__.clear()

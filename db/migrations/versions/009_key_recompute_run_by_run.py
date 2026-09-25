"""key recompute_run by run, not by workflow id

The workflow id is ``recompute-<plan version>``, reused by every re-forecast of
that plan version, so keying the run record on it kept exactly one row per plan
version: every later run landed on the first run's row via ON CONFLICT DO
NOTHING and then overwrote its state. Found against the running stack, where
ten runs had left one row with the first run's start time.

``run_id`` is the run's ``first_execution_run_id``, which is unique per
re-forecast and stays the same across continue-as-new, so one logical run is
one row however many executions it takes.

Revision ID: 009
Revises: 008
Create Date: 2026-09-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "009"
down_revision: Union[str, Sequence[str], None] = "008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "fpa_governance"


def upgrade() -> None:
    op.add_column("recompute_run", sa.Column("run_id", sa.Text(), nullable=True), schema=SCHEMA)
    # Rows written before this migration have no run id; the workflow id is the
    # only identity they ever had, and it is unique among them by construction.
    op.execute(f"UPDATE {SCHEMA}.recompute_run SET run_id = 'legacy:' || workflow_id WHERE run_id IS NULL")
    op.alter_column("recompute_run", "run_id", nullable=False, schema=SCHEMA)
    op.drop_constraint("pk_recompute_run", "recompute_run", schema=SCHEMA, type_="primary")
    op.create_primary_key("pk_recompute_run", "recompute_run", ["run_id"], schema=SCHEMA)
    op.create_index("recompute_run_workflow_idx", "recompute_run", ["workflow_id", "started_at"], schema=SCHEMA)


def downgrade() -> None:
    # Only reversible while each workflow id still has a single run.
    op.drop_index("recompute_run_workflow_idx", table_name="recompute_run", schema=SCHEMA)
    op.drop_constraint("pk_recompute_run", "recompute_run", schema=SCHEMA, type_="primary")
    op.create_primary_key("pk_recompute_run", "recompute_run", ["workflow_id"], schema=SCHEMA)
    op.drop_column("recompute_run", "run_id", schema=SCHEMA)

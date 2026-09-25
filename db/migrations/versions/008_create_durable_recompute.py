"""create durable recompute tables

Driver-to-line bindings for the recompute engine, the Temporal run record, and
the publication ledger that allocates cube revisions and tracks compensation.

Revision ID: 008
Revises: 007
Create Date: 2026-09-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "008"
down_revision: Union[str, Sequence[str], None] = "007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "fpa_governance"


def upgrade() -> None:
    op.create_table(
        "plan_driver_binding",
        sa.Column("binding_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("driver_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("account_code", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("elasticity", sa.Numeric(10, 6), server_default=sa.text("1"), nullable=False),
        sa.CheckConstraint("target IN ('quantity', 'unit_price')", name="ck_plan_driver_binding_target"),
        sa.CheckConstraint("elasticity >= 0", name="ck_plan_driver_binding_elasticity_non_negative"),
        sa.ForeignKeyConstraint(["driver_id"], [f"{SCHEMA}.plan_driver.driver_id"], name="fk_plan_driver_binding_driver_id_plan_driver", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_code"], [f"{SCHEMA}.dim_account.account_code"], name="fk_plan_driver_binding_account_code_dim_account"),
        sa.PrimaryKeyConstraint("binding_id", name="pk_plan_driver_binding"),
        sa.UniqueConstraint("driver_id", "account_code", "target", name="uq_plan_driver_binding_driver_id"),
        schema=SCHEMA,
    )

    op.create_table(
        "recompute_run",
        sa.Column("workflow_id", sa.Text(), nullable=False),
        sa.Column("plan_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("shocks", postgresql.JSONB(), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'RUNNING'"), nullable=False),
        sa.Column("phase", sa.Text(), server_default=sa.text("'STARTING'"), nullable=False),
        sa.Column("dirty_rows", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("processed_rows", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('RUNNING', 'AWAITING_APPROVAL', 'PUBLISHING', 'COMPLETED', "
            "'REJECTED', 'EXPIRED', 'CANCELLED', 'COMPENSATED', 'FAILED')",
            name="ck_recompute_run_state",
        ),
        sa.CheckConstraint("processed_rows >= 0 AND dirty_rows >= 0", name="ck_recompute_run_counts_non_negative"),
        sa.ForeignKeyConstraint(["plan_version_id"], [f"{SCHEMA}.plan_version.plan_version_id"], name="fk_recompute_run_plan_version_id_plan_version"),
        sa.ForeignKeyConstraint(["requested_by"], [f"{SCHEMA}.app_user.user_id"], name="fk_recompute_run_requested_by_app_user"),
        sa.ForeignKeyConstraint(["decided_by"], [f"{SCHEMA}.app_user.user_id"], name="fk_recompute_run_decided_by_app_user"),
        sa.PrimaryKeyConstraint("workflow_id", name="pk_recompute_run"),
        schema=SCHEMA,
    )
    op.create_index("recompute_run_plan_idx", "recompute_run", ["plan_version_id", "started_at"], schema=SCHEMA)

    op.create_table(
        "plan_publication",
        sa.Column("publication_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("plan_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("workflow_id", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'RESERVED'"), nullable=False),
        sa.Column("row_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("commitment_ids", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("revision > 0", name="ck_plan_publication_revision_positive"),
        sa.CheckConstraint(
            "state IN ('RESERVED', 'PUBLISHED', 'COMMITTED', 'COMPENSATED', 'COMPENSATION_FAILED')",
            name="ck_plan_publication_state",
        ),
        sa.ForeignKeyConstraint(["plan_version_id"], [f"{SCHEMA}.plan_version.plan_version_id"], name="fk_plan_publication_plan_version_id_plan_version"),
        sa.PrimaryKeyConstraint("publication_id", name="pk_plan_publication"),
        sa.UniqueConstraint("plan_version_id", "revision", name="uq_plan_publication_plan_version_id"),
        sa.UniqueConstraint("plan_version_id", "idempotency_key", name="uq_plan_publication_idempotency"),
        schema=SCHEMA,
    )

    # plan_publication deliberately carries no lock guard. The 004 guard stops
    # writes to a LOCKED version's lines, but publication and compensation both
    # happen after the lock, so this ledger has to stay writable.


def downgrade() -> None:
    op.drop_table("plan_publication", schema=SCHEMA)
    op.drop_index("recompute_run_plan_idx", table_name="recompute_run", schema=SCHEMA)
    op.drop_table("recompute_run", schema=SCHEMA)
    op.drop_table("plan_driver_binding", schema=SCHEMA)

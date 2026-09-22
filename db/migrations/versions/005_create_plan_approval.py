"""create plan approval

Approval requests and decisions, with the trigger that blocks self-approval
and approval while the covenant is not satisfied.

Revision ID: 005
Revises: 004
Create Date: 2026-09-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "005"
down_revision: Union[str, Sequence[str], None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "fpa_governance"


def upgrade() -> None:
    op.create_table(
        "plan_approval",
        sa.Column("approval_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("plan_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("decided_by", sa.Text(), nullable=True),
        sa.Column("decision", sa.Text(), server_default=sa.text("'PENDING'"), nullable=False),
        sa.Column("covenant_ok", sa.Boolean(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("decision IN ('PENDING', 'APPROVED', 'REJECTED')", name=op.f("ck_plan_approval_decision")),
        sa.CheckConstraint("decided_by IS NULL OR decided_by <> requested_by", name=op.f("ck_plan_approval_no_self_approval")),
        sa.CheckConstraint(
            "(decision = 'PENDING' AND decided_by IS NULL AND decided_at IS NULL) OR "
            "(decision <> 'PENDING' AND decided_by IS NOT NULL AND decided_at IS NOT NULL)",
            name=op.f("ck_plan_approval_decision_complete"),
        ),
        sa.ForeignKeyConstraint(["plan_version_id"], [f"{SCHEMA}.plan_version.plan_version_id"], name="fk_plan_approval_plan_version_id_plan_version"),
        sa.ForeignKeyConstraint(["requested_by"], [f"{SCHEMA}.app_user.user_id"], name="fk_plan_approval_requested_by_app_user"),
        sa.ForeignKeyConstraint(["decided_by"], [f"{SCHEMA}.app_user.user_id"], name="fk_plan_approval_decided_by_app_user"),
        sa.PrimaryKeyConstraint("approval_id", name="pk_plan_approval"),
        schema=SCHEMA,
    )
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.validate_plan_approval() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.decision = 'APPROVED' AND NOT NEW.covenant_ok THEN
                RAISE EXCEPTION 'approval blocked: covenant is not satisfied';
            END IF;
            IF NEW.decision = 'APPROVED' AND NEW.decided_by = NEW.requested_by THEN
                RAISE EXCEPTION 'approval blocked: requester cannot approve their own plan';
            END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute(f"""
        CREATE TRIGGER plan_approval_guard BEFORE INSERT OR UPDATE ON {SCHEMA}.plan_approval
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.validate_plan_approval();
    """)


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS plan_approval_guard ON {SCHEMA}.plan_approval")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.validate_plan_approval()")
    op.drop_table("plan_approval", schema=SCHEMA)

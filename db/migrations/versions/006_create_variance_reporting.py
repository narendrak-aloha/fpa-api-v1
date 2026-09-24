"""create variance reporting

Persisted variance reports pinned to a ledger vintage, and their lines whose
legs must tie to the plan/actual gap.

Revision ID: 006
Revises: 005
Create Date: 2026-09-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "006"
down_revision: Union[str, Sequence[str], None] = "005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "fpa_governance"
LEGS = ("price_variance", "volume_variance", "mix_variance", "fx_variance", "rate_variance", "efficiency_variance", "residual")


def upgrade() -> None:
    op.create_table(
        "variance_report",
        sa.Column("variance_report_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("plan_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scenario_code", sa.Text(), nullable=False),
        sa.Column("as_of_vintage", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'OPEN'"), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("closed_by", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('OPEN', 'REVIEWED', 'CLOSED')", name=op.f("ck_variance_report_status")),
        sa.CheckConstraint("status <> 'CLOSED' OR (closed_by IS NOT NULL AND closed_at IS NOT NULL)", name=op.f("ck_variance_report_closed_requires_closer")),
        sa.ForeignKeyConstraint(["plan_version_id"], [f"{SCHEMA}.plan_version.plan_version_id"], name="fk_variance_report_plan_version_id_plan_version"),
        sa.ForeignKeyConstraint(["as_of_vintage"], [f"{SCHEMA}.ledger_vintage.vintage"], name="fk_variance_report_as_of_vintage_ledger_vintage"),
        sa.ForeignKeyConstraint(["created_by"], [f"{SCHEMA}.app_user.user_id"], name="fk_variance_report_created_by_app_user"),
        sa.ForeignKeyConstraint(["closed_by"], [f"{SCHEMA}.app_user.user_id"], name="fk_variance_report_closed_by_app_user"),
        sa.PrimaryKeyConstraint("variance_report_id", name="pk_variance_report"),
        schema=SCHEMA,
    )
    op.create_table(
        "variance_report_line",
        sa.Column("variance_report_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_no", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("dimension_key", postgresql.JSONB(), nullable=False),
        sa.Column("plan_amount", sa.Numeric(20, 2), nullable=False),
        sa.Column("actual_amount", sa.Numeric(20, 2), nullable=False),
        *[sa.Column(leg, sa.Numeric(20, 2), server_default=sa.text("0"), nullable=False) for leg in LEGS],
        sa.CheckConstraint("line_no > 0", name=op.f("ck_variance_report_line_line_no_positive")),
        sa.CheckConstraint("jsonb_typeof(dimension_key) = 'object'", name=op.f("ck_variance_report_line_dimension_key_object")),
        sa.CheckConstraint(
            "round(actual_amount - plan_amount, 2) = round(price_variance + volume_variance + mix_variance + "
            "fx_variance + rate_variance + efficiency_variance + residual, 2)",
            name=op.f("ck_variance_report_line_legs_tie_to_gap"),
        ),
        sa.ForeignKeyConstraint(["variance_report_id"], [f"{SCHEMA}.variance_report.variance_report_id"], name="fk_variance_report_line_variance_report_id_variance_report", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("variance_report_id", "line_no", name="pk_variance_report_line"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("variance_report_line", schema=SCHEMA)
    op.drop_table("variance_report", schema=SCHEMA)

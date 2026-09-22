"""create planning model registry

Planning models with their dimensions, measures and effective-dated drivers.

Revision ID: 003
Revises: 002
Create Date: 2026-09-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "003"
down_revision: Union[str, Sequence[str], None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "fpa_governance"


def upgrade() -> None:
    op.create_table(
        "planning_model",
        sa.Column("model_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("model_code", sa.Text(), nullable=False),
        sa.Column("model_name", sa.Text(), nullable=False),
        sa.Column("plan_year", sa.Integer(), nullable=False),
        sa.Column("reporting_currency", sa.CHAR(3), nullable=False),
        sa.Column("calc_order_dag", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("plan_year BETWEEN 2000 AND 2200", name=op.f("ck_planning_model_plan_year_range")),
        sa.ForeignKeyConstraint(["created_by"], [f"{SCHEMA}.app_user.user_id"], name="fk_planning_model_created_by_app_user"),
        sa.PrimaryKeyConstraint("model_id", name="pk_planning_model"),
        sa.UniqueConstraint("model_code", name="uq_planning_model_model_code"),
        schema=SCHEMA,
    )
    op.create_table(
        "planning_dimension",
        sa.Column("model_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dimension_code", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.SmallInteger(), nullable=False),
        sa.CheckConstraint("ordinal > 0", name=op.f("ck_planning_dimension_ordinal_positive")),
        sa.ForeignKeyConstraint(["model_id"], [f"{SCHEMA}.planning_model.model_id"], name="fk_planning_dimension_model_id_planning_model", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("model_id", "dimension_code", name="pk_planning_dimension"),
        sa.UniqueConstraint("model_id", "ordinal", name="uq_planning_dimension_model_ordinal"),
        schema=SCHEMA,
    )
    op.create_table(
        "planning_measure",
        sa.Column("model_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("measure_code", sa.Text(), nullable=False),
        sa.Column("aggregation_type", sa.Text(), nullable=False),
        sa.Column("sql_expression", sa.Text(), nullable=False),
        sa.Column("available", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.CheckConstraint("aggregation_type IN ('additive', 'ratio', 'semi_additive')", name=op.f("ck_planning_measure_aggregation_type")),
        sa.ForeignKeyConstraint(["model_id"], [f"{SCHEMA}.planning_model.model_id"], name="fk_planning_measure_model_id_planning_model", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("model_id", "measure_code", name="pk_planning_measure"),
        schema=SCHEMA,
    )
    op.create_table(
        "plan_driver",
        sa.Column("driver_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("model_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("driver_code", sa.Text(), nullable=False),
        sa.Column("driver_name", sa.Text(), nullable=False),
        sa.Column("formula", sa.Text(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("unit", sa.Text(), nullable=False),
        sa.Column("value_type", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'ACTIVE'"), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("value_type IN ('numeric', 'percentage', 'currency', 'count')", name=op.f("ck_plan_driver_value_type")),
        sa.CheckConstraint("status IN ('DRAFT', 'ACTIVE', 'RETIRED')", name=op.f("ck_plan_driver_status")),
        sa.CheckConstraint("effective_to IS NULL OR effective_to > effective_from", name=op.f("ck_plan_driver_effective_range")),
        sa.ForeignKeyConstraint(["model_id"], [f"{SCHEMA}.planning_model.model_id"], name="fk_plan_driver_model_id_planning_model"),
        sa.ForeignKeyConstraint(["created_by"], [f"{SCHEMA}.app_user.user_id"], name="fk_plan_driver_created_by_app_user"),
        sa.PrimaryKeyConstraint("driver_id", name="pk_plan_driver"),
        sa.UniqueConstraint("model_id", "driver_code", "effective_from", name="uq_plan_driver_code_effective_from"),
        schema=SCHEMA,
    )
    op.create_index("driver_effective_idx", "plan_driver", ["model_id", "driver_code", "effective_from", "effective_to"], schema=SCHEMA)


def downgrade() -> None:
    op.drop_index("driver_effective_idx", table_name="plan_driver", schema=SCHEMA)
    op.drop_table("plan_driver", schema=SCHEMA)
    op.drop_table("planning_measure", schema=SCHEMA)
    op.drop_table("planning_dimension", schema=SCHEMA)
    op.drop_table("planning_model", schema=SCHEMA)

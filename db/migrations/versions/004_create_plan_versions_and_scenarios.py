"""create plan versions and scenarios

Plan state machine, plan versions, scenario sets as driver overrides, pinned
plan FX rates and plan lines, plus the trigger that refuses writes to LOCKED
versions (including from a direct psql session).

Revision ID: 004
Revises: 003
Create Date: 2026-09-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "004"
down_revision: Union[str, Sequence[str], None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "fpa_governance"


def upgrade() -> None:
    op.create_table(
        "plan_state_transition",
        sa.Column("transition_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("from_state", sa.Text(), nullable=False),
        sa.Column("to_state", sa.Text(), nullable=False),
        sa.Column("role_code", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["role_code"], [f"{SCHEMA}.role.role_code"], name="fk_plan_state_transition_role_code_role"),
        sa.PrimaryKeyConstraint("transition_id", name="pk_plan_state_transition"),
        sa.UniqueConstraint("from_state", "to_state", "role_code", name="uq_plan_state_transition_rule"),
        schema=SCHEMA,
    )
    op.create_table(
        "plan_version",
        sa.Column("plan_version_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("plan_version_code", sa.Text(), nullable=False),
        sa.Column("model_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("plan_year", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'DRAFT'"), nullable=False),
        sa.Column("covenant_ok", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("covenant_note", sa.Text(), nullable=True),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("approved_by", sa.Text(), nullable=True),
        sa.Column("supersedes_plan_version_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("plan_year BETWEEN 2000 AND 2200", name=op.f("ck_plan_version_plan_year_range")),
        sa.CheckConstraint("state IN ('DRAFT', 'IN_REVIEW', 'APPROVED', 'LOCKED', 'SUPERSEDED', 'REJECTED')", name=op.f("ck_plan_version_state")),
        sa.CheckConstraint("revision > 0", name=op.f("ck_plan_version_revision_positive")),
        sa.CheckConstraint("approved_by IS NULL OR approved_by <> requested_by", name=op.f("ck_plan_version_no_self_approval")),
        sa.CheckConstraint("state NOT IN ('APPROVED', 'LOCKED') OR covenant_ok", name=op.f("ck_plan_version_covenant_before_approval")),
        sa.CheckConstraint("state NOT IN ('APPROVED', 'LOCKED') OR approved_by IS NOT NULL", name=op.f("ck_plan_version_approver_required")),
        sa.ForeignKeyConstraint(["model_id"], [f"{SCHEMA}.planning_model.model_id"], name="fk_plan_version_model_id_planning_model"),
        sa.ForeignKeyConstraint(["requested_by"], [f"{SCHEMA}.app_user.user_id"], name="fk_plan_version_requested_by_app_user"),
        sa.ForeignKeyConstraint(["approved_by"], [f"{SCHEMA}.app_user.user_id"], name="fk_plan_version_approved_by_app_user"),
        sa.ForeignKeyConstraint(["supersedes_plan_version_id"], [f"{SCHEMA}.plan_version.plan_version_id"], name="fk_plan_version_supersedes_plan_version_id_plan_version"),
        sa.PrimaryKeyConstraint("plan_version_id", name="pk_plan_version"),
        sa.UniqueConstraint("plan_version_code", name="uq_plan_version_plan_version_code"),
        schema=SCHEMA,
    )
    op.create_table(
        "scenario_set",
        sa.Column("scenario_set_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("plan_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scenario_code", sa.Text(), nullable=False),
        sa.Column("scenario_name", sa.Text(), nullable=False),
        sa.Column("is_base", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'DRAFT'"), nullable=False),
        sa.CheckConstraint("scenario_code ~ '^[a-z][a-z0-9_]*$'", name=op.f("ck_scenario_set_scenario_code_format")),
        sa.CheckConstraint("state IN ('DRAFT', 'APPROVED', 'LOCKED')", name=op.f("ck_scenario_set_state")),
        sa.ForeignKeyConstraint(["plan_version_id"], [f"{SCHEMA}.plan_version.plan_version_id"], name="fk_scenario_set_plan_version_id_plan_version", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("scenario_set_id", name="pk_scenario_set"),
        sa.UniqueConstraint("plan_version_id", "scenario_code", name="uq_scenario_set_plan_scenario"),
        schema=SCHEMA,
    )
    op.create_index("one_base_scenario_per_plan", "scenario_set", ["plan_version_id"], unique=True, schema=SCHEMA, postgresql_where=sa.text("is_base"))
    op.create_table(
        "scenario_driver_override",
        sa.Column("scenario_set_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("driver_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("override_value", sa.Numeric(20, 6), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["scenario_set_id"], [f"{SCHEMA}.scenario_set.scenario_set_id"], name="fk_scenario_driver_override_scenario_set_id_scenario_set", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["driver_id"], [f"{SCHEMA}.plan_driver.driver_id"], name="fk_scenario_driver_override_driver_id_plan_driver"),
        sa.PrimaryKeyConstraint("scenario_set_id", "driver_id", name="pk_scenario_driver_override"),
        schema=SCHEMA,
    )
    op.create_table(
        "plan_fx_rate",
        sa.Column("plan_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("period_month", sa.Date(), nullable=False),
        sa.Column("from_currency", sa.CHAR(3), nullable=False),
        sa.Column("to_currency", sa.CHAR(3), server_default=sa.text("'USD'"), nullable=False),
        sa.Column("rate", sa.Numeric(20, 8), nullable=False),
        sa.CheckConstraint("period_month = date_trunc('month', period_month)::date", name=op.f("ck_plan_fx_rate_period_month_first_day")),
        sa.CheckConstraint("rate > 0", name=op.f("ck_plan_fx_rate_rate_positive")),
        sa.ForeignKeyConstraint(["plan_version_id"], [f"{SCHEMA}.plan_version.plan_version_id"], name="fk_plan_fx_rate_plan_version_id_plan_version", ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("plan_version_id", "period_month", "from_currency", "to_currency", name="pk_plan_fx_rate"),
        schema=SCHEMA,
    )
    op.create_table(
        "plan_version_line",
        sa.Column("plan_line_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("plan_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("scenario_code", sa.Text(), nullable=False),
        sa.Column("company_code", sa.Text(), nullable=False),
        sa.Column("period_month", sa.Date(), nullable=False),
        sa.Column("account_code", sa.Text(), nullable=False),
        sa.Column("dim_signature_hash", sa.CHAR(16), nullable=False),
        sa.Column("quantity", sa.Numeric(20, 6), nullable=False),
        sa.Column("unit_price", sa.Numeric(20, 6), nullable=False),
        sa.Column("amount_functional", sa.Numeric(20, 2), nullable=False),
        sa.Column("functional_currency", sa.CHAR(3), nullable=False),
        sa.Column("driver_derivation_trace", postgresql.JSONB(), nullable=False),
        sa.Column("source_revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("period_month = date_trunc('month', period_month)::date", name=op.f("ck_plan_version_line_period_month_first_day")),
        sa.CheckConstraint("dim_signature_hash ~ '^[0-9a-f]{16}$'", name=op.f("ck_plan_version_line_dim_signature_hash_format")),
        sa.CheckConstraint("jsonb_typeof(driver_derivation_trace) = 'object'", name=op.f("ck_plan_version_line_derivation_trace_object")),
        sa.CheckConstraint("driver_derivation_trace <> '{}'::jsonb", name=op.f("ck_plan_version_line_derivation_trace_not_empty")),
        sa.CheckConstraint("amount_functional = round(quantity * unit_price, 2)", name=op.f("ck_plan_version_line_amount_equals_quantity_x_price")),
        sa.ForeignKeyConstraint(["plan_version_id"], [f"{SCHEMA}.plan_version.plan_version_id"], name="fk_plan_version_line_plan_version_id_plan_version", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_code"], [f"{SCHEMA}.dim_company.company_code"], name="fk_plan_version_line_company_code_dim_company"),
        sa.ForeignKeyConstraint(["account_code"], [f"{SCHEMA}.dim_account.account_code"], name="fk_plan_version_line_account_code_dim_account"),
        sa.PrimaryKeyConstraint("plan_line_id", name="pk_plan_version_line"),
        sa.UniqueConstraint(
            "plan_version_id", "scenario_code", "company_code", "period_month", "account_code", "dim_signature_hash",
            name="uq_plan_version_line_grain",
        ),
        schema=SCHEMA,
    )
    op.create_index("plan_line_lookup_idx", "plan_version_line", ["plan_version_id", "scenario_code", "period_month", "company_code", "account_code"], schema=SCHEMA)

    op.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.reject_locked_plan_write() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE current_state text;
        BEGIN
            IF TG_TABLE_NAME = 'plan_version' THEN
                current_state := OLD.state;
            ELSE
                SELECT state INTO current_state FROM {SCHEMA}.plan_version WHERE plan_version_id = OLD.plan_version_id;
            END IF;
            IF current_state = 'LOCKED' THEN
                RAISE EXCEPTION 'plan version is locked; create a superseding version instead';
            END IF;
            RETURN COALESCE(NEW, OLD);
        END $$;
    """)
    op.execute(f"""
        CREATE TRIGGER plan_version_lock_guard BEFORE UPDATE OR DELETE ON {SCHEMA}.plan_version
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_locked_plan_write();
    """)
    op.execute(f"""
        CREATE TRIGGER plan_line_lock_guard BEFORE UPDATE OR DELETE ON {SCHEMA}.plan_version_line
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_locked_plan_write();
    """)


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS plan_line_lock_guard ON {SCHEMA}.plan_version_line")
    op.execute(f"DROP TRIGGER IF EXISTS plan_version_lock_guard ON {SCHEMA}.plan_version")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.reject_locked_plan_write()")
    op.drop_index("plan_line_lookup_idx", table_name="plan_version_line", schema=SCHEMA)
    op.drop_table("plan_version_line", schema=SCHEMA)
    op.drop_table("plan_fx_rate", schema=SCHEMA)
    op.drop_table("scenario_driver_override", schema=SCHEMA)
    op.drop_index("one_base_scenario_per_plan", table_name="scenario_set", schema=SCHEMA)
    op.drop_table("scenario_set", schema=SCHEMA)
    op.drop_table("plan_version", schema=SCHEMA)
    op.drop_table("plan_state_transition", schema=SCHEMA)

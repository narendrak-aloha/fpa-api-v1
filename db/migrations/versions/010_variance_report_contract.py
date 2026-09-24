"""variance report contract: vintage, citations, escalation, human-only close

The bridge's output has to say what it ran on and what it ran over, route a
material gap to escalation rather than closing quietly, and let an agent
advance a report but never close it. This migration gives the tables those
facts and puts the last rule in a trigger.

Revision ID: 010
Revises: 009
Create Date: 2026-09-24
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "010"
down_revision: Union[str, Sequence[str], None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "fpa_governance"


def upgrade() -> None:
    # --- who is a person -------------------------------------------------
    # The close guard below needs to know. Service and agent identities are
    # users too (they need to be, for attribution), but they are not people.
    op.add_column("app_user", sa.Column("is_human", sa.Boolean(), server_default=sa.text("true"), nullable=False), schema=SCHEMA)
    op.execute(f"UPDATE {SCHEMA}.app_user SET is_human = false WHERE user_id LIKE 'svc-%' OR user_id LIKE 'agent-%'")

    # --- the report ---------------------------------------------------------
    # Raw DDL: these names were fixed by 006 with op.f(), and passing them
    # through op.* again would wrap them in the naming convention twice.
    op.execute(f"ALTER TABLE {SCHEMA}.variance_report DROP CONSTRAINT ck_variance_report_status")
    op.execute(
        f"ALTER TABLE {SCHEMA}.variance_report ADD CONSTRAINT ck_variance_report_status "
        "CHECK (status IN ('OPEN', 'INVESTIGATING', 'ESCALATED', 'REVIEWED', 'CLOSED'))"
    )
    op.add_column("variance_report", sa.Column("dsl", sa.Text(), nullable=True), schema=SCHEMA)
    op.add_column("variance_report", sa.Column("measure", sa.Text(), nullable=True), schema=SCHEMA)
    op.add_column("variance_report", sa.Column("rollup", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False), schema=SCHEMA)
    op.add_column("variance_report", sa.Column("convention", sa.Text(), server_default=sa.text("'volume_first'"), nullable=False), schema=SCHEMA)
    op.add_column("variance_report", sa.Column("report_currency", sa.CHAR(3), server_default=sa.text("'USD'"), nullable=False), schema=SCHEMA)
    op.add_column("variance_report", sa.Column("vintage_closed_at", sa.DateTime(timezone=True), nullable=True), schema=SCHEMA)
    op.add_column("variance_report", sa.Column("line_count", sa.Integer(), server_default=sa.text("0"), nullable=False), schema=SCHEMA)
    op.add_column("variance_report", sa.Column("total_gap", sa.Numeric(20, 2), server_default=sa.text("0"), nullable=False), schema=SCHEMA)
    op.add_column("variance_report", sa.Column("materiality_threshold", sa.Numeric(20, 2), nullable=True), schema=SCHEMA)
    op.add_column("variance_report", sa.Column("ties", sa.Boolean(), server_default=sa.text("true"), nullable=False), schema=SCHEMA)
    op.add_column("variance_report", sa.Column("status_changed_by", sa.Text(), nullable=True), schema=SCHEMA)
    op.create_foreign_key("fk_variance_report_status_changed_by_app_user", "variance_report", "app_user", ["status_changed_by"], ["user_id"], source_schema=SCHEMA, referent_schema=SCHEMA)

    # --- the lines: one per rollup node -------------------------------------
    op.add_column("variance_report_line", sa.Column("level", sa.Integer(), server_default=sa.text("0"), nullable=False), schema=SCHEMA)
    op.add_column("variance_report_line", sa.Column("path", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False), schema=SCHEMA)
    op.add_column("variance_report_line", sa.Column("line_count", sa.Integer(), server_default=sa.text("0"), nullable=False), schema=SCHEMA)
    op.add_column("variance_report_line", sa.Column("tolerance", sa.Numeric(20, 2), server_default=sa.text("1"), nullable=False), schema=SCHEMA)
    # The part of the mix leg that happened *at this node*: the blend shift
    # among its children. The rest of mix_variance is inside the children.
    op.add_column("variance_report_line", sa.Column("mix_between_variance", sa.Numeric(20, 2), server_default=sa.text("0"), nullable=False), schema=SCHEMA)
    op.execute(f"ALTER TABLE {SCHEMA}.variance_report_line ADD CONSTRAINT ck_variance_report_line_ties CHECK (abs(residual) < tolerance)")

    # --- what each leaf cites -------------------------------------------------
    # "Poland missed by 2.2M" is not an answer; these rows are. A node's
    # citations are its leaves' citations, found by path prefix.
    op.create_table(
        "variance_report_citation",
        sa.Column("variance_report_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_no", sa.Integer(), nullable=False),
        sa.Column("company_code", sa.Text(), nullable=False),
        sa.Column("period_month", sa.Date(), nullable=False),
        sa.Column("account_code", sa.Text(), nullable=False),
        sa.Column("dim_signature_hash", sa.CHAR(16), nullable=False),
        sa.Column("plan_amount", sa.Numeric(20, 2), nullable=False),
        sa.Column("actual_amount", sa.Numeric(20, 2), nullable=False),
        sa.ForeignKeyConstraint(
            ["variance_report_id", "line_no"],
            [f"{SCHEMA}.variance_report_line.variance_report_id", f"{SCHEMA}.variance_report_line.line_no"],
            name="fk_variance_report_citation_line", ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("variance_report_id", "line_no", "company_code", "period_month", "account_code", "dim_signature_hash", name="pk_variance_report_citation"),
        schema=SCHEMA,
    )

    # --- only a human closes ------------------------------------------------
    # The actor is whoever the API set as fpa.actor for this transaction. No
    # actor, or a non-human one, cannot close; nobody can reopen a closed
    # report; and an ESCALATED report cannot be quietly downgraded to OPEN.
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.guard_variance_report_status() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE actor text := current_setting('fpa.actor', true);
                human boolean;
        BEGIN
            IF NEW.status = OLD.status THEN
                RETURN NEW;
            END IF;
            IF OLD.status = 'CLOSED' THEN
                RAISE EXCEPTION 'variance report is closed and cannot be reopened';
            END IF;
            IF OLD.status = 'ESCALATED' AND NEW.status IN ('OPEN', 'INVESTIGATING') THEN
                RAISE EXCEPTION 'an escalated variance report cannot be downgraded; review or close it';
            END IF;
            IF NEW.status = 'CLOSED' THEN
                IF actor IS NULL OR actor = '' THEN
                    RAISE EXCEPTION 'closing a variance report needs a named human actor (fpa.actor is not set)';
                END IF;
                SELECT is_human INTO human FROM {SCHEMA}.app_user WHERE user_id = actor;
                IF human IS DISTINCT FROM true THEN
                    RAISE EXCEPTION 'only a human closes a variance report; % is not one', actor;
                END IF;
                IF NOT EXISTS (SELECT 1 FROM {SCHEMA}.user_role WHERE user_id = actor AND role_code IN ('controller', 'cfo')) THEN
                    RAISE EXCEPTION 'closing a variance report needs the controller or cfo role; % has neither', actor;
                END IF;
                NEW.closed_by := actor;
                NEW.closed_at := now();
            END IF;
            NEW.status_changed_by := NULLIF(actor, '');
            RETURN NEW;
        END $$;
    """)
    op.execute(f"""
        CREATE TRIGGER variance_report_status_guard BEFORE UPDATE OF status ON {SCHEMA}.variance_report
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_variance_report_status();
    """)


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS variance_report_status_guard ON {SCHEMA}.variance_report")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.guard_variance_report_status()")
    op.drop_table("variance_report_citation", schema=SCHEMA)
    op.execute(f"ALTER TABLE {SCHEMA}.variance_report_line DROP CONSTRAINT ck_variance_report_line_ties")
    for column in ("mix_between_variance", "tolerance", "line_count", "path", "level"):
        op.drop_column("variance_report_line", column, schema=SCHEMA)
    op.drop_constraint("fk_variance_report_status_changed_by_app_user", "variance_report", schema=SCHEMA, type_="foreignkey")
    for column in ("status_changed_by", "ties", "materiality_threshold", "total_gap", "line_count", "vintage_closed_at",
                   "report_currency", "convention", "rollup", "measure", "dsl"):
        op.drop_column("variance_report", column, schema=SCHEMA)
    op.execute(f"ALTER TABLE {SCHEMA}.variance_report DROP CONSTRAINT ck_variance_report_status")
    op.execute(f"ALTER TABLE {SCHEMA}.variance_report ADD CONSTRAINT ck_variance_report_status CHECK (status IN ('OPEN', 'REVIEWED', 'CLOSED'))")
    op.drop_column("app_user", "is_human", schema=SCHEMA)

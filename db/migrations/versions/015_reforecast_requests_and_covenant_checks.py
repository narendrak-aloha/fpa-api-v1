"""question-driven re-forecast requests and the automated covenant check

Revision ID: 015
Revises: 014
Create Date: 2026-09-25

The write path becomes: a planner asks in words, the agent team drafts a
``reforecast_request``, a controller approves it (which starts the workflow),
the workflow recomputes and checks ``covenant_rule`` on the recomputed lines,
writing one ``covenant_check`` row per rule and scenario, and only a passing
run reaches the CFO. A breach ends the request for good.

What the database enforces here, whatever the application does:

- Only a human planner creates a request, as themselves.
- Only a human controller who is not the requester approves or rejects it.
- After that, the request's state can only mirror its run
  (``request_state_for_run``), so nothing but the workflow can say it
  covenant-failed or published. A closed request never changes again.
- Covenant rules are controller-only data; covenant checks are append-only.
- The workflow's service identity may set ``plan_version.covenant_ok`` only
  on a version whose covenant checks exist and all passed; everyone else
  still needs the controller role (011).
- A rejected re-forecast successor is closed: it never leaves REJECTED.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "015"
down_revision = "014"
branch_labels = depends_on = None

SCHEMA = "fpa_governance"
SERVICE_USER = "svc-temporal"
TERMINAL = "('CONTROLLER_REJECTED', 'COVENANT_FAILED', 'PUBLISHED', 'CFO_REJECTED', 'EXPIRED', 'CANCELLED', 'COMPENSATED', 'FAILED')"
STATES = (
    "PROPOSED", "CONTROLLER_REJECTED", "RUNNING", "COVENANT_FAILED", "AWAITING_CFO", "PUBLISHING",
    "PUBLISHED", "CFO_REJECTED", "EXPIRED", "CANCELLED", "COMPENSATED", "FAILED",
)


def upgrade() -> None:
    op.create_table(
        "reforecast_request",
        sa.Column("request_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("source_plan_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("driver_code", sa.Text(), nullable=False),
        sa.Column("from_value", sa.Numeric(20, 6), nullable=False),
        sa.Column("to_value", sa.Numeric(20, 6), nullable=False),
        sa.Column("companies", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("period_months", postgresql.ARRAY(sa.Date()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("scope_label", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), server_default=sa.text("'PROPOSED'"), nullable=False),
        sa.Column("controller_decided_by", sa.Text(), nullable=True),
        sa.Column("controller_decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("controller_comment", sa.Text(), nullable=True),
        sa.Column("run_id", sa.Text(), nullable=True),
        sa.Column("outcome_detail", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("state IN (" + ", ".join(f"'{s}'" for s in STATES) + ")", name=op.f("ck_reforecast_request_state")),
        sa.CheckConstraint("from_value > 0 AND to_value > 0", name=op.f("ck_reforecast_request_values_positive")),
        sa.CheckConstraint(
            "controller_decided_by IS NULL OR controller_decided_by <> requested_by",
            name=op.f("ck_reforecast_request_no_self_decision"),
        ),
        sa.ForeignKeyConstraint(["source_plan_version_id"], [f"{SCHEMA}.plan_version.plan_version_id"],
                                name="fk_reforecast_request_source_plan_version_id_plan_version"),
        sa.ForeignKeyConstraint(["requested_by"], [f"{SCHEMA}.app_user.user_id"], name="fk_reforecast_request_requested_by_app_user"),
        sa.ForeignKeyConstraint(["controller_decided_by"], [f"{SCHEMA}.app_user.user_id"],
                                name="fk_reforecast_request_controller_decided_by_app_user"),
        sa.PrimaryKeyConstraint("request_id", name="pk_reforecast_request"),
        sa.UniqueConstraint("run_id", name="uq_reforecast_request_run_id"),
        schema=SCHEMA,
    )
    op.create_index("reforecast_request_state_idx", "reforecast_request", ["state", "created_at"], schema=SCHEMA)

    op.create_table(
        "covenant_rule",
        sa.Column("rule_code", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column("measure", sa.Text(), nullable=False),
        sa.Column("comparator", sa.Text(), nullable=False),
        sa.Column("threshold", sa.Numeric(20, 6), nullable=False),
        sa.Column("scope", sa.Text(), server_default=sa.text("'REQUEST'"), nullable=False),
        sa.Column("scenarios", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{base,stretch,downside}'"), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("metric IN ('services_revenue', 'delivery_cost', 'gross_margin_pct')", name=op.f("ck_covenant_rule_metric")),
        sa.CheckConstraint("measure IN ('LEVEL', 'CHANGE_PCT')", name=op.f("ck_covenant_rule_measure")),
        sa.CheckConstraint("comparator IN ('>=', '<=')", name=op.f("ck_covenant_rule_comparator")),
        sa.CheckConstraint("scope IN ('REQUEST', 'PLAN')", name=op.f("ck_covenant_rule_scope")),
        sa.ForeignKeyConstraint(["created_by"], [f"{SCHEMA}.app_user.user_id"], name="fk_covenant_rule_created_by_app_user"),
        sa.PrimaryKeyConstraint("rule_code", name="pk_covenant_rule"),
        schema=SCHEMA,
    )

    op.create_table(
        "covenant_check",
        sa.Column("check_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("workflow_id", sa.Text(), nullable=False),
        sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("plan_version_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("rule_code", sa.Text(), nullable=False),
        sa.Column("scenario_code", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        sa.Column("metric", sa.Text(), nullable=False),
        sa.Column("measure", sa.Text(), nullable=False),
        sa.Column("before_value", sa.Numeric(24, 6), nullable=True),
        sa.Column("after_value", sa.Numeric(24, 6), nullable=True),
        sa.Column("measured_value", sa.Numeric(24, 6), nullable=True),
        sa.Column("comparator", sa.Text(), nullable=False),
        sa.Column("threshold", sa.Numeric(20, 6), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["request_id"], [f"{SCHEMA}.reforecast_request.request_id"],
                                name="fk_covenant_check_request_id_reforecast_request"),
        sa.ForeignKeyConstraint(["plan_version_id"], [f"{SCHEMA}.plan_version.plan_version_id"],
                                name="fk_covenant_check_plan_version_id_plan_version"),
        sa.ForeignKeyConstraint(["rule_code"], [f"{SCHEMA}.covenant_rule.rule_code"], name="fk_covenant_check_rule_code_covenant_rule"),
        sa.PrimaryKeyConstraint("check_id", name="pk_covenant_check"),
        sa.UniqueConstraint("run_id", "revision", "rule_code", "scenario_code", name="uq_covenant_check_run_id"),
        schema=SCHEMA,
    )
    op.create_index("covenant_check_version_idx", "covenant_check", ["plan_version_id"], schema=SCHEMA)

    # ------------------------------------------------------------------
    # A request's state, once its run exists, is a function of the run.
    # ------------------------------------------------------------------
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.request_state_for_run(p_run_id text) RETURNS text
        LANGUAGE sql STABLE AS $$
            SELECT CASE r.state
                WHEN 'RUNNING' THEN 'RUNNING'
                WHEN 'AWAITING_APPROVAL' THEN 'AWAITING_CFO'
                WHEN 'PUBLISHING' THEN 'PUBLISHING'
                WHEN 'COMPLETED' THEN 'PUBLISHED'
                WHEN 'REJECTED' THEN CASE WHEN EXISTS (
                        SELECT 1 FROM {SCHEMA}.covenant_check c WHERE c.run_id = r.run_id AND NOT c.passed)
                    THEN 'COVENANT_FAILED' ELSE 'CFO_REJECTED' END
                ELSE r.state
            END
            FROM {SCHEMA}.recompute_run r WHERE r.run_id = p_run_id
        $$;
    """)
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.guard_reforecast_request() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE actor text := current_setting('fpa.actor', true);
                mirrored text;
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 're-forecast requests are retained';
            END IF;
            IF TG_OP = 'INSERT' THEN
                IF NEW.state <> 'PROPOSED' OR NEW.controller_decided_by IS NOT NULL OR NEW.run_id IS NOT NULL THEN
                    RAISE EXCEPTION 'a re-forecast request starts PROPOSED, undecided and without a run';
                END IF;
                IF actor IS DISTINCT FROM NEW.requested_by OR NOT EXISTS (
                    SELECT 1 FROM {SCHEMA}.app_user u JOIN {SCHEMA}.user_role r USING (user_id)
                    WHERE u.user_id = actor AND u.active AND u.is_human AND r.role_code = 'planner')
                THEN
                    RAISE EXCEPTION 'only a human planner, as themselves, may request a re-forecast';
                END IF;
                RETURN NEW;
            END IF;

            IF (NEW.source_plan_version_id, NEW.driver_code, NEW.from_value, NEW.to_value, NEW.companies,
                NEW.period_months, NEW.question, NEW.evidence, NEW.requested_by, NEW.created_at)
               IS DISTINCT FROM
               (OLD.source_plan_version_id, OLD.driver_code, OLD.from_value, OLD.to_value, OLD.companies,
                OLD.period_months, OLD.question, OLD.evidence, OLD.requested_by, OLD.created_at)
            THEN
                RAISE EXCEPTION 'the request itself is immutable; ask again for a different re-forecast';
            END IF;
            IF OLD.state IN {TERMINAL} THEN
                RAISE EXCEPTION 're-forecast request % is % and closed', OLD.request_id, OLD.state;
            END IF;
            IF OLD.controller_decided_by IS NOT NULL AND
               (NEW.controller_decided_by, NEW.controller_decided_at, NEW.controller_comment)
               IS DISTINCT FROM (OLD.controller_decided_by, OLD.controller_decided_at, OLD.controller_comment)
            THEN
                RAISE EXCEPTION 'the controller decision is final';
            END IF;
            IF OLD.run_id IS NOT NULL AND NEW.run_id IS DISTINCT FROM OLD.run_id THEN
                RAISE EXCEPTION 'a request is bound to the one run it started';
            END IF;

            IF OLD.state = 'PROPOSED' AND NEW.state <> 'PROPOSED' THEN
                IF NEW.state NOT IN ('RUNNING', 'CONTROLLER_REJECTED') THEN
                    RAISE EXCEPTION 'a proposed request can only be approved (RUNNING) or rejected by a controller';
                END IF;
                IF actor IS NULL OR actor = '' OR NEW.controller_decided_by IS DISTINCT FROM actor OR NOT EXISTS (
                    SELECT 1 FROM {SCHEMA}.app_user u JOIN {SCHEMA}.user_role r USING (user_id)
                    WHERE u.user_id = actor AND u.active AND u.is_human AND r.role_code = 'controller')
                THEN
                    RAISE EXCEPTION 'only a human controller, as themselves, decides a re-forecast request';
                END IF;
                IF actor = OLD.requested_by THEN
                    RAISE EXCEPTION 'segregation of duties: % requested this re-forecast', actor;
                END IF;
                NEW.controller_decided_at := now();
            ELSIF OLD.state <> 'PROPOSED' AND NEW.state IS DISTINCT FROM OLD.state THEN
                mirrored := {SCHEMA}.request_state_for_run(NEW.run_id);
                IF mirrored IS DISTINCT FROM NEW.state THEN
                    RAISE EXCEPTION 'request state follows its run: the run says %, not %', coalesce(mirrored, 'nothing'), NEW.state;
                END IF;
            END IF;
            NEW.updated_at := now();
            RETURN NEW;
        END $$;
    """)
    op.execute(f"""
        CREATE TRIGGER reforecast_request_guard BEFORE INSERT OR UPDATE OR DELETE ON {SCHEMA}.reforecast_request
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_reforecast_request();
    """)
    # The run drives the request from here on: every change to the run's state
    # is copied onto the request it belongs to, through the guard above.
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.sync_request_from_run() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE mirrored text := {SCHEMA}.request_state_for_run(NEW.run_id);
        BEGIN
            UPDATE {SCHEMA}.reforecast_request
               SET state = mirrored, outcome_detail = coalesce(NEW.detail, outcome_detail)
             WHERE run_id = NEW.run_id AND state <> 'PROPOSED' AND state NOT IN {TERMINAL}
               AND state IS DISTINCT FROM mirrored;
            RETURN NEW;
        END $$;
    """)
    op.execute(f"""
        CREATE TRIGGER recompute_run_sync_request AFTER INSERT OR UPDATE ON {SCHEMA}.recompute_run
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.sync_request_from_run();
    """)

    # ------------------------------------------------------------------
    # Covenant rules are controller data; checks are an append-only record.
    # ------------------------------------------------------------------
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.guard_covenant_rule() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            PERFORM {SCHEMA}.require_controller('covenant_rule');
            RETURN COALESCE(NEW, OLD);
        END $$;
    """)
    op.execute(f"""
        CREATE TRIGGER covenant_rule_guard BEFORE INSERT OR UPDATE OR DELETE ON {SCHEMA}.covenant_rule
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_covenant_rule();
    """)
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.reject_covenant_check_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'covenant_check is append-only';
        END $$;
    """)
    op.execute(f"""
        CREATE TRIGGER covenant_check_no_update BEFORE UPDATE OR DELETE ON {SCHEMA}.covenant_check
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_covenant_check_change();
    """)

    # ------------------------------------------------------------------
    # covenant_ok: the controller, or the workflow on a version whose
    # automated checks all passed. Otherwise identical to 011.
    # ------------------------------------------------------------------
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.guard_controller_fields() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_TABLE_NAME = 'plan_version' THEN
                IF TG_OP = 'INSERT' THEN
                    IF NEW.covenant_ok THEN PERFORM {SCHEMA}.require_controller('plan_version.covenant_ok'); END IF;
                ELSIF NEW.covenant_ok IS DISTINCT FROM OLD.covenant_ok OR NEW.covenant_note IS DISTINCT FROM OLD.covenant_note THEN
                    IF current_setting('fpa.actor', true) = '{SERVICE_USER}' AND NEW.covenant_ok
                       AND EXISTS (SELECT 1 FROM {SCHEMA}.covenant_check c WHERE c.plan_version_id = NEW.plan_version_id)
                       AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.covenant_check c
                                       WHERE c.plan_version_id = NEW.plan_version_id AND NOT c.passed)
                    THEN
                        RETURN NEW;
                    END IF;
                    PERFORM {SCHEMA}.require_controller('plan_version.covenant_ok / covenant_note');
                END IF;
                RETURN NEW;
            END IF;
            PERFORM {SCHEMA}.require_controller('plan_fx_rate');
            RETURN COALESCE(NEW, OLD);
        END $$;
    """)

    # A rejected re-forecast is closed. The original plan keeps its own
    # REJECTED -> DRAFT move; a successor never leaves REJECTED.
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.reject_reopening_successor() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.state = 'REJECTED' AND OLD.supersedes_plan_version_id IS NOT NULL
               AND NEW.state IS DISTINCT FROM OLD.state THEN
                RAISE EXCEPTION 'a rejected re-forecast is closed; ask for a new one';
            END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute(f"""
        CREATE TRIGGER plan_version_rejected_successor_final BEFORE UPDATE ON {SCHEMA}.plan_version
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_reopening_successor();
    """)


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS plan_version_rejected_successor_final ON {SCHEMA}.plan_version")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.reject_reopening_successor()")
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.guard_controller_fields() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_TABLE_NAME = 'plan_version' THEN
                IF TG_OP = 'INSERT' THEN
                    IF NEW.covenant_ok THEN PERFORM {SCHEMA}.require_controller('plan_version.covenant_ok'); END IF;
                ELSIF NEW.covenant_ok IS DISTINCT FROM OLD.covenant_ok OR NEW.covenant_note IS DISTINCT FROM OLD.covenant_note THEN
                    PERFORM {SCHEMA}.require_controller('plan_version.covenant_ok / covenant_note');
                END IF;
                RETURN NEW;
            END IF;
            PERFORM {SCHEMA}.require_controller('plan_fx_rate');
            RETURN COALESCE(NEW, OLD);
        END $$;
    """)
    op.execute(f"DROP TRIGGER IF EXISTS recompute_run_sync_request ON {SCHEMA}.recompute_run")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.sync_request_from_run()")
    op.execute(f"DROP TABLE {SCHEMA}.covenant_check")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.reject_covenant_check_change()")
    op.execute(f"DROP TABLE {SCHEMA}.covenant_rule")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.guard_covenant_rule()")
    op.execute(f"DROP TABLE {SCHEMA}.reforecast_request")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.guard_reforecast_request()")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.request_state_for_run(text)")

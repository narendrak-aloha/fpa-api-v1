"""branches not copies, traces that explain, superseding a locked plan, four eyes

Revision ID: 016
Revises: 015
Create Date: 2026-09-25

Six guarantees, each in the database because each is a rule a client with a
database URL must not be able to walk around:

1. **Scenario branches are driver overrides, not copied lines.**
   ``plan_version_line`` holds the base scenario only. Stretch and downside are
   ``scenario_set`` rows with ``scenario_driver_override`` values; an attempt to
   store a line for one of them is refused. (The cube's ``fact_plan_line``
   still carries three scenarios: that is the seeded data, which the
   assignment forbids editing, and it is the published projection, not the
   governed record.)
2. **A derivation trace names its driver, its formula and its inputs.** 004
   only refused an empty object; a trace of ``{"x": 1}`` passed. Validated
   against every existing row: run on a freshly seeded store (``make
   docker-reinit``), where the only lines are the imported seed lines, which
   already carry the three keys.
3. **LOCKED -> SUPERSEDED.** The lock guard refused every update to a locked
   row, so a replaced plan stayed LOCKED forever and the state machine's last
   state was unreachable. Exactly one move is now allowed on a locked row: to
   SUPERSEDED, with no other column changing, and only when a LOCKED successor
   in the same line exists. A SUPERSEDED version is as frozen as a LOCKED one.
4. **A failed automated covenant check cannot be overridden by hand.** 015 let
   a controller set ``covenant_ok`` whatever the recorded checks said.
5. **Two gates, two people.** On a question-driven re-forecast, the controller
   who started it may not be the approver who publishes it (the CFO holds the
   controller role, so one person could otherwise do both).
6. **Citations carry quantity, price and FX**, so drill-through can show what
   each cited row contributed to the leg that was clicked.
"""
from alembic import op
import sqlalchemy as sa

revision = "016"
down_revision = "015"
branch_labels = depends_on = None

SCHEMA = "fpa_governance"
SERVICE_USER = "svc-temporal"
FROZEN = "('LOCKED', 'SUPERSEDED')"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 3. The lock guard, with its one exception
    # ------------------------------------------------------------------
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.reject_locked_plan_write() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE current_state text;
        BEGIN
            IF TG_TABLE_NAME = 'plan_version' THEN
                current_state := OLD.state;
                IF TG_OP = 'UPDATE' AND OLD.state = 'LOCKED' AND NEW.state = 'SUPERSEDED' THEN
                    -- Nothing but the state (and its timestamp) may change, and
                    -- only once a locked successor in the same line exists.
                    IF (to_jsonb(NEW) - 'state' - 'updated_at' - 'row_version')
                         IS DISTINCT FROM (to_jsonb(OLD) - 'state' - 'updated_at' - 'row_version') THEN
                        RAISE EXCEPTION 'superseding a locked plan version changes its state and nothing else';
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1 FROM {SCHEMA}.plan_version s
                        WHERE s.state = 'LOCKED' AND s.plan_version_id <> OLD.plan_version_id
                          AND (s.supersedes_plan_version_id = OLD.plan_version_id
                               OR (OLD.supersedes_plan_version_id IS NOT NULL
                                   AND s.supersedes_plan_version_id = OLD.supersedes_plan_version_id
                                   AND s.revision > OLD.revision))) THEN
                        RAISE EXCEPTION 'a locked plan version is superseded only by a locked successor, and there is none';
                    END IF;
                    RETURN NEW;
                END IF;
            ELSE
                -- The version a line leaves (UPDATE, DELETE) and the one it
                -- lands in (INSERT, UPDATE): either being frozen is a refusal.
                SELECT state INTO current_state FROM {SCHEMA}.plan_version
                WHERE state IN {FROZEN}
                  AND plan_version_id IN (
                      CASE WHEN TG_OP <> 'INSERT' THEN OLD.plan_version_id END,
                      CASE WHEN TG_OP <> 'DELETE' THEN NEW.plan_version_id END)
                LIMIT 1;
            END IF;
            IF current_state = 'LOCKED' THEN
                RAISE EXCEPTION 'plan version is locked; create a superseding version instead';
            ELSIF current_state = 'SUPERSEDED' THEN
                RAISE EXCEPTION 'plan version is superseded; it is history and cannot change';
            END IF;
            RETURN COALESCE(NEW, OLD);
        END $$;
    """)
    # The LOCKED -> SUPERSEDED role rule is data, seeded with the other
    # transitions in db/seed.yaml: migrations run before the roles exist.

    # ------------------------------------------------------------------
    # 1. Base lines only
    # ------------------------------------------------------------------
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.plan_line_base_scenario_only() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE lineage uuid; base text;
        BEGIN
            SELECT COALESCE(v.supersedes_plan_version_id, v.plan_version_id) INTO lineage
            FROM {SCHEMA}.plan_version v WHERE v.plan_version_id = NEW.plan_version_id;
            -- The version's own base scenario, else the one of the plan it
            -- re-forecasts; a plan with no scenario sets is its base alone.
            SELECT s.scenario_code INTO base FROM {SCHEMA}.scenario_set s
            WHERE s.is_base AND s.plan_version_id IN (NEW.plan_version_id, lineage)
            ORDER BY (s.plan_version_id = NEW.plan_version_id) DESC LIMIT 1;
            base := COALESCE(base, 'base');
            IF NEW.scenario_code <> base THEN
                RAISE EXCEPTION 'plan_version_line holds the base scenario (%) only; % is a branch, defined by scenario_driver_override, never by copied lines',
                    base, NEW.scenario_code;
            END IF;
            RETURN NEW;
        END $$;
    """)
    # Named to sort after plan_line_lock_guard: on a locked version the lock
    # is the reason to report.
    op.execute(f"""
        CREATE TRIGGER plan_line_scenario_guard BEFORE INSERT OR UPDATE OF scenario_code, plan_version_id
        ON {SCHEMA}.plan_version_line
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.plan_line_base_scenario_only();
    """)

    # ------------------------------------------------------------------
    # 2. A trace that explains
    # ------------------------------------------------------------------
    op.execute(f"""
        ALTER TABLE {SCHEMA}.plan_version_line ADD CONSTRAINT ck_plan_version_line_derivation_trace_explains CHECK (
            driver_derivation_trace ?& array['driver', 'formula', 'inputs']
            AND coalesce(driver_derivation_trace->>'driver', '') <> ''
            AND coalesce(driver_derivation_trace->>'formula', '') <> ''
            AND jsonb_typeof(driver_derivation_trace->'inputs') = 'object'
        )
    """)

    # ------------------------------------------------------------------
    # 4. covenant_ok: no hand-written pass over a recorded failure
    # ------------------------------------------------------------------
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.guard_controller_fields() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_TABLE_NAME = 'plan_version' THEN
                IF TG_OP = 'INSERT' THEN
                    IF NEW.covenant_ok THEN PERFORM {SCHEMA}.require_controller('plan_version.covenant_ok'); END IF;
                ELSIF NEW.covenant_ok IS DISTINCT FROM OLD.covenant_ok OR NEW.covenant_note IS DISTINCT FROM OLD.covenant_note THEN
                    IF NEW.covenant_ok AND EXISTS (SELECT 1 FROM {SCHEMA}.covenant_check c
                                                   WHERE c.plan_version_id = NEW.plan_version_id AND NOT c.passed) THEN
                        RAISE EXCEPTION 'the automated covenant check failed for this version; nobody can record a pass over it';
                    END IF;
                    IF current_setting('fpa.actor', true) = '{SERVICE_USER}' AND NEW.covenant_ok
                       AND EXISTS (SELECT 1 FROM {SCHEMA}.covenant_check c WHERE c.plan_version_id = NEW.plan_version_id)
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

    # ------------------------------------------------------------------
    # 5. Two gates, two people
    # ------------------------------------------------------------------
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.plan_approval_separate_gates() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.decision = 'APPROVED' AND OLD.decision IS DISTINCT FROM 'APPROVED' AND EXISTS (
                SELECT 1 FROM {SCHEMA}.covenant_check c
                JOIN {SCHEMA}.reforecast_request r ON r.request_id = c.request_id
                WHERE c.plan_version_id = NEW.plan_version_id AND r.controller_decided_by = NEW.decided_by) THEN
                RAISE EXCEPTION 'segregation of duties: % started this re-forecast as controller and cannot also approve it',
                    NEW.decided_by;
            END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute(f"""
        CREATE TRIGGER plan_approval_separate_gates BEFORE UPDATE ON {SCHEMA}.plan_approval
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.plan_approval_separate_gates();
    """)

    # ------------------------------------------------------------------
    # 6. What a citation needs to show its contribution
    # ------------------------------------------------------------------
    for column, kind in (
        ("plan_quantity", sa.Numeric(20, 6)), ("actual_quantity", sa.Numeric(20, 6)),
        ("plan_unit_price", sa.Numeric(20, 6)), ("actual_unit_price", sa.Numeric(20, 6)),
        ("plan_fx", sa.Numeric(20, 8)), ("actual_fx", sa.Numeric(20, 8)), ("account_type", sa.Text()),
    ):
        op.add_column("variance_report_citation", sa.Column(column, kind, nullable=True), schema=SCHEMA)


def downgrade() -> None:
    for column in ("account_type", "actual_fx", "plan_fx", "actual_unit_price", "plan_unit_price",
                   "actual_quantity", "plan_quantity"):
        op.drop_column("variance_report_citation", column, schema=SCHEMA)
    op.execute(f"DROP TRIGGER IF EXISTS plan_approval_separate_gates ON {SCHEMA}.plan_approval")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.plan_approval_separate_gates()")
    op.execute(f"ALTER TABLE {SCHEMA}.plan_version_line DROP CONSTRAINT IF EXISTS ck_plan_version_line_derivation_trace_explains")
    op.execute(f"DROP TRIGGER IF EXISTS plan_line_scenario_guard ON {SCHEMA}.plan_version_line")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.plan_line_base_scenario_only()")
    op.execute(f"DELETE FROM {SCHEMA}.plan_state_transition WHERE from_state = 'LOCKED' AND to_state = 'SUPERSEDED'")
    # Back to the 015 body of guard_controller_fields and the 014 body of the lock guard.
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
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.reject_locked_plan_write() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE current_state text;
        BEGIN
            IF TG_TABLE_NAME = 'plan_version' THEN
                current_state := OLD.state;
            ELSE
                SELECT state INTO current_state FROM {SCHEMA}.plan_version
                WHERE state = 'LOCKED'
                  AND plan_version_id IN (
                      CASE WHEN TG_OP <> 'INSERT' THEN OLD.plan_version_id END,
                      CASE WHEN TG_OP <> 'DELETE' THEN NEW.plan_version_id END)
                LIMIT 1;
            END IF;
            IF current_state = 'LOCKED' THEN
                RAISE EXCEPTION 'plan version is locked; create a superseding version instead';
            END IF;
            RETURN COALESCE(NEW, OLD);
        END $$;
    """)

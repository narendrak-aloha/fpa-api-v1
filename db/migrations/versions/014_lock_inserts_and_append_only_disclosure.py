"""close the two write paths the database still allowed

Revision ID: 014
Revises: 013
Create Date: 2026-09-22

1. ``plan_line_lock_guard`` fired on UPDATE OR DELETE only, so an INSERT of a
   new line into a LOCKED version went through from psql. The guard now fires
   on INSERT as well, and checks the version a row is moving *into* as well as
   the one it is leaving, so a line cannot be re-parented into a locked plan
   either.
2. ``llm_disclosure_log`` was append-only by convention (and by a REVOKE in
   ``db/runtime_roles.sql`` that the stack does not apply, because it connects
   as ``postgres``). It now has the same kind of trigger as ``audit_event``:
   a disclosure row, once written, can be neither changed nor removed.
"""
from alembic import op

revision = "014"
down_revision = "013"
branch_labels = depends_on = None

SCHEMA = "fpa_governance"


def upgrade() -> None:
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.reject_locked_plan_write() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE current_state text;
        BEGIN
            IF TG_TABLE_NAME = 'plan_version' THEN
                current_state := OLD.state;
            ELSE
                -- The version a line leaves (UPDATE, DELETE) and the one it
                -- lands in (INSERT, UPDATE): either being LOCKED is a refusal.
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
    op.execute(f"DROP TRIGGER IF EXISTS plan_line_lock_guard ON {SCHEMA}.plan_version_line")
    op.execute(f"""
        CREATE TRIGGER plan_line_lock_guard BEFORE INSERT OR UPDATE OR DELETE ON {SCHEMA}.plan_version_line
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_locked_plan_write();
    """)

    op.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.reject_disclosure_change() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'llm_disclosure_log is append-only';
        END $$;
    """)
    op.execute(f"""
        CREATE TRIGGER llm_disclosure_log_no_update BEFORE UPDATE OR DELETE ON {SCHEMA}.llm_disclosure_log
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_disclosure_change();
    """)
    op.execute(f"""
        CREATE TRIGGER llm_disclosure_log_no_truncate BEFORE TRUNCATE ON {SCHEMA}.llm_disclosure_log
        FOR EACH STATEMENT EXECUTE FUNCTION {SCHEMA}.reject_disclosure_change();
    """)


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS llm_disclosure_log_no_truncate ON {SCHEMA}.llm_disclosure_log")
    op.execute(f"DROP TRIGGER IF EXISTS llm_disclosure_log_no_update ON {SCHEMA}.llm_disclosure_log")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.reject_disclosure_change()")
    op.execute(f"DROP TRIGGER IF EXISTS plan_line_lock_guard ON {SCHEMA}.plan_version_line")
    op.execute(f"""
        CREATE TRIGGER plan_line_lock_guard BEFORE UPDATE OR DELETE ON {SCHEMA}.plan_version_line
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.reject_locked_plan_write();
    """)
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

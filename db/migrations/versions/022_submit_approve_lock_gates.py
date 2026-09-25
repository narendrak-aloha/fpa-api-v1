"""three human gates: planner submits, controller approves, CFO locks; no back to draft

Revision ID: 022
Revises: 021
Create Date: 2026-09-25

The re-forecast run used to park once, for the CFO, whose "approve and
publish" took the successor APPROVED and LOCKED in one step, and a controller's
"approve and start" launched the recompute. Now:

* the planner who asked confirms the agent's draft, which starts the recompute
  (PROPOSED -> RUNNING), or withdraws it (PROPOSED -> CANCELLED). No controller
  decides a proposed request any more; the controller's decision is the plan's.
* the run parks three times: AWAITING_SUBMISSION (the planner reviews the
  recomputed draft and submits it, DRAFT -> IN_REVIEW), AWAITING_APPROVAL (a
  controller approves, IN_REVIEW -> APPROVED) and AWAITING_LOCK (the CFO locks,
  APPROVED -> LOCKED). Publishing follows the lock.
* the request mirrors those as AWAITING_SUBMISSION, AWAITING_CONTROLLER and
  AWAITING_CFO. A rejection reads as CONTROLLER_REJECTED before approval and
  CFO_REJECTED after it.
* plan_state_transition: REJECTED -> DRAFT ("back to draft") is gone, so a
  rejected version stays rejected and a new change is a new version. APPROVED
  -> REJECTED (cfo) is new: the CFO may decline to lock, and a run that expires
  or is cancelled at the lock gate closes its approved successor.
"""
from alembic import op

revision = "022"
down_revision = "021"
branch_labels = depends_on = None

SCHEMA = "fpa_governance"
TERMINAL = "('CONTROLLER_REJECTED', 'COVENANT_FAILED', 'PUBLISHED', 'CFO_REJECTED', 'EXPIRED', 'CANCELLED', 'COMPENSATED', 'FAILED')"
RUN_STATES_015 = ("RUNNING", "AWAITING_APPROVAL", "PUBLISHING", "COMPLETED", "REJECTED", "EXPIRED", "CANCELLED",
                  "COMPENSATED", "FAILED")
RUN_STATES = (*RUN_STATES_015, "AWAITING_SUBMISSION", "AWAITING_LOCK")
REQUEST_STATES_015 = ("PROPOSED", "CONTROLLER_REJECTED", "RUNNING", "COVENANT_FAILED", "AWAITING_CFO", "PUBLISHING",
                      "PUBLISHED", "CFO_REJECTED", "EXPIRED", "CANCELLED", "COMPENSATED", "FAILED")
REQUEST_STATES = (*REQUEST_STATES_015, "AWAITING_SUBMISSION", "AWAITING_CONTROLLER")


# 008 named its check through the naming convention, which prefixed it a
# second time; 015 used op.f and kept the plain name. These are the real ones.
RUN_CHECK = "ck_recompute_run_ck_recompute_run_state"
REQUEST_CHECK = "ck_reforecast_request_state"


def _check(table: str, name: str, states: tuple[str, ...]) -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.{table} DROP CONSTRAINT IF EXISTS {name}")
    op.execute(f"ALTER TABLE {SCHEMA}.{table} ADD CONSTRAINT {name} CHECK (state IN ("
               + ", ".join(f"'{s}'" for s in states) + "))")


def _request_state_for_run(three_gates: bool) -> str:
    if three_gates:
        awaiting = """
                WHEN 'AWAITING_SUBMISSION' THEN 'AWAITING_SUBMISSION'
                WHEN 'AWAITING_APPROVAL' THEN 'AWAITING_CONTROLLER'
                WHEN 'AWAITING_LOCK' THEN 'AWAITING_CFO'"""
        rejected = f"""CASE
                    WHEN EXISTS (SELECT 1 FROM {SCHEMA}.covenant_check c WHERE c.run_id = r.run_id AND NOT c.passed)
                        THEN 'COVENANT_FAILED'
                    WHEN EXISTS (SELECT 1 FROM {SCHEMA}.covenant_check c
                                 JOIN {SCHEMA}.plan_version v ON v.plan_version_id = c.plan_version_id
                                 WHERE c.run_id = r.run_id AND v.approved_by IS NOT NULL)
                        THEN 'CFO_REJECTED'
                    ELSE 'CONTROLLER_REJECTED' END"""
    else:
        awaiting = """
                WHEN 'AWAITING_APPROVAL' THEN 'AWAITING_CFO'"""
        rejected = f"""CASE WHEN EXISTS (
                        SELECT 1 FROM {SCHEMA}.covenant_check c WHERE c.run_id = r.run_id AND NOT c.passed)
                    THEN 'COVENANT_FAILED' ELSE 'CFO_REJECTED' END"""
    return f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.request_state_for_run(p_run_id text) RETURNS text
        LANGUAGE sql STABLE AS $$
            SELECT CASE r.state
                WHEN 'RUNNING' THEN 'RUNNING'{awaiting}
                WHEN 'PUBLISHING' THEN 'PUBLISHING'
                WHEN 'COMPLETED' THEN 'PUBLISHED'
                WHEN 'REJECTED' THEN {rejected}
                ELSE r.state
            END
            FROM {SCHEMA}.recompute_run r WHERE r.run_id = p_run_id
        $$;
    """


def _guard_reforecast_request(planner_confirms: bool) -> str:
    if planner_confirms:
        proposed = f"""
                IF NEW.state NOT IN ('RUNNING', 'CANCELLED') THEN
                    RAISE EXCEPTION 'a proposed request is confirmed (RUNNING) or withdrawn (CANCELLED) by the planner who asked';
                END IF;
                IF actor IS DISTINCT FROM OLD.requested_by OR NOT EXISTS (
                    SELECT 1 FROM {SCHEMA}.app_user u JOIN {SCHEMA}.user_role r USING (user_id)
                    WHERE u.user_id = actor AND u.active AND u.is_human AND r.role_code = 'planner')
                THEN
                    RAISE EXCEPTION 'only the planner who asked, as themselves, confirms or withdraws a re-forecast request';
                END IF;
                IF NEW.controller_decided_by IS NOT NULL THEN
                    RAISE EXCEPTION 'no controller decides a proposed request; a controller approves the recomputed plan';
                END IF;"""
    else:
        proposed = f"""
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
                NEW.controller_decided_at := now();"""
    return f"""
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

            IF OLD.state = 'PROPOSED' AND NEW.state <> 'PROPOSED' THEN{proposed}
            ELSIF OLD.state <> 'PROPOSED' AND NEW.state IS DISTINCT FROM OLD.state THEN
                mirrored := {SCHEMA}.request_state_for_run(NEW.run_id);
                IF mirrored IS DISTINCT FROM NEW.state THEN
                    RAISE EXCEPTION 'request state follows its run: the run says %, not %', coalesce(mirrored, 'nothing'), NEW.state;
                END IF;
            END IF;
            NEW.updated_at := now();
            RETURN NEW;
        END $$;
    """


def upgrade() -> None:
    # db/seed.yaml seeds role rows, but only after migrations run (see
    # docker/entrypoint.sh), so a fresh database has no 'cfo' row yet.
    op.execute(
        f"INSERT INTO {SCHEMA}.role (role_code, description) VALUES ('cfo', 'Approve and lock plans') "
        "ON CONFLICT DO NOTHING"
    )
    op.execute(f"DELETE FROM {SCHEMA}.plan_state_transition WHERE from_state = 'REJECTED' AND to_state = 'DRAFT'")
    op.execute(
        f"INSERT INTO {SCHEMA}.plan_state_transition (from_state, to_state, role_code) "
        "VALUES ('APPROVED', 'REJECTED', 'cfo') ON CONFLICT DO NOTHING"
    )
    _check("recompute_run", RUN_CHECK, RUN_STATES)
    _check("reforecast_request", REQUEST_CHECK, REQUEST_STATES)
    op.execute(_request_state_for_run(three_gates=True))
    op.execute(_guard_reforecast_request(planner_confirms=True))


def downgrade() -> None:
    op.execute(_guard_reforecast_request(planner_confirms=False))
    op.execute(_request_state_for_run(three_gates=False))
    _check("reforecast_request", REQUEST_CHECK, REQUEST_STATES_015)
    _check("recompute_run", RUN_CHECK, RUN_STATES_015)
    op.execute(f"DELETE FROM {SCHEMA}.plan_state_transition WHERE from_state = 'APPROVED' AND to_state = 'REJECTED'")
    op.execute(
        f"INSERT INTO {SCHEMA}.role (role_code, description) VALUES ('planner', 'Create and revise draft plans') "
        "ON CONFLICT DO NOTHING"
    )
    op.execute(
        f"INSERT INTO {SCHEMA}.plan_state_transition (from_state, to_state, role_code) "
        "VALUES ('REJECTED', 'DRAFT', 'planner') ON CONFLICT DO NOTHING"
    )

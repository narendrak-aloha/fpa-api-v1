"""Persist Agno pauses and second-human decisions.

Revision ID: 013
Revises: 012
Create Date: 2026-09-28
"""
from alembic import op

revision = "013"
down_revision = "012"
branch_labels = depends_on = None


def upgrade():
    op.execute("""
    CREATE TABLE fpa_governance.agent_proposal (
      proposal_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
      run_id text NOT NULL UNIQUE,
      requested_by text NOT NULL REFERENCES fpa_governance.app_user(user_id),
      decided_by text REFERENCES fpa_governance.app_user(user_id),
      state text NOT NULL DEFAULT 'PENDING' CHECK (state IN ('PENDING','APPROVED','REJECTED')),
      provider text NOT NULL,
      scope jsonb NOT NULL,
      drafts jsonb NOT NULL,
      paused_run jsonb NOT NULL,
      resumed_run jsonb,
      created_at timestamptz NOT NULL DEFAULT now(),
      decided_at timestamptz,
      CHECK (decided_by IS NULL OR decided_by <> requested_by),
      CHECK ((state = 'PENDING') = (decided_by IS NULL))
    );
    CREATE FUNCTION fpa_governance.guard_agent_proposal() RETURNS trigger LANGUAGE plpgsql AS $$
    DECLARE actor text := current_setting('fpa.actor', true);
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'agent proposals are retained'; END IF;
      IF TG_OP = 'UPDATE' THEN
        IF (NEW.requested_by, NEW.run_id, NEW.scope, NEW.drafts, NEW.paused_run, NEW.provider)
           IS DISTINCT FROM (OLD.requested_by, OLD.run_id, OLD.scope, OLD.drafts, OLD.paused_run, OLD.provider)
        THEN RAISE EXCEPTION 'proposal intent is immutable'; END IF;
        IF OLD.state <> 'PENDING' AND (NEW.state, NEW.decided_by, NEW.decided_at)
           IS DISTINCT FROM (OLD.state, OLD.decided_by, OLD.decided_at)
        THEN RAISE EXCEPTION 'proposal decision is immutable'; END IF;
        IF NEW.state <> OLD.state THEN
          IF NEW.decided_by IS DISTINCT FROM actor OR NOT EXISTS (
            SELECT 1 FROM fpa_governance.app_user u JOIN fpa_governance.user_role r USING(user_id)
            WHERE u.user_id = actor AND u.active AND u.is_human AND r.role_code IN ('controller','cfo')
          ) THEN RAISE EXCEPTION 'proposal decisions require a human controller'; END IF;
        END IF;
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER agent_proposal_guard BEFORE UPDATE OR DELETE ON fpa_governance.agent_proposal
      FOR EACH ROW EXECUTE FUNCTION fpa_governance.guard_agent_proposal();
    """)


def downgrade():
    op.execute("DROP TABLE fpa_governance.agent_proposal; DROP FUNCTION fpa_governance.guard_agent_proposal()")

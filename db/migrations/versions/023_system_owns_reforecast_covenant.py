"""a re-forecast's covenant verdict is the system's; no role records it by hand

Revision ID: 023
Revises: 022
Create Date: 2026-09-25

The run checks a re-forecast's covenants when the planner submits it: a
breach rejects the submitted draft before any controller sees it, and a pass
sets covenant_ok as the service identity. Until now a controller could also
write covenant_ok / covenant_note on a successor by hand (the manual re-forecast
path relied on it). From here, on a version that supersedes another, those two
fields change only through the service's passing verdict; any person, whatever
their role, is refused. An original plan has no recompute for the system to
measure, so its covenant review stays the controller's (unchanged from 020).
"""
from alembic import op

revision = "023"
down_revision = "022"
branch_labels = depends_on = None

SCHEMA = "fpa_governance"
SERVICE_CHECK = (f"EXISTS (SELECT 1 FROM {SCHEMA}.user_role "
                 "WHERE user_id = current_setting('fpa.actor', true) AND role_code = 'service')")


def _guard(system_owns_reforecast_covenant: bool) -> str:
    reforecast = f"""
                    IF NEW.supersedes_plan_version_id IS NOT NULL THEN
                        RAISE EXCEPTION 'a re-forecast''s covenant is checked by the system when the planner submits it; nobody records it by hand';
                    END IF;""" if system_owns_reforecast_covenant else ""
    return f"""
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
                    IF {SERVICE_CHECK} AND NEW.covenant_ok
                       AND EXISTS (SELECT 1 FROM {SCHEMA}.covenant_check c WHERE c.plan_version_id = NEW.plan_version_id)
                    THEN
                        RETURN NEW;
                    END IF;{reforecast}
                    PERFORM {SCHEMA}.require_controller('plan_version.covenant_ok / covenant_note');
                END IF;
                RETURN NEW;
            END IF;
            PERFORM {SCHEMA}.require_controller('plan_fx_rate');
            RETURN COALESCE(NEW, OLD);
        END $$;
    """


def upgrade() -> None:
    op.execute(_guard(system_owns_reforecast_covenant=True))


def downgrade() -> None:
    op.execute(_guard(system_owns_reforecast_covenant=False))

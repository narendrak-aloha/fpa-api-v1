"""user ids are UUIDs, and the workflow is recognised by its role

Revision ID: 020
Revises: 019
Create Date: 2026-09-22

* ``app_user.user_id`` must be a UUID as 32 lower-case hex characters with no
  dashes (``uuid4().hex``). Signups get a random one; the seeded
  identities have fixed ones (``fpa_project.identities``). The column stays
  ``text``, as do the columns that reference it: the references are foreign
  keys, so the check on this one column covers them all, and the type change
  would have rewritten nineteen columns across twelve tables for no guarantee
  this does not already give.
* ``guard_controller_fields`` let the workflow set ``covenant_ok`` by comparing
  the actor to the literal ``'svc-temporal'``. It now asks whether the actor
  holds the ``service`` role, which only a non-human identity may hold (017),
  so no id is written into a trigger again.

Run on a freshly initialised store: ids already in the table must be UUIDs
for the check to be added.
"""
from alembic import op

revision = "020"
down_revision = "019"
branch_labels = depends_on = None

SCHEMA = "fpa_governance"
UUID_PATTERN = "^[0-9a-f]{32}$"


def _guard(service_check: str) -> str:
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
                    IF {service_check} AND NEW.covenant_ok
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
    """


def upgrade() -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.app_user ADD CONSTRAINT ck_app_user_user_id_uuid CHECK (user_id ~ '{UUID_PATTERN}')")
    op.execute(_guard(
        f"EXISTS (SELECT 1 FROM {SCHEMA}.user_role WHERE user_id = current_setting('fpa.actor', true) AND role_code = 'service')"
    ))


def downgrade() -> None:
    op.execute(_guard("current_setting('fpa.actor', true) = 'svc-temporal'"))
    op.execute(f"ALTER TABLE {SCHEMA}.app_user DROP CONSTRAINT IF EXISTS ck_app_user_user_id_uuid")

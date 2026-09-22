"""a display name is changed by its holder or a superadmin

Revision ID: 018
Revises: 017
Create Date: 2026-09-22

017 guarded an account's status, identity and token, but not its name: any
connection could rename anyone. A name grants nothing, but it is what the
audit trail and every approval screen show, so renaming someone else is a
superadmin's action (and the account holder may rename themselves). An empty
name is refused. Otherwise ``guard_app_user`` is exactly 017's.
"""
from alembic import op

revision = "018"
down_revision = "017"
branch_labels = depends_on = None

SCHEMA = "fpa_governance"


def _guard(name_rule: str) -> str:
    return f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.guard_app_user() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE actor text := current_setting('fpa.actor', true);
                admin boolean := {SCHEMA}.is_superadmin(actor);
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NOT admin AND {SCHEMA}.superadmin_exists()
                   AND (NEW.status <> 'PENDING' OR NOT NEW.is_human OR NEW.api_token_hash IS NOT NULL) THEN
                    RAISE EXCEPTION 'a new account starts PENDING, as a person, with no standing token; a superadmin approves it';
                END IF;
            ELSE
                IF NEW.user_id <> OLD.user_id THEN
                    RAISE EXCEPTION 'a user id never changes';
                END IF;
                IF NEW.status IS DISTINCT FROM OLD.status OR NEW.is_human IS DISTINCT FROM OLD.is_human
                   OR NEW.api_token_hash IS DISTINCT FROM OLD.api_token_hash OR lower(NEW.email) IS DISTINCT FROM lower(OLD.email) THEN
                    IF NOT admin THEN
                        RAISE EXCEPTION 'only a superadmin changes an account''s status, identity or standing token';
                    END IF;
                    IF actor = OLD.user_id THEN
                        RAISE EXCEPTION 'a superadmin cannot change their own account';
                    END IF;
                END IF;
                IF NEW.status IS DISTINCT FROM OLD.status AND NOT (
                    (OLD.status = 'PENDING' AND NEW.status IN ('ACTIVE', 'REJECTED'))
                    OR (OLD.status = 'ACTIVE' AND NEW.status = 'DISABLED')
                    OR (OLD.status IN ('DISABLED', 'REJECTED') AND NEW.status = 'ACTIVE')) THEN
                    RAISE EXCEPTION 'an account cannot move from % to %', OLD.status, NEW.status;
                END IF;
                IF NEW.password_hash IS DISTINCT FROM OLD.password_hash AND NOT admin AND actor IS DISTINCT FROM OLD.user_id THEN
                    RAISE EXCEPTION 'only the account holder or a superadmin sets a password';
                END IF;
                {name_rule}
                IF NEW.status = 'DISABLED' AND {SCHEMA}.is_superadmin(OLD.user_id)
                   AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.user_role r JOIN {SCHEMA}.app_user u USING (user_id)
                                   WHERE r.role_code = 'superadmin' AND u.status = 'ACTIVE' AND u.user_id <> OLD.user_id) THEN
                    RAISE EXCEPTION 'the last active superadmin cannot be disabled';
                END IF;
            END IF;
            NEW.active := NEW.status = 'ACTIVE';
            RETURN NEW;
        END $$;
    """


NAME_RULE = """IF NEW.display_name IS DISTINCT FROM OLD.display_name THEN
                    IF NOT admin AND actor IS DISTINCT FROM OLD.user_id THEN
                        RAISE EXCEPTION 'only the account holder or a superadmin changes a name';
                    END IF;
                    IF btrim(coalesce(NEW.display_name, '')) = '' THEN
                        RAISE EXCEPTION 'a name cannot be empty';
                    END IF;
                END IF;"""


def upgrade() -> None:
    op.execute(_guard(NAME_RULE))


def downgrade() -> None:
    op.execute(_guard(""))

"""move standing api tokens from app_user into user_session

Revision ID: 021
Revises: 020
Create Date: 2026-09-22

Standing tokens (the seeded dev tokens) were stored as sha256 hashes on
app_user.api_token_hash. They now live in user_session with expires_at NULL
(never expire), so authenticate() is a single-table lookup.

* user_session.expires_at becomes nullable; NULL means a standing token.
* Each app_user.api_token_hash is copied into user_session, then the column goes.
* guard_app_user (019) no longer names the dropped column. Its standing-token
  rules move to user_session: a never-expiring token is minted only while no
  superadmin exists (the seed's bootstrap) or by a superadmin for someone else,
  the same rule 017 applied to api_token_hash.
"""
from alembic import op
import sqlalchemy as sa

revision = "021"
down_revision = "020"
branch_labels = depends_on = None

SCHEMA = "fpa_governance"


def _guard_app_user(with_token_column: bool) -> str:
    """019's guard_app_user, with or without the api_token_hash column."""
    token_changed = " OR NEW.api_token_hash IS DISTINCT FROM OLD.api_token_hash" if with_token_column else ""
    token_on_insert = " OR NEW.api_token_hash IS NOT NULL" if with_token_column else ""
    return f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.guard_app_user() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE actor text := current_setting('fpa.actor', true);
                admin boolean := {SCHEMA}.is_superadmin(actor);
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NOT admin AND {SCHEMA}.superadmin_exists()
                   AND (NEW.status <> 'PENDING' OR NOT NEW.is_human{token_on_insert}) THEN
                    RAISE EXCEPTION 'a new account starts PENDING, as a person, with no standing token; a superadmin approves it';
                END IF;
            ELSE
                IF NEW.user_id <> OLD.user_id THEN
                    RAISE EXCEPTION 'a user id never changes';
                END IF;
                IF NEW.status IS DISTINCT FROM OLD.status OR NEW.is_human IS DISTINCT FROM OLD.is_human{token_changed} THEN
                    IF NOT admin THEN
                        RAISE EXCEPTION 'only a superadmin changes an account''s status, identity or standing token';
                    END IF;
                    IF actor = OLD.user_id THEN
                        RAISE EXCEPTION 'a superadmin cannot change their own account';
                    END IF;
                END IF;
                IF lower(NEW.email) IS DISTINCT FROM lower(OLD.email) THEN
                    IF NOT admin THEN
                        RAISE EXCEPTION 'only a superadmin changes an email';
                    END IF;
                    IF btrim(coalesce(NEW.email, '')) = '' THEN
                        RAISE EXCEPTION 'an email cannot be empty';
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
                IF NEW.display_name IS DISTINCT FROM OLD.display_name THEN
                    IF NOT admin AND actor IS DISTINCT FROM OLD.user_id THEN
                        RAISE EXCEPTION 'only the account holder or a superadmin changes a name';
                    END IF;
                    IF btrim(coalesce(NEW.display_name, '')) = '' THEN
                        RAISE EXCEPTION 'a name cannot be empty';
                    END IF;
                END IF;
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


def upgrade() -> None:
    op.alter_column("user_session", "expires_at", existing_type=sa.DateTime(timezone=True), nullable=True, schema=SCHEMA)
    op.execute(
        f"INSERT INTO {SCHEMA}.user_session (token_hash, user_id, expires_at) "
        f"SELECT api_token_hash, user_id, NULL "
        f"FROM {SCHEMA}.app_user WHERE api_token_hash IS NOT NULL "
        "ON CONFLICT DO NOTHING"
    )
    op.execute(_guard_app_user(with_token_column=False))
    op.drop_column("app_user", "api_token_hash", schema=SCHEMA)
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.guard_standing_token() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE actor text := current_setting('fpa.actor', true);
        BEGIN
            IF NEW.expires_at IS NULL AND (TG_OP = 'INSERT' OR OLD.expires_at IS NOT NULL)
               AND {SCHEMA}.superadmin_exists() THEN
                IF NOT {SCHEMA}.is_superadmin(actor) THEN
                    RAISE EXCEPTION 'only a superadmin issues a standing token';
                END IF;
                IF actor = NEW.user_id THEN
                    RAISE EXCEPTION 'a superadmin cannot issue a standing token to themselves';
                END IF;
            END IF;
            RETURN NEW;
        END $$;
    """)
    op.execute(f"""
        CREATE TRIGGER user_session_standing_token_guard BEFORE INSERT OR UPDATE ON {SCHEMA}.user_session
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_standing_token();
    """)


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS user_session_standing_token_guard ON {SCHEMA}.user_session")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.guard_standing_token()")
    op.add_column("app_user", sa.Column("api_token_hash", sa.CHAR(64), nullable=True), schema=SCHEMA)
    op.execute(_guard_app_user(with_token_column=True))
    # Restoring standing tokens is a superadmin's change; the migration makes it.
    op.execute(f"ALTER TABLE {SCHEMA}.app_user DISABLE TRIGGER USER")
    op.execute(
        f"UPDATE {SCHEMA}.app_user u "
        f"SET api_token_hash = s.token_hash "
        f"FROM {SCHEMA}.user_session s "
        f"WHERE s.user_id = u.user_id AND s.expires_at IS NULL AND s.revoked_at IS NULL"
    )
    op.execute(f"ALTER TABLE {SCHEMA}.app_user ENABLE TRIGGER USER")
    op.execute(f"ALTER TABLE {SCHEMA}.app_user ADD CONSTRAINT uq_app_user_api_token_hash UNIQUE (api_token_hash)")
    op.execute(f"DELETE FROM {SCHEMA}.user_session WHERE expires_at IS NULL")
    op.alter_column("user_session", "expires_at", existing_type=sa.DateTime(timezone=True), nullable=False, schema=SCHEMA)

"""accounts, sessions and a superadmin who manages access and nothing else

Revision ID: 017
Revises: 016
Create Date: 2026-09-22

Until now users, roles and company scope came only from db/seed.yaml. This
adds self-service signup and a superadmin who approves accounts and grants
access, with the rules in the database so that a client with a database URL
cannot grant itself anything:

* ``app_user.status``: PENDING -> ACTIVE | REJECTED, ACTIVE <-> DISABLED.
  A new account written by anyone but a superadmin starts PENDING, human,
  tokenless. Only a superadmin moves a status, never their own. ``active``
  follows the status, so every existing check that reads it keeps working.
* ``user_role`` and ``user_company_scope`` are written only by a superadmin,
  never for themselves, only for ACTIVE people. A superadmin holds no business
  role and no company scope (they manage access; they do not read or approve
  the plan). ``service`` and ``agent`` belong to non-human identities only.
  The last superadmin cannot be removed.
* ``user_session``: tokens issued at signup and sign-in, stored as sha256
  only, with an expiry and a revocation time.
* Bootstrap: the very first superadmin grant is allowed while no superadmin
  exists, which is how the seeded ``u-admin`` gets the role. Once one exists,
  every grant goes through a superadmin.

The ``superadmin`` role itself is data, seeded with the other roles
(migrations run before the seed).
"""
from alembic import op
import sqlalchemy as sa

revision = "017"
down_revision = "016"
branch_labels = depends_on = None

SCHEMA = "fpa_governance"
STATUSES = ("PENDING", "ACTIVE", "REJECTED", "DISABLED")
MACHINE_ROLES = "('service', 'agent')"


def upgrade() -> None:
    op.add_column("app_user", sa.Column("status", sa.Text(), server_default=sa.text("'ACTIVE'"), nullable=False), schema=SCHEMA)
    op.add_column("app_user", sa.Column("password_hash", sa.Text(), nullable=True), schema=SCHEMA)
    op.add_column("app_user", sa.Column("status_changed_by", sa.Text(), nullable=True), schema=SCHEMA)
    op.add_column("app_user", sa.Column("status_changed_at", sa.DateTime(timezone=True), nullable=True), schema=SCHEMA)
    op.add_column("app_user", sa.Column("status_note", sa.Text(), nullable=True), schema=SCHEMA)
    op.create_check_constraint("ck_app_user_status", "app_user",
                               "status IN (" + ", ".join(f"'{s}'" for s in STATUSES) + ")", schema=SCHEMA)
    op.create_foreign_key("fk_app_user_status_changed_by_app_user", "app_user", "app_user",
                          ["status_changed_by"], ["user_id"], source_schema=SCHEMA, referent_schema=SCHEMA)
    # Case-insensitive uniqueness: Priya@x and priya@x are one person.
    op.execute(f"CREATE UNIQUE INDEX uq_app_user_email_lower ON {SCHEMA}.app_user (lower(email))")

    op.create_table(
        "user_session",
        sa.Column("session_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("token_hash", sa.CHAR(64), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], [f"{SCHEMA}.app_user.user_id"], name="fk_user_session_user_id_app_user",
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("session_id", name="pk_user_session"),
        sa.UniqueConstraint("token_hash", name="uq_user_session_token_hash"),
        sa.CheckConstraint("expires_at > created_at", name="ck_user_session_expires_after_created"),
        schema=SCHEMA,
    )

    # VOLATILE plpgsql, not SQL functions: a row-level trigger must see the
    # rows earlier in the same statement (the seed grants the first superadmin
    # and then, as that superadmin, everyone else, in one INSERT).
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.is_superadmin(who text) RETURNS boolean LANGUAGE plpgsql VOLATILE AS $$
        BEGIN
            RETURN who IS NOT NULL AND EXISTS (
                SELECT 1 FROM {SCHEMA}.user_role WHERE user_id = who AND role_code = 'superadmin');
        END $$;
    """)
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.superadmin_exists() RETURNS boolean LANGUAGE plpgsql VOLATILE AS $$
        BEGIN
            RETURN EXISTS (SELECT 1 FROM {SCHEMA}.user_role WHERE role_code = 'superadmin');
        END $$;
    """)

    # ------------------------------------------------------------------
    # app_user: signup is PENDING; only a superadmin moves a status
    # ------------------------------------------------------------------
    op.execute(f"""
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
                IF NEW.status = 'DISABLED' AND {SCHEMA}.is_superadmin(OLD.user_id)
                   AND NOT EXISTS (SELECT 1 FROM {SCHEMA}.user_role r JOIN {SCHEMA}.app_user u USING (user_id)
                                   WHERE r.role_code = 'superadmin' AND u.status = 'ACTIVE' AND u.user_id <> OLD.user_id) THEN
                    RAISE EXCEPTION 'the last active superadmin cannot be disabled';
                END IF;
            END IF;
            NEW.active := NEW.status = 'ACTIVE';
            RETURN NEW;
        END $$;
    """)
    op.execute(f"""
        CREATE TRIGGER app_user_guard BEFORE INSERT OR UPDATE ON {SCHEMA}.app_user
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_app_user();
    """)

    # ------------------------------------------------------------------
    # Roles and company scope: granted by a superadmin, to someone else
    # ------------------------------------------------------------------
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.guard_user_access() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE actor text := current_setting('fpa.actor', true);
                target text;
                human boolean;
                account_status text;
                bootstrap boolean := false;
        BEGIN
            target := CASE WHEN TG_OP = 'DELETE' THEN OLD.user_id ELSE NEW.user_id END;
            -- BEFORE INSERT fires before ON CONFLICT: re-inserting a row that
            -- already exists (the seed, every start) changes nothing, so it is
            -- not a grant and is let through for the conflict to discard.
            IF TG_OP = 'INSERT' THEN
                IF TG_TABLE_NAME = 'user_role' THEN
                    IF EXISTS (SELECT 1 FROM {SCHEMA}.user_role WHERE user_id = NEW.user_id AND role_code = NEW.role_code) THEN
                        RETURN NEW;
                    END IF;
                ELSIF EXISTS (SELECT 1 FROM {SCHEMA}.user_company_scope
                              WHERE user_id = NEW.user_id AND company_code = NEW.company_code) THEN
                    RETURN NEW;
                END IF;
            END IF;
            -- Nested, not AND-ed: PL/pgSQL does not short-circuit, and
            -- user_company_scope has no role_code for NEW to carry.
            IF TG_TABLE_NAME = 'user_role' AND TG_OP = 'INSERT' THEN
                IF NEW.role_code = 'superadmin' AND NOT {SCHEMA}.superadmin_exists() THEN
                    bootstrap := true;   -- the first superadmin, while there is none
                END IF;
            END IF;
            IF NOT bootstrap THEN
                IF NOT {SCHEMA}.is_superadmin(actor) THEN
                    RAISE EXCEPTION 'only a superadmin grants roles and company access (acting as %)', coalesce(nullif(actor, ''), 'nobody');
                END IF;
                IF actor = target THEN
                    RAISE EXCEPTION 'a superadmin cannot change their own access';
                END IF;
            END IF;

            IF TG_OP = 'DELETE' THEN
                IF TG_TABLE_NAME = 'user_role' THEN
                    IF OLD.role_code = 'superadmin' AND NOT EXISTS (
                        SELECT 1 FROM {SCHEMA}.user_role WHERE role_code = 'superadmin' AND user_id <> OLD.user_id) THEN
                        RAISE EXCEPTION 'the last superadmin cannot be removed';
                    END IF;
                END IF;
                RETURN OLD;
            END IF;

            SELECT is_human, status INTO human, account_status FROM {SCHEMA}.app_user WHERE user_id = NEW.user_id;
            IF human AND account_status <> 'ACTIVE' THEN
                RAISE EXCEPTION 'access is granted only to an ACTIVE account; % is %', NEW.user_id, account_status;
            END IF;
            IF TG_TABLE_NAME = 'user_role' THEN
                IF NEW.role_code IN {MACHINE_ROLES} AND human THEN
                    RAISE EXCEPTION 'the % role belongs to a service or agent identity, never to a person', NEW.role_code;
                END IF;
                IF NEW.role_code NOT IN {MACHINE_ROLES} AND NOT human THEN
                    RAISE EXCEPTION 'a service or agent identity holds only its own role, not %', NEW.role_code;
                END IF;
                IF NEW.role_code = 'superadmin' AND EXISTS (
                    SELECT 1 FROM {SCHEMA}.user_role WHERE user_id = NEW.user_id AND role_code <> 'superadmin') THEN
                    RAISE EXCEPTION 'a superadmin holds no business role; remove %''s other roles first', NEW.user_id;
                END IF;
                IF NEW.role_code = 'superadmin' AND EXISTS (
                    SELECT 1 FROM {SCHEMA}.user_company_scope WHERE user_id = NEW.user_id) THEN
                    RAISE EXCEPTION 'a superadmin reads no company data; remove %''s company access first', NEW.user_id;
                END IF;
                IF NEW.role_code <> 'superadmin' AND {SCHEMA}.is_superadmin(NEW.user_id) THEN
                    RAISE EXCEPTION 'a superadmin cannot hold the business role %', NEW.role_code;
                END IF;
            ELSIF {SCHEMA}.is_superadmin(NEW.user_id) THEN
                RAISE EXCEPTION 'a superadmin manages access and reads no company data';
            END IF;
            RETURN NEW;
        END $$;
    """)
    for table in ("user_role", "user_company_scope"):
        op.execute(f"""
            CREATE TRIGGER {table}_guard BEFORE INSERT OR UPDATE OR DELETE ON {SCHEMA}.{table}
            FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_user_access();
        """)


def downgrade() -> None:
    for table in ("user_role", "user_company_scope"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_guard ON {SCHEMA}.{table}")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.guard_user_access()")
    op.execute(f"DROP TRIGGER IF EXISTS app_user_guard ON {SCHEMA}.app_user")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.guard_app_user()")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.superadmin_exists()")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.is_superadmin(text)")
    op.drop_table("user_session", schema=SCHEMA)
    op.execute(f"DROP INDEX IF EXISTS {SCHEMA}.uq_app_user_email_lower")
    op.drop_constraint("fk_app_user_status_changed_by_app_user", "app_user", schema=SCHEMA, type_="foreignkey")
    op.drop_constraint("ck_app_user_status", "app_user", schema=SCHEMA, type_="check")
    for column in ("status_note", "status_changed_at", "status_changed_by", "password_hash", "status"):
        op.drop_column("app_user", column, schema=SCHEMA)

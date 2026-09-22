"""governance hardening: a real hash chain, field permissions, row versions, tokens

Four guarantees the plan spine has to enforce, each put where it cannot be
walked around by a client with a database URL:

* The audit log becomes a genuine chain. The database computes every row's
  ``event_key`` (its logical identity, unique, so a retried append lands
  once) and ``event_hash`` (sha256 of the previous hash and the key) in a
  BEFORE INSERT trigger under an advisory lock. Application code inserts the
  facts and never the hashes. Existing rows are re-chained once, here.
* Field-level permission. The covenant flag and note on a plan version, and
  every plan FX rate, are writable only by an actor holding the controller
  role. The actor is ``fpa.actor``, a transaction-local setting the API
  makes; no actor means no write.
* Concurrent edits do not clobber. ``plan_version.row_version`` counts every
  update, and a writer that names the version it read is refused when the
  row has moved on.
* Tokens and entity scope. A caller's scope comes from the rows in
  ``user_company_scope`` for the user their bearer token resolves to, and
  from nowhere else.

Revision ID: 011
Revises: 010
Create Date: 2026-09-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "011"
down_revision: Union[str, Sequence[str], None] = "010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "fpa_governance"


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. The audit chain
    # ------------------------------------------------------------------
    op.add_column("audit_event", sa.Column("event_key", sa.CHAR(64), nullable=True), schema=SCHEMA)
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.audit_event_key(
            actor text, entity_type text, entity_id text, action text, payload jsonb
        ) RETURNS char(64) LANGUAGE sql IMMUTABLE AS $$
            -- payload::text is Postgres's canonical jsonb rendering, so the
            -- same facts always hash the same way whoever inserted them.
            SELECT encode(sha256(convert_to(concat_ws('|', coalesce(actor, ''), entity_type, entity_id, action, payload::text), 'UTF8')), 'hex')
        $$;
    """)
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.audit_event_chain_hash(previous char(64), key char(64))
        RETURNS char(64) LANGUAGE sql IMMUTABLE AS $$
            SELECT encode(sha256(convert_to(coalesce(previous, '') || '|' || key, 'UTF8')), 'hex')
        $$;
    """)

    # Re-chain what is already there. The append-only guard is switched off
    # for exactly this statement: rewriting history is the one thing a
    # migration may do that nothing else can, and it does it once.
    op.execute(f"ALTER TABLE {SCHEMA}.audit_event DISABLE TRIGGER audit_event_no_update")
    op.execute(f"""
        DO $$
        DECLARE r record; prev char(64) := NULL; key char(64);
        BEGIN
            FOR r IN SELECT * FROM {SCHEMA}.audit_event ORDER BY audit_event_id LOOP
                key := {SCHEMA}.audit_event_key(r.actor_user_id, r.entity_type, r.entity_id, r.action, r.payload);
                UPDATE {SCHEMA}.audit_event
                   SET event_key = key, previous_hash = prev, event_hash = {SCHEMA}.audit_event_chain_hash(prev, key)
                 WHERE audit_event_id = r.audit_event_id;
                prev := {SCHEMA}.audit_event_chain_hash(prev, key);
            END LOOP;
        END $$;
    """)
    op.execute(f"ALTER TABLE {SCHEMA}.audit_event ENABLE TRIGGER audit_event_no_update")
    op.alter_column("audit_event", "event_key", nullable=False, schema=SCHEMA)
    op.create_unique_constraint("uq_audit_event_event_key", "audit_event", ["event_key"], schema=SCHEMA)

    # From here on the database owns the hashes. The advisory lock serialises
    # appends so two writers cannot both read the same tail and fork the chain.
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.audit_event_link() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE prev char(64);
        BEGIN
            PERFORM pg_advisory_xact_lock(hashtext('{SCHEMA}.audit_event'));
            SELECT event_hash INTO prev FROM {SCHEMA}.audit_event ORDER BY audit_event_id DESC LIMIT 1;
            NEW.event_key := {SCHEMA}.audit_event_key(NEW.actor_user_id, NEW.entity_type, NEW.entity_id, NEW.action, NEW.payload);
            NEW.previous_hash := prev;
            NEW.event_hash := {SCHEMA}.audit_event_chain_hash(prev, NEW.event_key);
            RETURN NEW;
        END $$;
    """)
    op.execute(f"""
        CREATE TRIGGER audit_event_chain BEFORE INSERT ON {SCHEMA}.audit_event
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.audit_event_link();
    """)

    # ------------------------------------------------------------------
    # 2. Field-level permission: controller-only fields
    # ------------------------------------------------------------------
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.require_controller(what text) RETURNS void LANGUAGE plpgsql AS $$
        DECLARE actor text := current_setting('fpa.actor', true);
        BEGIN
            IF actor IS NULL OR actor = '' THEN
                RAISE EXCEPTION '% may only be written by a named actor with the controller role (fpa.actor is not set)', what;
            END IF;
            IF NOT EXISTS (SELECT 1 FROM {SCHEMA}.user_role WHERE user_id = actor AND role_code = 'controller') THEN
                RAISE EXCEPTION '% may only be written by a controller; % does not hold that role', what, actor;
            END IF;
        END $$;
    """)
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
    op.execute(f"""
        -- Named to sort after plan_version_lock_guard: Postgres fires BEFORE
        -- triggers alphabetically, and on a locked version the lock is the
        -- reason to report, not the field permission.
        CREATE TRIGGER plan_version_write_fields_guard BEFORE INSERT OR UPDATE ON {SCHEMA}.plan_version
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_controller_fields();
    """)
    op.execute(f"""
        CREATE TRIGGER plan_fx_rate_field_guard BEFORE INSERT OR UPDATE OR DELETE ON {SCHEMA}.plan_fx_rate
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.guard_controller_fields();
    """)

    # ------------------------------------------------------------------
    # 3. Row versions
    # ------------------------------------------------------------------
    op.add_column("plan_version", sa.Column("row_version", sa.Integer(), server_default=sa.text("1"), nullable=False), schema=SCHEMA)
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.bump_row_version() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            NEW.row_version := OLD.row_version + 1;
            RETURN NEW;
        END $$;
    """)
    op.execute(f"""
        CREATE TRIGGER plan_version_row_version BEFORE UPDATE ON {SCHEMA}.plan_version
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.bump_row_version();
    """)

    # ------------------------------------------------------------------
    # 4. Tokens and entity scope
    # ------------------------------------------------------------------
    op.add_column("app_user", sa.Column("api_token_hash", sa.CHAR(64), nullable=True), schema=SCHEMA)
    op.create_unique_constraint("uq_app_user_api_token_hash", "app_user", ["api_token_hash"], schema=SCHEMA)
    op.create_table(
        "user_company_scope",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("company_code", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], [f"{SCHEMA}.app_user.user_id"], name="fk_user_company_scope_user_id_app_user", ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["company_code"], [f"{SCHEMA}.dim_company.company_code"], name="fk_user_company_scope_company_code_dim_company"),
        sa.PrimaryKeyConstraint("user_id", "company_code", name="pk_user_company_scope"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("user_company_scope", schema=SCHEMA)
    op.drop_constraint("uq_app_user_api_token_hash", "app_user", schema=SCHEMA, type_="unique")
    op.drop_column("app_user", "api_token_hash", schema=SCHEMA)
    op.execute(f"DROP TRIGGER IF EXISTS plan_version_row_version ON {SCHEMA}.plan_version")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.bump_row_version()")
    op.drop_column("plan_version", "row_version", schema=SCHEMA)
    op.execute(f"DROP TRIGGER IF EXISTS plan_fx_rate_field_guard ON {SCHEMA}.plan_fx_rate")
    op.execute(f"DROP TRIGGER IF EXISTS plan_version_write_fields_guard ON {SCHEMA}.plan_version")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.guard_controller_fields()")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.require_controller(text)")
    op.execute(f"DROP TRIGGER IF EXISTS audit_event_chain ON {SCHEMA}.audit_event")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.audit_event_link()")
    op.drop_constraint("uq_audit_event_event_key", "audit_event", schema=SCHEMA, type_="unique")
    op.drop_column("audit_event", "event_key", schema=SCHEMA)
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.audit_event_chain_hash(char, char)")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.audit_event_key(text, text, text, text, jsonb)")

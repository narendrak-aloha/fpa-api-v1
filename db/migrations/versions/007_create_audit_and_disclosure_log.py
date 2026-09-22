"""create audit and disclosure log

LLM disclosure metadata and the hash-linked, append-only audit event log.

Revision ID: 007
Revises: 006
Create Date: 2026-09-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "007"
down_revision: Union[str, Sequence[str], None] = "006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "fpa_governance"


def upgrade() -> None:
    op.create_table(
        "llm_disclosure_log",
        sa.Column("disclosure_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("scope", postgresql.JSONB(), nullable=False),
        sa.Column("field_classes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("model_name", sa.Text(), nullable=False),
        sa.Column("payload_sha256", sa.CHAR(64), nullable=False),
        sa.Column("response_sha256", sa.CHAR(64), nullable=True),
        sa.Column("disclosed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], [f"{SCHEMA}.app_user.user_id"], name="fk_llm_disclosure_log_user_id_app_user"),
        sa.PrimaryKeyConstraint("disclosure_id", name="pk_llm_disclosure_log"),
        schema=SCHEMA,
    )
    op.create_table(
        "audit_event",
        sa.Column("audit_event_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("actor_user_id", sa.Text(), nullable=True),
        sa.Column("entity_type", sa.Text(), nullable=False),
        sa.Column("entity_id", sa.Text(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("previous_hash", sa.CHAR(64), nullable=True),
        sa.Column("event_hash", sa.CHAR(64), nullable=False),
        sa.ForeignKeyConstraint(["actor_user_id"], [f"{SCHEMA}.app_user.user_id"], name="fk_audit_event_actor_user_id_app_user"),
        sa.PrimaryKeyConstraint("audit_event_id", name="pk_audit_event"),
        sa.UniqueConstraint("event_hash", name="uq_audit_event_event_hash"),
        schema=SCHEMA,
    )
    op.create_index("audit_entity_idx", "audit_event", ["entity_type", "entity_id", "occurred_at"], schema=SCHEMA)
    op.execute(f"""
        CREATE OR REPLACE FUNCTION {SCHEMA}.audit_event_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'audit_event is append-only';
        END $$;
    """)
    op.execute(f"""
        CREATE TRIGGER audit_event_no_update BEFORE UPDATE OR DELETE ON {SCHEMA}.audit_event
        FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.audit_event_append_only();
    """)


def downgrade() -> None:
    op.execute(f"DROP TRIGGER IF EXISTS audit_event_no_update ON {SCHEMA}.audit_event")
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.audit_event_append_only()")
    op.drop_index("audit_entity_idx", table_name="audit_event", schema=SCHEMA)
    op.drop_table("audit_event", schema=SCHEMA)
    op.drop_table("llm_disclosure_log", schema=SCHEMA)

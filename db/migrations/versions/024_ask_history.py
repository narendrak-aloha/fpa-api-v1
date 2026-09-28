"""ask history: every question a person asked and the answer they got

Revision ID: 024
Revises: 023
Create Date: 2026-09-28

One row per question, owned by the person who asked it and shown to nobody
else. The answer is kept as it was returned, so a past question can be
re-read without re-running it. ``companies`` is the scope the answer was
computed under; if the person's scope later loses any of those companies,
the API withholds the stored figures and offers to ask again instead.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "024"
down_revision = "023"
branch_labels = depends_on = None

SCHEMA = "fpa_governance"


def upgrade() -> None:
    op.create_table(
        "ask_history",
        sa.Column("ask_id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("companies", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False),
        sa.Column("response", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("btrim(question) <> ''", name=op.f("ck_ask_history_question_not_empty")),
        sa.ForeignKeyConstraint(["user_id"], [f"{SCHEMA}.app_user.user_id"], name="fk_ask_history_user_id_app_user",
                                ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("ask_id", name="pk_ask_history"),
        schema=SCHEMA,
    )
    op.create_index("ask_history_user_idx", "ask_history", ["user_id", sa.text("created_at DESC")], schema=SCHEMA)


def downgrade() -> None:
    op.drop_index("ask_history_user_idx", table_name="ask_history", schema=SCHEMA)
    op.drop_table("ask_history", schema=SCHEMA)

"""create schema and access control

Users, roles and role membership used for segregation of duties.

Revision ID: 001
Revises:
Create Date: 2026-09-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "001"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "fpa_governance"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
    op.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")

    op.create_table(
        "app_user",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("user_id", name="pk_app_user"),
        sa.UniqueConstraint("email", name="uq_app_user_email"),
        schema=SCHEMA,
    )
    op.create_table(
        "role",
        sa.Column("role_code", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("role_code", name="pk_role"),
        schema=SCHEMA,
    )
    op.create_table(
        "user_role",
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("role_code", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], [f"{SCHEMA}.app_user.user_id"], name="fk_user_role_user_id_app_user"),
        sa.ForeignKeyConstraint(["role_code"], [f"{SCHEMA}.role.role_code"], name="fk_user_role_role_code_role"),
        sa.PrimaryKeyConstraint("user_id", "role_code", name="pk_user_role"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("user_role", schema=SCHEMA)
    op.drop_table("role", schema=SCHEMA)
    op.drop_table("app_user", schema=SCHEMA)

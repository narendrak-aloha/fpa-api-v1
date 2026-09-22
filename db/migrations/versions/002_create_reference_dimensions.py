"""create reference dimensions

Company, account and cost-centre masters plus sealed ledger vintages.

Revision ID: 002
Revises: 001
Create Date: 2026-09-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: Union[str, Sequence[str], None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "fpa_governance"


def upgrade() -> None:
    op.create_table(
        "dim_company",
        sa.Column("company_code", sa.Text(), nullable=False),
        sa.Column("company_name", sa.Text(), nullable=False),
        sa.Column("country_code", sa.CHAR(2), nullable=False),
        sa.Column("region", sa.Text(), nullable=False),
        sa.Column("functional_currency", sa.CHAR(3), nullable=False),
        sa.CheckConstraint("country_code = upper(country_code)", name=op.f("ck_dim_company_country_code_upper")),
        sa.CheckConstraint("functional_currency = upper(functional_currency)", name=op.f("ck_dim_company_functional_currency_upper")),
        sa.PrimaryKeyConstraint("company_code", name="pk_dim_company"),
        schema=SCHEMA,
    )
    op.create_table(
        "dim_account",
        sa.Column("account_code", sa.Text(), nullable=False),
        sa.Column("account_name", sa.Text(), nullable=False),
        sa.Column("account_type", sa.Text(), nullable=False),
        sa.Column("engine_tag", sa.Text(), nullable=False),
        sa.CheckConstraint("account_type IN ('Revenue', 'COGS', 'OpEx')", name=op.f("ck_dim_account_account_type")),
        sa.CheckConstraint("engine_tag IN ('Services', 'Recurring', 'Shared')", name=op.f("ck_dim_account_engine_tag")),
        sa.PrimaryKeyConstraint("account_code", name="pk_dim_account"),
        schema=SCHEMA,
    )
    op.create_table(
        "dim_cost_center",
        sa.Column("cost_center_code", sa.Text(), nullable=False),
        sa.Column("country_code", sa.CHAR(2), nullable=False),
        sa.Column("practice_code", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("cost_center_code", name="pk_dim_cost_center"),
        schema=SCHEMA,
    )
    op.create_table(
        "ledger_vintage",
        sa.Column("vintage", sa.Integer(), autoincrement=False, nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.CheckConstraint("vintage > 0", name=op.f("ck_ledger_vintage_vintage_positive")),
        sa.PrimaryKeyConstraint("vintage", name="pk_ledger_vintage"),
        schema=SCHEMA,
    )


def downgrade() -> None:
    op.drop_table("ledger_vintage", schema=SCHEMA)
    op.drop_table("dim_cost_center", schema=SCHEMA)
    op.drop_table("dim_account", schema=SCHEMA)
    op.drop_table("dim_company", schema=SCHEMA)

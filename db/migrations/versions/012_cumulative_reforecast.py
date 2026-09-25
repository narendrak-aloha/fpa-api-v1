"""cumulative re-forecasts: a publication records the whole shock set it applied

A re-forecast used to be computed from the frozen baseline with only its own
shocks, so approving utilisation and then heads published a heads-only plan
over the utilisation one, and the utilisation commitments stayed reserved
for numbers the cube no longer showed. A publication now records the full,
cumulative shock set it applied, the next run starts from it, and a
superseded revision's commitments are released once the new one commits.

Revision ID: 012
Revises: 011
Create Date: 2026-09-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "012"
down_revision: Union[str, Sequence[str], None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

SCHEMA = "fpa_governance"
# 008 passed an already-prefixed name through the naming convention, so the
# constraint in the database carries the prefix twice. This is its real name.
STATE_CHECK = "ck_plan_publication_ck_plan_publication_state"


def upgrade() -> None:
    op.add_column(
        "plan_publication",
        sa.Column("shocks", postgresql.JSONB(), server_default=sa.text("'[]'::jsonb"), nullable=False),
        schema=SCHEMA,
    )
    op.execute(f"ALTER TABLE {SCHEMA}.plan_publication DROP CONSTRAINT {STATE_CHECK}")
    op.execute(
        f"ALTER TABLE {SCHEMA}.plan_publication ADD CONSTRAINT {STATE_CHECK} CHECK "
        "(state IN ('RESERVED', 'PUBLISHED', 'COMMITTED', 'SUPERSEDED', 'COMPENSATED', 'COMPENSATION_FAILED'))"
    )


def downgrade() -> None:
    op.execute(f"ALTER TABLE {SCHEMA}.plan_publication DROP CONSTRAINT {STATE_CHECK}")
    op.execute(
        f"ALTER TABLE {SCHEMA}.plan_publication ADD CONSTRAINT {STATE_CHECK} CHECK "
        "(state IN ('RESERVED', 'PUBLISHED', 'COMMITTED', 'COMPENSATED', 'COMPENSATION_FAILED'))"
    )
    op.drop_column("plan_publication", "shocks", schema=SCHEMA)

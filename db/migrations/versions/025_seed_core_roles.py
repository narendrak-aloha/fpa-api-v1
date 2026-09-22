"""seed core roles

Revision ID: 025
Revises: 024
Create Date: 2026-09-22

The role table is a fixed enumeration tied to FK constraints (user_role,
plan_state_transition), not environment-specific business data, so it
belongs here rather than in db/seed.yaml. db/seed.yaml's role entries were
the only source before this and only load after `alembic upgrade head`
(see docker/entrypoint.sh) -- a migration that referenced a role_code
before it existed would fail on a brand-new database (022 did exactly
this; fixed there by seeding what it needed inline). Seeding the full set
here means no migration after this one has to worry about it again.
"""
from alembic import op

revision = "025"
down_revision = "024"
branch_labels = depends_on = None

SCHEMA = "fpa_governance"

# Mirrors db/seed.yaml's role section, which stays as the single source of
# truth for descriptions; this only needs the rows to exist early enough.
ROLES = [
    ("analyst", "Read governed plans and analytical results"),
    ("planner", "Create and revise draft plans"),
    ("controller", "Maintain rates/covenants and approve plans"),
    ("cfo", "Approve and lock plans"),
    ("service", "Service identity for workflow publication"),
    ("agent", "The Agno team. Drafts and investigates; commits nothing, closes nothing"),
    ("superadmin", "Approve accounts and grant roles and company access; no business role, no company data"),
]


def upgrade() -> None:
    for role_code, description in ROLES:
        op.execute(
            f"INSERT INTO {SCHEMA}.role (role_code, description) VALUES ('{role_code}', '{description}') "
            "ON CONFLICT DO NOTHING"
        )


def downgrade() -> None:
    # Reference data: leave it in place. user_role/plan_state_transition rows
    # may depend on it, and downgrading past 001 drops the table anyway.
    pass

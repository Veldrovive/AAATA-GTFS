"""Baseline: the schema as shipped before Alembic (user_version 2).

Databases from before Alembic are brought to user_version 2 and stamped here
(`db.migrate`); new ones run the same SQL, so both have identical schemas.

Revision ID: 0001
Revises:
Create Date: 2026-10-26
"""
from collections.abc import Sequence

from alembic import op

from aaata_gtfs.migrations import legacy

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for script in legacy.MIGRATIONS:
        for statement in legacy.statements(script):
            op.execute(statement)


def downgrade() -> None:
    raise NotImplementedError("the baseline can't be downgraded")

"""Record operator-reported intervention outcomes with observation references.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import swarmos.models

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "intervention_outcomes",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("action_id", sa.String(length=32), nullable=False),
        sa.Column("city_id", sa.String(length=64), nullable=False),
        sa.Column("recorded_by", sa.String(length=32), nullable=True),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
        sa.Column("measurement_ids", sa.JSON(), nullable=False),
        sa.Column("recorded_at", swarmos.models.UTCDateTime(), nullable=False),
        sa.Column("updated_at", swarmos.models.UTCDateTime(), nullable=False),
        sa.CheckConstraint("outcome in ('improved','unchanged','worsened','unverified')", name="outcome_valid"),
        sa.ForeignKeyConstraint(["action_id"], ["proposed_actions.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["city_id"], ["cities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recorded_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("action_id"),
    )
    op.create_index("ix_intervention_outcomes_action_id", "intervention_outcomes", ["action_id"])
    op.create_index("ix_intervention_outcomes_city_id", "intervention_outcomes", ["city_id"])
    op.create_index("ix_intervention_outcomes_recorded_at", "intervention_outcomes", ["recorded_at"])


def downgrade() -> None:
    op.drop_index("ix_intervention_outcomes_recorded_at", table_name="intervention_outcomes")
    op.drop_index("ix_intervention_outcomes_city_id", table_name="intervention_outcomes")
    op.drop_index("ix_intervention_outcomes_action_id", table_name="intervention_outcomes")
    op.drop_table("intervention_outcomes")

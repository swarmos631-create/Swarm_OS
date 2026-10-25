"""Persist saved scenarios against durable agent tasks.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import swarmos.models

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scenario_runs",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("city_id", sa.String(length=32), nullable=False),
        sa.Column("task_id", sa.String(length=32), nullable=False),
        sa.Column("created_by", sa.String(length=32), nullable=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("params", sa.JSON(), nullable=False),
        sa.Column("created_at", swarmos.models.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["city_id"], ["cities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["task_id"], ["agent_tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("task_id"),
    )
    op.create_index("ix_scenario_runs_city_id", "scenario_runs", ["city_id"])
    op.create_index("ix_scenario_runs_task_id", "scenario_runs", ["task_id"])
    op.create_index("ix_scenario_runs_created_at", "scenario_runs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_scenario_runs_created_at", table_name="scenario_runs")
    op.drop_index("ix_scenario_runs_task_id", table_name="scenario_runs")
    op.drop_index("ix_scenario_runs_city_id", table_name="scenario_runs")
    op.drop_table("scenario_runs")

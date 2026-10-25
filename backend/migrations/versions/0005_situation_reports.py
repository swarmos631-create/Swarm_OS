"""Persist generated city situation snapshots.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import swarmos.models

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "situation_reports",
        sa.Column("id", sa.String(length=32), nullable=False),
        sa.Column("city_id", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=32), nullable=True),
        sa.Column("title", sa.String(length=160), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", swarmos.models.UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(["city_id"], ["cities.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_situation_reports_city_id", "situation_reports", ["city_id"])
    op.create_index("ix_situation_reports_created_at", "situation_reports", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_situation_reports_created_at", table_name="situation_reports")
    op.drop_index("ix_situation_reports_city_id", table_name="situation_reports")
    op.drop_table("situation_reports")

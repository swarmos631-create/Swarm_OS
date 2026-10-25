"""Add auditable links from incidents to existing evidence records.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

import swarmos.models

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "incident_evidence",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("incident_id", sa.String(length=32), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("source_id", sa.String(length=64), nullable=False),
        sa.Column("relationship", sa.String(length=20), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=32), nullable=True),
        sa.Column("created_at", swarmos.models.UTCDateTime(), nullable=False),
        sa.CheckConstraint("relationship in ('supports','contradicts','context')", name="relationship_valid"),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("incident_id", "source_type", "source_id", name="uq_incident_evidence_source"),
    )
    op.create_index("ix_incident_evidence_incident_id", "incident_evidence", ["incident_id"])


def downgrade() -> None:
    op.drop_index("ix_incident_evidence_incident_id", table_name="incident_evidence")
    op.drop_table("incident_evidence")

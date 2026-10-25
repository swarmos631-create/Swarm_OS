"""Add shared hashed fixed-window rate-limit counters.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "shared_rate_limit_buckets",
        sa.Column("bucket_key", sa.String(length=96), nullable=False),
        sa.Column("hits", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("bucket_key"),
    )
    op.create_index("ix_shared_rate_limit_buckets_expires_at", "shared_rate_limit_buckets", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_shared_rate_limit_buckets_expires_at", table_name="shared_rate_limit_buckets")
    op.drop_table("shared_rate_limit_buckets")

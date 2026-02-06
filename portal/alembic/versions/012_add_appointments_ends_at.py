"""Add appointments.ends_at column (if missing)

Some deployments had appointments table without ends_at.
This migration adds ends_at and backfills it using starts_at + 45 minutes
when null.

Revision ID: 012_add_appointments_ends_at
Revises: 013_merge_heads
Create Date: 2026-02-06
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "012_add_appointments_ends_at"
down_revision = "013_merge_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add column safely (Postgres)
    op.execute(sa.text("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS ends_at TIMESTAMP"))
    # Add index if desired
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_appointments_ends_at ON appointments (ends_at)"))
    # Backfill nulls where possible
    op.execute(sa.text("UPDATE appointments SET ends_at = starts_at + INTERVAL '45 minutes' WHERE ends_at IS NULL"))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_appointments_ends_at"))
    op.execute(sa.text("ALTER TABLE appointments DROP COLUMN IF EXISTS ends_at"))

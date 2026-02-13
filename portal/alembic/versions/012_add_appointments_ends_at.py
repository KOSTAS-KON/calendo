"""Add appointments.ends_at column (if missing)

Revision ID: 012_add_appointments_ends_at
Revises: 011_add_tenant_archived_at
Create Date: 2026-02-13

This migration is intentionally defensive:
- Adds the `ends_at` column only if it doesn't already exist.
- Backfills existing rows where ends_at is NULL using starts_at + 45 minutes.

"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "012_add_appointments_ends_at"
down_revision = "011_add_tenant_archived_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add column if it doesn't exist (Postgres)
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
              IF NOT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'appointments'
                  AND column_name = 'ends_at'
              ) THEN
                ALTER TABLE appointments ADD COLUMN ends_at TIMESTAMP WITH TIME ZONE;
                CREATE INDEX IF NOT EXISTS ix_appointments_ends_at ON appointments (ends_at);
              END IF;
            END $$;
            """
        )
    )

    # Backfill NULL ends_at
    op.execute(
        sa.text(
            """
            UPDATE appointments
               SET ends_at = starts_at + INTERVAL '45 minutes'
             WHERE ends_at IS NULL;
            """
        )
    )


def downgrade() -> None:
    # Drop index + column if present
    op.execute(sa.text("DROP INDEX IF EXISTS ix_appointments_ends_at;"))
    op.execute(
        sa.text(
            """
            DO $$
            BEGIN
              IF EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_name = 'appointments'
                  AND column_name = 'ends_at'
              ) THEN
                ALTER TABLE appointments DROP COLUMN ends_at;
              END IF;
            END $$;
            """
        )
    )

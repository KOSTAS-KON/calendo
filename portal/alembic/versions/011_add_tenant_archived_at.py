"""tenant archive fields (compat revision)

Revision ID: 011_add_tenant_archived_at
Revises: 010_fix_clinic_settings_id_identity
Create Date: 2026-01-30
"""

from alembic import op
import sqlalchemy as sa

revision = "011_add_tenant_archived_at"
down_revision = "010_fix_clinic_settings_id_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # DB might already have these columns if previously migrated.
    # Use Postgres IF NOT EXISTS to be idempotent.
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS is_archived BOOLEAN NOT NULL DEFAULT FALSE;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP NULL;")

    # Remove default after creation (optional; keeps schema clean)
    try:
        op.execute("ALTER TABLE tenants ALTER COLUMN is_archived DROP DEFAULT;")
    except Exception:
        # Not fatal if default already removed
        pass


def downgrade() -> None:
    # Safe downgrade: drop if exists
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS archived_at;")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS is_archived;")

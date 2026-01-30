"""add tenant soft delete fields

Revision ID: 011_add_tenant_archive_softdelete
Revises: 011_add_tenant_archived_at
Create Date: 2026-01-30
"""

from alembic import op

revision = "011_add_tenant_archive_softdelete"
down_revision = "011_add_tenant_archived_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP NULL;")
    op.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS deleted_by VARCHAR(255) NULL;")


def downgrade() -> None:
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS deleted_by;")
    op.execute("ALTER TABLE tenants DROP COLUMN IF EXISTS deleted_at;")

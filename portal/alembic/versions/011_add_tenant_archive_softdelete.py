"""add_tenant_archive_softdelete

Revision ID: 011
Revises: 010
Create Date: 2026-01-30
"""

from alembic import op
import sqlalchemy as sa


revision = "011"
down_revision = "010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.text("false")))
    op.add_column("tenants", sa.Column("archived_at", sa.DateTime(), nullable=True))
    op.add_column("tenants", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    op.add_column("tenants", sa.Column("deleted_by", sa.String(length=255), nullable=True))

    op.create_index("ix_tenants_is_archived", "tenants", ["is_archived"], unique=False)
    op.create_index("ix_tenants_deleted_at", "tenants", ["deleted_at"], unique=False)

    # remove server default after backfill
    op.alter_column("tenants", "is_archived", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_tenants_deleted_at", table_name="tenants")
    op.drop_index("ix_tenants_is_archived", table_name="tenants")
    op.drop_column("tenants", "deleted_by")
    op.drop_column("tenants", "deleted_at")
    op.drop_column("tenants", "archived_at")
    op.drop_column("tenants", "is_archived")

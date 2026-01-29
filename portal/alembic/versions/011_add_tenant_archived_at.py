"""add tenants.archived_at

Revision ID: 011_add_tenant_archived_at
Revises: 010_fix_clinic_settings_id_identity
Create Date: 2026-01-29
"""

from alembic import op
import sqlalchemy as sa


revision = "011_add_tenant_archived_at"
down_revision = "010_fix_clinic_settings_id_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("archived_at", sa.DateTime(), nullable=True))
    op.create_index("ix_tenants_archived_at", "tenants", ["archived_at"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_tenants_archived_at", table_name="tenants")
    op.drop_column("tenants", "archived_at")

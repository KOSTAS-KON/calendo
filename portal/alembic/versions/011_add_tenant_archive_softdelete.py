"""add tenant archive + soft delete fields

Revision ID: 011_add_tenant_archive_softdelete
Revises: 010_fix_clinic_settings_id_identity
Create Date: 2026-01-30
"""

from alembic import op
import sqlalchemy as sa

revision = "011_add_tenant_archive_softdelete"
down_revision = "010_fix_clinic_settings_id_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # tenants: archive + soft delete fields
    op.add_column(
        "tenants",
        sa.Column("is_archived", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column("tenants", sa.Column("archived_at", sa.DateTime(), nullable=True))

    op.add_column("tenants", sa.Column("deleted_at", sa.DateTime(), nullable=True))
    op.add_column("tenants", sa.Column("deleted_by", sa.String(length=255), nullable=True))

    # remove server_default so ORM doesn't keep it forever
    op.alter_column("tenants", "is_archived", server_default=None)


def downgrade() -> None:
    op.drop_column("tenants", "deleted_by")
    op.drop_column("tenants", "deleted_at")
    op.drop_column("tenants", "archived_at")
    op.drop_column("tenants", "is_archived")

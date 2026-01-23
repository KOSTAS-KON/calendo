"""$enforce_tenant_constraints

Revision ID: 004
Revises: 003
Create Date: 2026-01-23T17:13:31.084827"""

from alembic import op
import sqlalchemy as sa


revision = "004"
down_revision = '003'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("clinic_settings", "tenant_id", existing_type=sa.String(length=36), nullable=False)
    op.create_foreign_key("fk_clinic_settings_tenant", "clinic_settings", "tenants", ["tenant_id"], ["id"])
    op.create_unique_constraint("uq_clinic_settings_tenant_id", "clinic_settings", ["tenant_id"])

    for table, fkname in (("children","fk_children_tenant"), ("therapists","fk_therapists_tenant"), ("appointments","fk_appointments_tenant")):
        op.alter_column(table, "tenant_id", existing_type=sa.String(length=36), nullable=False)
        op.create_foreign_key(fkname, table, "tenants", ["tenant_id"], ["id"])



def downgrade() -> None:
    pass

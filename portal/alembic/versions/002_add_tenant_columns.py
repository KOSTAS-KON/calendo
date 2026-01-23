"""$add_tenant_columns

Revision ID: 002
Revises: 001
Create Date: 2026-01-23T17:13:31.083457"""

from alembic import op
import sqlalchemy as sa


revision = "002"
down_revision = '001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # clinic_settings
    op.add_column("clinic_settings", sa.Column("tenant_id", sa.String(length=36), nullable=True))
    op.create_index("ix_clinic_settings_tenant_id", "clinic_settings", ["tenant_id"], unique=False)
    op.add_column("clinic_settings", sa.Column("google_maps_link", sa.String(length=500), nullable=True, server_default=""))

    # children
    op.add_column("children", sa.Column("tenant_id", sa.String(length=36), nullable=True))
    op.create_index("ix_children_tenant_id", "children", ["tenant_id"], unique=False)

    # therapists
    op.add_column("therapists", sa.Column("tenant_id", sa.String(length=36), nullable=True))
    op.create_index("ix_therapists_tenant_id", "therapists", ["tenant_id"], unique=False)

    # appointments
    op.add_column("appointments", sa.Column("tenant_id", sa.String(length=36), nullable=True))
    op.create_index("ix_appointments_tenant_id", "appointments", ["tenant_id"], unique=False)



def downgrade() -> None:
    pass

"""$backfill_default_tenant

Revision ID: 003
Revises: 002
Create Date: 2026-01-23T17:13:31.084357"""

from alembic import op
import sqlalchemy as sa


revision = "003"
down_revision = '002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    import uuid
    conn = op.get_bind()
    tenant_id = str(uuid.uuid4())
    conn.execute(sa.text("INSERT INTO tenants (id, slug, name, status) VALUES (:id, 'default', 'Default Tenant', 'active')"),
                 {"id": tenant_id})

    for table in ("clinic_settings", "children", "therapists", "appointments"):
        conn.execute(sa.text(f"UPDATE {table} SET tenant_id = :tid WHERE tenant_id IS NULL"), {"tid": tenant_id})



def downgrade() -> None:
    pass

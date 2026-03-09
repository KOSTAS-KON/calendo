"""Add child therapist assignments and user/therapist link

Revision ID: 015_add_child_therapist_assignments_and_user_fields
Revises: 014_add_billing_item_details
Create Date: 2026-03-09
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "015_add_child_therapist_assignments_and_user_fields"
down_revision = "014_add_billing_item_details"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE users ADD COLUMN IF NOT EXISTS job_title VARCHAR(120)"))
    op.execute(sa.text("ALTER TABLE therapists ADD COLUMN IF NOT EXISTS user_id VARCHAR(36)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_therapists_user_id ON therapists (user_id)"))
    op.execute(sa.text("""
        CREATE TABLE IF NOT EXISTS child_therapist_assignments (
            id SERIAL PRIMARY KEY,
            tenant_id VARCHAR(36) NOT NULL,
            child_id INTEGER NOT NULL,
            therapist_id INTEGER NOT NULL,
            assigned_by_user_id VARCHAR(36),
            is_active BOOLEAN NOT NULL DEFAULT TRUE,
            assigned_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_child_therapist_assignments_tenant_id ON child_therapist_assignments (tenant_id)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_child_therapist_assignments_child_id ON child_therapist_assignments (child_id)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_child_therapist_assignments_therapist_id ON child_therapist_assignments (therapist_id)"))
    # backfill therapist.user_id by matching email where possible
    op.execute(sa.text("""
        UPDATE therapists t
        SET user_id = u.id
        FROM users u
        WHERE t.user_id IS NULL
          AND u.tenant_id = t.tenant_id
          AND lower(coalesce(u.email,'')) = lower(coalesce(t.email,''))
    """))
    # backfill assignment rows for existing appointments
    op.execute(sa.text("""
        INSERT INTO child_therapist_assignments (tenant_id, child_id, therapist_id, assigned_by_user_id, is_active)
        SELECT DISTINCT a.tenant_id, a.child_id, t.id, NULL, TRUE
        FROM appointments a
        JOIN therapists t ON t.tenant_id = a.tenant_id AND lower(coalesce(t.name,'')) = lower(coalesce(a.therapist_name,''))
        LEFT JOIN child_therapist_assignments x ON x.tenant_id = a.tenant_id AND x.child_id = a.child_id AND x.therapist_id = t.id
        WHERE a.child_id IS NOT NULL AND x.id IS NULL
    """))


def downgrade() -> None:
    op.execute(sa.text("DROP TABLE IF EXISTS child_therapist_assignments"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_therapists_user_id"))
    # keep downgrade safe
    op.execute(sa.text("ALTER TABLE therapists DROP COLUMN IF EXISTS user_id"))
    op.execute(sa.text("ALTER TABLE users DROP COLUMN IF EXISTS job_title"))

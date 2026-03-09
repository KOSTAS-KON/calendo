"""Add archive flags to children and therapists

Revision ID: 016_add_people_archive_flags
Revises: 015_add_child_therapist_assignments_and_user_fields
Create Date: 2026-03-08
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "016_add_people_archive_flags"
down_revision = "015_add_child_therapist_assignments_and_user_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("ALTER TABLE children ADD COLUMN IF NOT EXISTS is_archived BOOLEAN"))
    op.execute(sa.text("ALTER TABLE children ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP"))
    op.execute(sa.text("UPDATE children SET is_archived = FALSE WHERE is_archived IS NULL"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_children_is_archived ON children (is_archived)"))

    op.execute(sa.text("ALTER TABLE therapists ADD COLUMN IF NOT EXISTS is_archived BOOLEAN"))
    op.execute(sa.text("ALTER TABLE therapists ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP"))
    op.execute(sa.text("UPDATE therapists SET is_archived = FALSE WHERE is_archived IS NULL"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_therapists_is_archived ON therapists (is_archived)"))


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_therapists_is_archived"))
    op.execute(sa.text("ALTER TABLE therapists DROP COLUMN IF EXISTS archived_at"))
    op.execute(sa.text("ALTER TABLE therapists DROP COLUMN IF EXISTS is_archived"))

    op.execute(sa.text("DROP INDEX IF EXISTS ix_children_is_archived"))
    op.execute(sa.text("ALTER TABLE children DROP COLUMN IF EXISTS archived_at"))
    op.execute(sa.text("ALTER TABLE children DROP COLUMN IF EXISTS is_archived"))

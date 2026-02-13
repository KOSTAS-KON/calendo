"""Merge heads: tenant soft-delete + appointments.ends_at

Revision ID: 013_merge_heads
Revises: 011_add_tenant_archive_softdelete, 012_add_appointments_ends_at
Create Date: 2026-02-13

This is a merge revision to reconcile multiple heads.
It performs no schema changes.

"""

from __future__ import annotations

# revision identifiers, used by Alembic.
revision = "013_merge_heads"
down_revision = ("011_add_tenant_archive_softdelete", "012_add_appointments_ends_at")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

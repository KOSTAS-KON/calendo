"""Merge Alembic heads (resolve multiple-head error)

Revision ID: 013_merge_heads
Revises: 011_add_tenant_archived_at, 011_add_tenant_archive_softdelete
Create Date: 2026-02-06
"""
from __future__ import annotations

revision = "013_merge_heads"
down_revision = ("011_add_tenant_archived_at", "011_add_tenant_archive_softdelete")
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Merge migration — no schema changes
    pass


def downgrade() -> None:
    pass

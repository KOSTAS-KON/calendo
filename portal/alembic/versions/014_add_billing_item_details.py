"""Add billing item amount + description fields

Revision ID: 014_add_billing_item_details
Revises: 013_merge_heads
Create Date: 2026-02-14

This patch stores Calendar "billing" inputs (amount + description) in the DB.
It is written defensively so it can run on older client DBs that may already
have partial schema changes.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "014_add_billing_item_details"
down_revision = "013_merge_heads"
branch_labels = None
depends_on = None


def _has_column(bind, table: str, col: str) -> bool:
    try:
        insp = sa.inspect(bind)
        cols = insp.get_columns(table)
        names = {c.get("name") for c in cols}
        return col in names
    except Exception:
        return False


def upgrade() -> None:
    bind = op.get_bind()

    if not _has_column(bind, "billing_items", "amount_cents"):
        op.add_column("billing_items", sa.Column("amount_cents", sa.Integer(), nullable=True))

    if not _has_column(bind, "billing_items", "currency"):
        op.add_column(
            "billing_items",
            sa.Column(
                "currency",
                sa.String(length=8),
                nullable=True,
                server_default=sa.text("'EUR'"),
            ),
        )
        # Backfill for existing rows
        try:
            op.execute(sa.text("UPDATE billing_items SET currency='EUR' WHERE currency IS NULL"))
        except Exception:
            pass
        # Remove default where supported (keeps ORM-level default)
        try:
            op.alter_column("billing_items", "currency", server_default=None)
        except Exception:
            pass

    if not _has_column(bind, "billing_items", "description"):
        op.add_column("billing_items", sa.Column("description", sa.Text(), nullable=True))


def downgrade() -> None:
    bind = op.get_bind()

    # Best-effort: SQLite may not support DROP COLUMN; ignore downgrade failures.
    for col in ("description", "currency", "amount_cents"):
        if _has_column(bind, "billing_items", col):
            try:
                op.drop_column("billing_items", col)
            except Exception:
                pass

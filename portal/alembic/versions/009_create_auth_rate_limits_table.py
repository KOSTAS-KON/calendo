"""create auth_rate_limits table

Revision ID: 009_create_auth_rate_limits_table
Revises: 008_create_sms_outbox_table
Create Date: 2026-01-28
"""

from alembic import op
import sqlalchemy as sa


revision = "009_create_auth_rate_limits_table"
down_revision = "008_create_sms_outbox_table"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "auth_rate_limits",
        sa.Column("ip", sa.String(length=64), primary_key=True),
        sa.Column("window_start", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("blocked_until", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_auth_rate_limits_window_start", "auth_rate_limits", ["window_start"])


def downgrade():
    op.drop_index("ix_auth_rate_limits_window_start", table_name="auth_rate_limits")
    op.drop_table("auth_rate_limits")

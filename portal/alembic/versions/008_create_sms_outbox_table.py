"""create sms_outbox table

Revision ID: 008_create_sms_outbox_table
Revises: 007_add_must_reset_password
Create Date: 2026-01-28
"""

from alembic import op
import sqlalchemy as sa


revision = "008_create_sms_outbox_table"
down_revision = "007_add_must_reset_password"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "sms_outbox",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), sa.ForeignKey("tenants.id"), nullable=False),

        sa.Column("to_phone", sa.String(length=80), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),

        sa.Column("scheduled_at", sa.DateTime(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="queued"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("provider_message_id", sa.String(length=200), nullable=True),

        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )

    op.create_index("ix_sms_outbox_tenant_id", "sms_outbox", ["tenant_id"])
    op.create_index("ix_sms_outbox_status", "sms_outbox", ["status"])
    op.create_index("ix_sms_outbox_scheduled_at", "sms_outbox", ["scheduled_at"])
    op.create_index("ix_sms_outbox_tenant_sched", "sms_outbox", ["tenant_id", "scheduled_at"])


def downgrade():
    op.drop_index("ix_sms_outbox_tenant_sched", table_name="sms_outbox")
    op.drop_index("ix_sms_outbox_scheduled_at", table_name="sms_outbox")
    op.drop_index("ix_sms_outbox_status", table_name="sms_outbox")
    op.drop_index("ix_sms_outbox_tenant_id", table_name="sms_outbox")
    op.drop_table("sms_outbox")

"""add must_reset_password to users

Revision ID: 007_add_must_reset_password
Revises: 006_create_users_table
Create Date: 2026-01-28
"""

from alembic import op
import sqlalchemy as sa


revision = "007_add_must_reset_password"
down_revision = "006_create_users_table"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("users", sa.Column("must_reset_password", sa.Boolean(), nullable=False, server_default=sa.text("false")))


def downgrade():
    op.drop_column("users", "must_reset_password")

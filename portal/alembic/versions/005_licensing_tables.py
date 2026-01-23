"""$licensing_tables

Revision ID: 005
Revises: 004
Create Date: 2026-01-23T17:13:31.085352"""

from alembic import op
import sqlalchemy as sa


revision = "005"
down_revision = '004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plans",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("code", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("duration_days", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("features_json", sa.Text(), nullable=False, server_default="{}"),
    )
    op.create_index("ix_plans_code", "plans", ["code"], unique=True)

    op.create_table(
        "subscriptions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="active"),
        sa.Column("starts_at", sa.DateTime(), nullable=True),
        sa.Column("ends_at", sa.DateTime(), nullable=True),
        sa.Column("source", sa.String(length=30), nullable=False, server_default="manual"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"]),
    )
    op.create_index("ix_subscriptions_tenant_id", "subscriptions", ["tenant_id"], unique=False)

    op.create_table(
        "activation_codes",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("code_hash", sa.String(length=200), nullable=False),
        sa.Column("issued_at", sa.DateTime(), nullable=True),
        sa.Column("redeem_by", sa.DateTime(), nullable=True),
        sa.Column("max_redemptions", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("redeemed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("note", sa.String(length=300), nullable=False, server_default=""),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(["plan_id"], ["plans.id"]),
    )
    op.create_index("ix_activation_codes_code_hash", "activation_codes", ["code_hash"], unique=True)
    op.create_index("ix_activation_codes_tenant_id", "activation_codes", ["tenant_id"], unique=False)

    op.create_table(
        "license_audit_log",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("tenant_id", sa.String(length=36), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("ip", sa.String(length=80), nullable=False, server_default=""),
        sa.Column("user_agent", sa.String(length=300), nullable=False, server_default=""),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
    )
    op.create_index("ix_license_audit_tenant", "license_audit_log", ["tenant_id"], unique=False)

    # seed plans
    conn = op.get_bind()
    conn.execute(sa.text("INSERT INTO plans (code, name, duration_days, features_json) VALUES "
                         "('TRIAL_7D','7-day Trial',7,'{}'),"
                         "('MONTHLY_30D','Monthly',30,'{}'),"
                         "('YEARLY_365D','Yearly',365,'{}')"))



def downgrade() -> None:
    pass

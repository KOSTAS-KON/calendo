"""fix clinic_settings id default sequence

Revision ID: 010_fix_clinic_settings_id_identity
Revises: 009_create_auth_rate_limits_table
Create Date: 2026-01-28
"""

from alembic import op
import sqlalchemy as sa

revision = "010_fix_clinic_settings_id_identity"
down_revision = "009_create_auth_rate_limits_table"
branch_labels = None
depends_on = None


def upgrade():
    # Ensure sequence exists
    op.execute(sa.text("CREATE SEQUENCE IF NOT EXISTS clinic_settings_id_seq;"))

    # Ensure column uses the sequence by default
    op.execute(
        sa.text(
            "ALTER TABLE clinic_settings "
            "ALTER COLUMN id SET DEFAULT nextval('clinic_settings_id_seq');"
        )
    )

    # Ensure sequence is owned by the column
    op.execute(sa.text("ALTER SEQUENCE clinic_settings_id_seq OWNED BY clinic_settings.id;"))

    # If any rows somehow have NULL id, backfill them (should be rare)
    op.execute(sa.text("UPDATE clinic_settings SET id = nextval('clinic_settings_id_seq') WHERE id IS NULL;"))

    # IMPORTANT: sync sequence to avoid duplicates (e.g., sequence still at 1 while id=1 already exists)
    # Set sequence to MAX(id)+1 and mark is_called=false so the next nextval() returns exactly that value.
    op.execute(
        sa.text(
            """
            SELECT setval(
              'clinic_settings_id_seq',
              COALESCE((SELECT MAX(id) FROM clinic_settings), 0) + 1,
              false
            );
            """
        )
    )


def downgrade():
    # Remove default (do not drop the sequence automatically in downgrade)
    op.execute(sa.text("ALTER TABLE clinic_settings ALTER COLUMN id DROP DEFAULT;"))

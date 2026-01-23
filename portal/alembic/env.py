from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# --- Make sure "app" is importable when Alembic runs in Docker/Render ---
BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# Now imports work
from app.db import Base  # noqa: E402

# IMPORTANT: import models so Alembic sees them in metadata
# Adjust these imports if your model module names differ.
from app.models.tenant import Tenant  # noqa: F401, E402
from app.models.child import Child  # noqa: F401, E402
from app.models.therapist import Therapist  # noqa: F401, E402
from app.models.appointment import Appointment  # noqa: F401, E402
from app.models.clinic_settings import ClinicSettings  # noqa: F401, E402
try:
    from app.models.sms_outbox import SmsOutbox  # noqa: F401, E402
except Exception:
    # If your outbox model is named differently, it's OK—Alembic will still run,
    # but you should update this import to match your actual model file.
    pass

try:
    # Licensing models (if present)
    from app.models.licensing import Plan, Subscription, ActivationCode, LicenseAuditLog  # noqa: F401, E402
except Exception:
    pass


config = context.config

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _get_db_url() -> str:
    # Render provides DATABASE_URL via environment variables
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        return url
    # fallback to alembic.ini if needed
    return config.get_main_option("sqlalchemy.url")


def run_migrations_offline() -> None:
    url = _get_db_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _get_db_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

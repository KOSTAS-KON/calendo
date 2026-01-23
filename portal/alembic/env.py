from __future__ import annotations

import os
import sys
import pkgutil
import importlib
from pathlib import Path
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool


# -----------------------------------------------------------------------------
# Ensure the project root is on sys.path so "import app" works in Render/Docker.
#
# Typical container layout for your portal service:
#   /app/
#     app/                 <-- Python package "app"
#     alembic/
#       env.py             <-- this file
#     alembic.ini
#
# In that case, the correct import root is /app (parent of "app" package).
# -----------------------------------------------------------------------------
THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent  # /app (or .../portal in local dev)

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Some environments set cwd differently; also ensure cwd isn't hurting imports
try:
    os.chdir(str(PROJECT_ROOT))
except Exception:
    pass


# -----------------------------------------------------------------------------
# Alembic config and logging
# -----------------------------------------------------------------------------
config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


# -----------------------------------------------------------------------------
# DB URL handling (Render provides DATABASE_URL via environment variables)
# Normalize "postgres://" -> "postgresql://" for SQLAlchemy compatibility.
# -----------------------------------------------------------------------------
def _normalize_db_url(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    return url


def _get_db_url() -> str:
    env_url = _normalize_db_url(os.getenv("DATABASE_URL", ""))
    if env_url:
        return env_url

    ini_url = _normalize_db_url(config.get_main_option("sqlalchemy.url"))
    return ini_url


# -----------------------------------------------------------------------------
# Import Base + auto-import models so Alembic autogenerate sees metadata.
# This avoids brittle "from app.models.x import Y" blocks.
# -----------------------------------------------------------------------------
from app.db import Base  # noqa: E402


def _import_all_models() -> None:
    """
    Import all Python modules under app.models.* so that:
      - SQLAlchemy model classes are registered
      - Base.metadata is complete for autogenerate
    """
    try:
        import app.models  # noqa: F401
        pkg_path = getattr(importlib.import_module("app.models"), "__path__", None)
        if not pkg_path:
            return

        for module_info in pkgutil.walk_packages(pkg_path, prefix="app.models."):
            # Skip private modules if any
            if module_info.name.split(".")[-1].startswith("_"):
                continue
            try:
                importlib.import_module(module_info.name)
            except Exception:
                # Don't crash migrations if one optional module fails to import.
                # You can tighten this later once the codebase stabilizes.
                pass
    except Exception:
        # If models package isn't importable, let Alembic fail with a clear error later.
        pass


_import_all_models()

target_metadata = Base.metadata


# -----------------------------------------------------------------------------
# Offline / online migration runners
# -----------------------------------------------------------------------------
def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = _get_db_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        # If you use schemas, set version_table_schema here.
        # version_table_schema="public",
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
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
            # If you use schemas:
            # include_schemas=True,
            # version_table_schema="public",
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()

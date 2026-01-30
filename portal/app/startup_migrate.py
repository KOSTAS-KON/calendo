from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from sqlalchemy import create_engine, text

# Alembic is a runtime dependency (listed in portal/requirements.txt)
from alembic import command
from alembic.config import Config


def _normalize_db_url(url: str) -> str:
    u = (url or "").strip()
    # SQLAlchemy expects postgresql:// not postgres://
    if u.startswith("postgres://"):
        u = "postgresql://" + u[len("postgres://"):]
    return u


def _should_run() -> bool:
    # Explicit opt-out wins
    if os.getenv("RUN_MIGRATIONS_ON_STARTUP", "").strip() in {"0", "false", "False", "no", "NO"}:
        return False

    # Explicit opt-in
    if os.getenv("RUN_MIGRATIONS_ON_STARTUP", "").strip() in {"1", "true", "True", "yes", "YES"}:
        return True

    # Default behavior:
    # - On Render we DO want migrations to run before any ORM queries, to avoid startup crashes
    # - Locally, it's also safe (idempotent), but you can disable via env var above.
    if os.getenv("RENDER", "") or os.getenv("RENDER_SERVICE_ID", "") or os.getenv("RENDER_SERVICE_NAME", ""):
        return True

    # Default to False outside Render unless explicitly enabled
    return False


def run_migrations() -> None:
    """
    Run Alembic migrations to 'head' using portal/alembic.ini.

    Uses a Postgres advisory lock to avoid concurrent migration races when multiple
    workers/instances start at the same time.
    """
    if not _should_run():
        return

    db_url = _normalize_db_url(os.getenv("DATABASE_URL", ""))
    if not db_url:
        # No DB configured; nothing to migrate
        return

    # Resolve config path relative to this file (portal/app/startup_migrate.py)
    # portal/ is two parents up from app/
    portal_dir = Path(__file__).resolve().parents[1]
    cfg_path = portal_dir / "alembic.ini"
    alembic_dir = portal_dir / "alembic"

    cfg = Config(str(cfg_path))
    # Make sure script_location points to the actual alembic directory
    cfg.set_main_option("script_location", str(alembic_dir))
    cfg.set_main_option("sqlalchemy.url", db_url)

    # Postgres advisory lock
    engine = create_engine(db_url, pool_pre_ping=True)
    lock_id = 934188521  # stable constant for this app
    with engine.connect() as conn:
        try:
            conn.execute(text("SELECT pg_advisory_lock(:id)"), {"id": lock_id})
        except Exception:
            # Not Postgres or lock failed; still try migrations without lock
            pass

        try:
            command.upgrade(cfg, "head")
        finally:
            try:
                conn.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": lock_id})
            except Exception:
                pass

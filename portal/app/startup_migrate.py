from __future__ import annotations

import os
import time
from contextlib import contextmanager

from sqlalchemy import text
from app.db import SessionLocal


@contextmanager
def _db_session():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _env_truthy(name: str, default: str = "0") -> bool:
    return (os.getenv(name, default) or "").strip().lower() in ("1", "true", "yes", "on")


def run_migrations_if_enabled() -> None:
    """
    Safe migration runner.

    - Only runs when RUN_MIGRATIONS_ON_STARTUP=1
    - Uses pg_advisory_lock to avoid concurrent runs
    - If lock isn't acquired quickly, it SKIPS (doesn't crash the app)
    - Never exits the process
    """
    if not _env_truthy("RUN_MIGRATIONS_ON_STARTUP", "0"):
        return

    # Lazy imports so the app can still boot even if alembic isn't installed locally
    try:
        from alembic import command
        from alembic.config import Config
    except Exception as e:
        print(f"[startup_migrate] Alembic not available: {e}. Skipping.")
        return

    # Try to acquire advisory lock (so only one instance migrates)
    lock_key = 88442211  # arbitrary constant
    max_wait_s = int(os.getenv("MIGRATION_LOCK_WAIT_SECONDS", "10"))
    start = time.time()

    acquired = False
    with _db_session() as db:
        while time.time() - start < max_wait_s:
            try:
                acquired = bool(db.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": lock_key}).scalar())
            except Exception as e:
                print(f"[startup_migrate] Lock check failed: {e}. Skipping migrations.")
                return

            if acquired:
                break
            time.sleep(0.5)

        if not acquired:
            print("[startup_migrate] Another process is migrating. Skipping.")
            return

        try:
            cfg = Config("portal/alembic.ini")
            print("[startup_migrate] Running alembic upgrade head...")
            command.upgrade(cfg, "head")
            print("[startup_migrate] Alembic upgrade complete.")
        except Exception as e:
            # DO NOT crash the app. Log and continue.
            print(f"[startup_migrate] Alembic upgrade failed (non-fatal): {e}")
        finally:
            try:
                db.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key})
            except Exception:
                pass


def run_migrations() -> None:
    """
    Backwards-compatible helper used by main.py startup hook.

    IMPORTANT:
    - This intentionally calls the safe, env-gated runner.
    - Set RUN_MIGRATIONS_ON_STARTUP=1 only if your platform does NOT already run migrations.
    - On Render, usually set RUN_MIGRATIONS_ON_STARTUP=0 (avoid double-run).
    """
    run_migrations_if_enabled()

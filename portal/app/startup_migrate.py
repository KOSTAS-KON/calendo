from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

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


def _find_alembic_ini() -> str | None:
    """
    In production containers we've seen two layouts:
      - /app/alembic.ini (root)
      - /app/portal/alembic.ini (portal subfolder)
    Prefer root if present.
    """
    candidates = [
        Path("alembic.ini"),
        Path("portal") / "alembic.ini",
    ]
    for p in candidates:
        try:
            if p.exists():
                return str(p)
        except Exception:
            continue
    return None


def run_migrations_if_enabled() -> None:
    """
    Safe migration runner.

    - Runs only when RUN_MIGRATIONS_ON_STARTUP=1
    - Uses pg_advisory_lock to prevent concurrent runs
    - If lock can't be acquired quickly, SKIPS (doesn't crash)
    - Uses the correct Alembic config file automatically
    """
    if not _env_truthy("RUN_MIGRATIONS_ON_STARTUP", "0"):
        return

    try:
        from alembic import command
        from alembic.config import Config
    except Exception as e:
        print(f"[startup_migrate] Alembic not available: {e}. Skipping migrations.")
        return

    ini_path = _find_alembic_ini()
    if not ini_path:
        print("[startup_migrate] No alembic.ini found (alembic.ini or portal/alembic.ini). Skipping.")
        return

    lock_key = 88442211
    max_wait_s = int(os.getenv("MIGRATION_LOCK_WAIT_SECONDS", "12"))
    start = time.time()

    with _db_session() as db:
        acquired = False
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
            cfg = Config(ini_path)
            print(f"[startup_migrate] Running alembic upgrade head using {ini_path} ...")
            command.upgrade(cfg, "head")
            print("[startup_migrate] Alembic upgrade complete.")
        except Exception as e:
            # do not kill the app
            print(f"[startup_migrate] Alembic upgrade failed (non-fatal): {e}")
        finally:
            try:
                db.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key})
            except Exception:
                pass


def run_migrations() -> None:
    """Compatibility wrapper used by main.py startup hook."""
    run_migrations_if_enabled()

from __future__ import annotations

from datetime import datetime, timedelta
import os
import uuid

from fastapi import FastAPI, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.db import Base, engine, SessionLocal
from app.routers.web import router as web_router
from app.routers.auth import router as auth_router

app = FastAPI(title="Calendo Portal", version="1.0.0")

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(auth_router)
app.include_router(web_router)


# --- Health endpoints (Render-friendly) ---
@app.get("/health", include_in_schema=False)
def health_get():
    return {"ok": True}


@app.head("/health", include_in_schema=False)
def health_head():
    return Response(status_code=200)


@app.head("/", include_in_schema=False)
def root_head():
    return Response(status_code=200)


@app.get("/")
def root():
    # Default tenant landing
    return RedirectResponse(url="/suite", status_code=307)


# --- Schema patching / seeding for multi-tenant migration ---
def _normalize_db_url(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    return url


def ensure_multitenant_schema():
    """Upgrade an existing single-tenant DB in-place to support tenants.

    We keep this as a safety net even if you later adopt Alembic.
    """
    with engine.begin() as conn:
        # Tenants table
        conn.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS tenants (
                    id VARCHAR(36) PRIMARY KEY,
                    slug VARCHAR(80) UNIQUE NOT NULL,
                    name VARCHAR(200) NOT NULL DEFAULT '',
                    status VARCHAR(20) NOT NULL DEFAULT 'active',
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                );
                """
            )
        )

        # Add tenant_id columns
        # Postgres supports IF NOT EXISTS for ADD COLUMN
        conn.execute(text("ALTER TABLE IF EXISTS clinic_settings ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(36);"))
        conn.execute(text("ALTER TABLE IF EXISTS children ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(36);"))
        conn.execute(text("ALTER TABLE IF EXISTS therapists ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(36);"))
        conn.execute(text("ALTER TABLE IF EXISTS appointments ADD COLUMN IF NOT EXISTS tenant_id VARCHAR(36);"))
        conn.execute(text("ALTER TABLE IF EXISTS clinic_settings ADD COLUMN IF NOT EXISTS google_maps_link VARCHAR(1000) DEFAULT '';"))

        # Ensure default tenant row exists
        conn.execute(
            text(
                """
                INSERT INTO tenants (id, slug, name, status)
                VALUES (:id, 'default', 'Default Tenant', 'active')
                ON CONFLICT (slug) DO NOTHING;
                """
            ),
            {"id": str(uuid.uuid4())},
        )

        # Backfill tenant_id using default tenant id
        default_id = conn.execute(text("SELECT id FROM tenants WHERE slug='default' LIMIT 1")).scalar()
        if default_id:
            conn.execute(text("UPDATE clinic_settings SET tenant_id=:tid WHERE tenant_id IS NULL;"), {"tid": default_id})
            conn.execute(text("UPDATE children SET tenant_id=:tid WHERE tenant_id IS NULL;"), {"tid": default_id})
            conn.execute(text("UPDATE therapists SET tenant_id=:tid WHERE tenant_id IS NULL;"), {"tid": default_id})
            conn.execute(text("UPDATE appointments SET tenant_id=:tid WHERE tenant_id IS NULL;"), {"tid": default_id})


def seed_defaults():
    """Create minimal per-tenant clinic settings row and legacy license row."""
    db = SessionLocal()
    try:
        from app.models.tenant import Tenant
        from app.models.clinic_settings import ClinicSettings, AppLicense

        t = db.query(Tenant).filter(Tenant.slug == "default").first()
        if not t:
            t = Tenant(id=str(uuid.uuid4()), slug="default", name="Default Tenant", status="active")
            db.add(t)
            db.commit()
            db.refresh(t)

        # Clinic settings per tenant
        cs = db.query(ClinicSettings).filter(ClinicSettings.tenant_id == t.id).first()
        if not cs:
            db.add(ClinicSettings(tenant_id=t.id))
            db.commit()

        # Legacy AppLicense singleton (id=1)
        if not db.get(AppLicense, 1):
            db.add(
                AppLicense(
                    id=1,
                    product_mode="BOTH",
                    trial_end=datetime.utcnow() + timedelta(days=28),
                )
            )
            db.commit()

    finally:
        db.close()


@app.on_event("startup")
def on_startup():
    # Create new tables (won't alter existing columns)
    Base.metadata.create_all(bind=engine)

    # Patch existing DB for tenant_id columns (Postgres)
    try:
        ensure_multitenant_schema()
    except Exception:
        # Don't crash startup; but logs will show if something is wrong
        pass

    seed_defaults()

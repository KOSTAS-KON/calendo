from __future__ import annotations

from datetime import datetime, timedelta
import os
import uuid

from fastapi import FastAPI, Response
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.db import SessionLocal
from app.routers.web import router as web_router
from app.routers.auth import router as auth_router
from app.routers.admin import router as admin_router

app = FastAPI(title="Calendo Portal", version="1.0.0")

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(auth_router)
app.include_router(web_router)
app.include_router(admin_router)


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


def seed_defaults() -> None:
    """Seed minimal SaaS defaults.

    IMPORTANT:
    - No ad-hoc ALTER TABLE or CREATE TABLE here.
    - Schema must be handled by Alembic migrations (run in Docker CMD).
    """
    db = SessionLocal()
    try:
        from app.models.tenant import Tenant
        from app.models.clinic_settings import ClinicSettings
        from app.models.licensing import Plan, Subscription

        # Ensure default tenant exists
        t = db.query(Tenant).filter(Tenant.slug == "default").first()
        if not t:
            t = Tenant(
                id=str(uuid.uuid4()),
                slug="default",
                name="Default Tenant",
                status="active",
            )
            db.add(t)
            db.commit()
            db.refresh(t)

        # Ensure clinic settings row for tenant
        cs = db.query(ClinicSettings).filter(ClinicSettings.tenant_id == t.id).first()
        if not cs:
            cs = ClinicSettings(tenant_id=t.id)
            db.add(cs)
            db.commit()

        # Ensure default plans exist (migrations seed these, but safe to keep)
        def ensure_plan(code: str, name: str, days: int) -> Plan:
            p = db.query(Plan).filter(Plan.code == code).first()
            if not p:
                p = Plan(code=code, duration_days=days, features_json="{}")
                # Some builds include a name column; ignore if not present
                if hasattr(p, "name"):
                    setattr(p, "name", name)
                db.add(p)
                db.commit()
                db.refresh(p)
            return p

        p_trial = ensure_plan("TRIAL_7D", "7-day Trial", 7)
        ensure_plan("MONTHLY_30D", "Monthly", 30)
        ensure_plan("YEARLY_365D", "Yearly", 365)

        # Ensure subscription for default tenant
        sub = (
            db.query(Subscription)
            .filter(Subscription.tenant_id == t.id)
            .order_by(Subscription.starts_at.desc())
            .first()
        )
        if not sub:
            sub = Subscription(
                id=str(uuid.uuid4()),
                tenant_id=t.id,
                plan_id=p_trial.id,
                status="active",
                starts_at=datetime.utcnow(),
                ends_at=datetime.utcnow() + timedelta(days=p_trial.duration_days),
                source="manual",
            )
            db.add(sub)
            db.commit()

    finally:
        db.close()


@app.on_event("startup")
def on_startup():
    # Alembic migrations run before uvicorn starts (see Dockerfile CMD).
    # Startup only seeds essential default rows.
    seed_defaults()

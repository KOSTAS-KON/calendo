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
from app.routers.admin import router as admin_router

from app.models.child import Child
from app.models.appointment import Appointment
from app.models.clinic_settings import ClinicSettings, AppLicense


app = FastAPI(title="Therapy Archive Portal", version="0.3.0")

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(auth_router)
app.include_router(web_router)
app.include_router(admin_router)


# Compatibility routes: older deployments and some links may use /therapy
@app.get("/therapy")
@app.get("/therapy/")
def therapy_root():
    return RedirectResponse(url="/suite", status_code=307)


def ensure_schema():
    """
    Lightweight schema patching for legacy DBs.
    In SaaS mode you should rely on Alembic, but this remains a safe fallback.
    """
    try:
        with engine.begin() as conn:
            # clinic_settings: provider + optional Infobip fields
            conn.execute(
                text(
                    "ALTER TABLE clinic_settings "
                    "ADD COLUMN IF NOT EXISTS sms_provider VARCHAR(50) DEFAULT 'infobip'"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE clinic_settings "
                    "ADD COLUMN IF NOT EXISTS infobip_username VARCHAR(200) DEFAULT ''"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE clinic_settings "
                    "ADD COLUMN IF NOT EXISTS infobip_userkey VARCHAR(300) DEFAULT ''"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE clinic_settings "
                    "ADD COLUMN IF NOT EXISTS google_maps_link VARCHAR(1000) DEFAULT ''"
                )
            )
    except Exception:
        pass


def seed_saas_defaults():
    """
    Seed default tenant + default plans + a trial subscription.
    Safe to call repeatedly. Does NOT crash if licensing models aren't present.
    """
    db = SessionLocal()
    try:
        from app.models.tenant import Tenant
        from app.models.licensing import Plan, Subscription

        # Default tenant
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

        def ensure_plan(code: str, name: str, days: int) -> Plan:
            p = db.query(Plan).filter(Plan.code == code).first()
            if not p:
                p = Plan(code=code, name=name, duration_days=days, features_json="{}")
                db.add(p)
                db.commit()
                db.refresh(p)
            return p

        p_trial = ensure_plan("TRIAL_7D", "7-day Trial", 7)
        ensure_plan("MONTHLY_30D", "Monthly", 30)
        ensure_plan("YEARLY_365D", "Yearly", 365)

        # Ensure a subscription exists for default tenant
        sub = (
            db.query(Subscription)
            .filter(Subscription.tenant_id == t.id)
            .order_by(Subscription.starts_at.desc())
            .first()
        )
        if not sub:
            db.add(
                Subscription(
                    id=str(uuid.uuid4()),
                    tenant_id=t.id,
                    plan_id=p_trial.id,
                    status="active",
                    starts_at=datetime.utcnow(),
                    ends_at=datetime.utcnow() + timedelta(days=p_trial.duration_days),
                    source="manual",
                )
            )
            db.commit()

        # Ensure tenant clinic settings row exists
        cs = db.query(ClinicSettings).filter(ClinicSettings.tenant_id == t.id).first()
        if not cs:
            db.add(ClinicSettings(tenant_id=t.id))
            db.commit()

    except Exception:
        pass
    finally:
        db.close()


def seed_legacy_singletons():
    """
    Legacy single-tenant support:
    keep minimal singleton rows for older UI parts that still read AppLicense id=1.
    """
    db = SessionLocal()
    try:
        if not db.get(AppLicense, 1):
            db.add(
                AppLicense(
                    id=1,
                    product_mode="BOTH",
                    trial_end=datetime.utcnow() + timedelta(weeks=4),
                )
            )
            db.commit()

        # Optional sample content for legacy demo/testing
        if os.getenv("PORTAL_SEED_SAMPLE", "0") != "1":
            return

        if db.query(Child).count() > 0:
            return

        child1 = Child(full_name="Sample Child", notes="")
        db.add(child1)
        db.commit()

        now = datetime.now().replace(second=0, microsecond=0)
        appt = Appointment(
            child_id=child1.id,
            starts_at=now + timedelta(days=1, hours=10),
            ends_at=now + timedelta(days=1, hours=11),
            therapist_name="",
            procedure="Session",
            attendance_status="scheduled",
        )
        db.add(appt)
        db.commit()

    finally:
        db.close()


# --- Render-friendly health checks (avoid HEAD 405) ---
@app.get("/health", include_in_schema=False)
def health_get():
    return {"ok": True}


@app.head("/health", include_in_schema=False)
def health_head():
    return Response(status_code=200)


@app.head("/", include_in_schema=False)
def root_head():
    return Response(status_code=200)


@app.on_event("startup")
def on_startup():
    # For non-alembic legacy installs
    Base.metadata.create_all(bind=engine)

    ensure_schema()

    # Seeds for new SaaS (multi-tenant)
    seed_saas_defaults()

    # Back-compat legacy singletons
    seed_legacy_singletons()

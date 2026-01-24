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


# Compatibility routes: older deployments and the SMS app may link to /therapy/
@app.get("/therapy")
@app.get("/therapy/")
def therapy_root():
    return RedirectResponse(url="/suite", status_code=307)


def ensure_schema():
    """Lightweight schema patching for older client DBs.

    NOTE: In the new Alembic-based SaaS path, this becomes less necessary.
    Keeping it as a safety net for legacy installs / older DBs.
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

            # app_license: activation fields
            conn.execute(
                text(
                    "ALTER TABLE app_license "
                    "ADD COLUMN IF NOT EXISTS client_id VARCHAR(120) DEFAULT ''"
                )
            )
            conn.execute(
                text(
                    "ALTER TABLE app_license "
                    "ADD COLUMN IF NOT EXISTS activation_token VARCHAR(2000) DEFAULT ''"
                )
            )
            conn.execute(text("ALTER TABLE app_license ADD COLUMN IF NOT EXISTS plan INTEGER DEFAULT 0"))
            conn.execute(text("ALTER TABLE app_license ADD COLUMN IF NOT EXISTS activated_at TIMESTAMP"))

            # children: parent/guardian fields
            conn.execute(text("ALTER TABLE children ADD COLUMN IF NOT EXISTS parent1_name VARCHAR(200)"))
            conn.execute(text("ALTER TABLE children ADD COLUMN IF NOT EXISTS parent1_phone VARCHAR(80)"))
            conn.execute(text("ALTER TABLE children ADD COLUMN IF NOT EXISTS parent2_name VARCHAR(200)"))
            conn.execute(text("ALTER TABLE children ADD COLUMN IF NOT EXISTS parent2_phone VARCHAR(80)"))

    except Exception:
        # If DB isn't ready yet, create_all will handle fresh installs.
        pass


def seed_saas_defaults():
    """Seed default tenant + plans + a trial subscription (multi-tenant SaaS).

    Safe to call repeatedly.
    """
    db = SessionLocal()
    try:
        # Import inside to avoid circular imports at app import-time
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

    except Exception:
        # If licensing models aren't present in some older build, don't crash startup.
        pass
    finally:
        db.close()


def seed_if_empty():
    """Ensure required singleton rows exist (legacy single-tenant mode support).

    By default, the database starts empty for production use.
    If you want to load sample content for training/testing, set:
        PORTAL_SEED_SAMPLE=1
    """
    db = SessionLocal()
    try:
        # Ensure singleton settings rows exist (legacy pattern)
        if not db.get(ClinicSettings, 1):
            db.add(ClinicSettings(id=1))
            db.commit()

        if not db.get(AppLicense, 1):
            # Default: 4-week trial, BOTH apps enabled
            db.add(
                AppLicense(
                    id=1,
                    product_mode="BOTH",
                    trial_end=datetime.utcnow() + timedelta(weeks=4),
                )
            )
            db.commit()

        # Optional sample content
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
    # Create tables for any non-Alembic legacy installs
    Base.metadata.create_all(bind=engine)

    # Backward-compatible patching for older DBs
    ensure_schema()

    # Legacy seed (single-tenant-ish)
    seed_if_empty()

    # SaaS seed (multi-tenant + licensing)
    seed_saas_defaults()

from datetime import datetime, timedelta, date

from fastapi import FastAPI, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.db import Base, engine, SessionLocal
from app.routers.web import router as web_router
from app.routers.auth import router as auth_router

from app.models.child import Child
from app.models.appointment import Appointment
from app.models.billing import BillingItem
from app.models.billing_plan import BillingPlan
from app.models.timeline import TimelineEvent
from app.models.clinic_settings import ClinicSettings, AppLicense
from app.models.therapist import Therapist

app = FastAPI(title="Therapy Archive Portal", version="0.3.0")

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(auth_router)
app.include_router(web_router)


def ensure_schema():
    """Lightweight schema patching for packaged deployments.

    We avoid Alembic for this packaged deployment. For existing client databases,
    we add any missing columns safely.
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
            conn.execute(
                text("ALTER TABLE app_license ADD COLUMN IF NOT EXISTS plan INTEGER DEFAULT 0")
            )
            conn.execute(
                text("ALTER TABLE app_license ADD COLUMN IF NOT EXISTS activated_at TIMESTAMP")
            )

            # children: parent/guardian fields
            conn.execute(
                text("ALTER TABLE children ADD COLUMN IF NOT EXISTS parent1_name VARCHAR(200)")
            )
            conn.execute(
                text("ALTER TABLE children ADD COLUMN IF NOT EXISTS parent1_phone VARCHAR(80)")
            )
            conn.execute(
                text("ALTER TABLE children ADD COLUMN IF NOT EXISTS parent2_name VARCHAR(200)")
            )
            conn.execute(
                text("ALTER TABLE children ADD COLUMN IF NOT EXISTS parent2_phone VARCHAR(80)")
            )

    except Exception:
        # If DB isn't ready yet, create_all will handle fresh installs.
        pass


def seed_if_empty():
    """Ensure required singleton rows exist.

    By default, the database starts empty for production use.
    If you want to load sample content for training/testing, set:
        PORTAL_SEED_SAMPLE=1
    """
    db = SessionLocal()
    try:
        # Ensure singleton settings rows exist
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
        import os

        if os.getenv("PORTAL_SEED_SAMPLE", "0") != "1":
            return

        if db.query(Child).count() > 0:
            return

        # Minimal sample data (can be removed entirely by leaving PORTAL_SEED_SAMPLE unset)
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


# Optional but recommended: Render sometimes probes HEAD /
@app.head("/", include_in_schema=False)
def root_head():
    return Response(status_code=200)


@app.on_event("startup")
def on_startup():
    Base.metadata.create_all(bind=engine)
    ensure_schema()
    seed_if_empty()

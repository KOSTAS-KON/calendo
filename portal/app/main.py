from __future__ import annotations

from datetime import datetime, timedelta
import uuid
import os
import hashlib
from urllib.parse import quote, unquote

from fastapi import FastAPI, Request, Response, Form
from fastapi.responses import RedirectResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

import bcrypt

# Optional: Sentry
try:
    import sentry_sdk
except Exception:
    sentry_sdk = None

from sqlalchemy.exc import ProgrammingError

from app.db import SessionLocal
from app.startup_migrate import run_migrations  # env-gated safe runner
from app.routers.web import router as web_router
from app.routers.auth import router as auth_router
from app.routers.admin import router as admin_router
from app.api.public_alias import router as public_alias_router
from app.dev_seed import ensure_test_users


app = FastAPI(title="Calendo Portal", version="1.0.0")

# Routers + static
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# Optional deterministic test users (ENABLE_TEST_USERS=1)
ensure_test_users()

# Routers
app.include_router(auth_router)
app.include_router(web_router)
app.include_router(admin_router)
app.include_router(public_alias_router)

# ✅ DO NOT run migrations at import-time (causes double-run on Render)
# ✅ We run them in startup hook only (and only if RUN_MIGRATIONS_ON_STARTUP=1)


# Sentry initialization (optional)
_SENTRY_DSN = (os.getenv("SENTRY_DSN") or "").strip()
if sentry_sdk and _SENTRY_DSN:
    sentry_sdk.init(
        dsn=_SENTRY_DSN,
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE") or "0.05"),
        environment=os.getenv("SENTRY_ENV") or os.getenv("RENDER_SERVICE_NAME") or "production",
    )


# ----------------------------
# Health endpoints
# ----------------------------
@app.get("/health", include_in_schema=False)
def health_get():
    return {"ok": True}


@app.head("/health", include_in_schema=False)
def health_head():
    return Response(status_code=200)


@app.head("/", include_in_schema=False)
def root_head():
    return Response(status_code=200)


# ----------------------------
# Helpers
# ----------------------------
def _safe_next(next_path: str) -> str:
    if not next_path:
        return "/"
    try:
        nxt = unquote(next_path)
    except Exception:
        nxt = next_path
    if not nxt.startswith("/"):
        return "/"
    if nxt.startswith("//"):
        return "/"
    return nxt


def _sess(request: Request) -> dict:
    s = request.scope.get("session")
    return s if isinstance(s, dict) else {}


def _set_sess(request: Request, key: str, value) -> None:
    s = request.scope.get("session")
    if isinstance(s, dict):
        s[key] = value


@app.get("/me")
def me(request: Request):
    s = _sess(request)
    return {
        "user_id": s.get("user_id"),
        "email": s.get("email"),
        "role": s.get("role"),
        "tenant_slug": s.get("tenant_slug"),
        "tenant_id": s.get("tenant_id"),
        "subscription_until": s.get("subscription_until"),
        "has_session": "session" in request.scope,
    }


def _logged_in(request: Request) -> bool:
    return bool(_sess(request).get("user_id"))


def _user_role(request: Request) -> str:
    return str(_sess(request).get("role") or "").lower()


def _extract_tenant_slug(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "t":
        return parts[1]
    return "default"


def _session_tenant_slug(request: Request) -> str:
    return str(_sess(request).get("tenant_slug") or "default")


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _format_iso_utc(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat() + "Z"


def _is_missing_tenant_archive_columns(err: Exception) -> bool:
    """
    Detect the specific failure you keep seeing:
      psycopg2.errors.UndefinedColumn: column tenants.is_archived does not exist
    """
    msg = str(err).lower()
    return ("undefinedcolumn" in msg and "tenants.is_archived" in msg) or (
        "does not exist" in msg and "tenants.is_archived" in msg
    )


# ... keep ALL your existing code unchanged here ...
# TenantGateMiddleware, routes, etc.


# ----------------------------
# Seed defaults + bootstrap owner
# ----------------------------
def seed_defaults() -> None:
    """
    Seed core data safely.

    Production behavior:
    - If DB schema is behind (missing tenants.is_archived), try migrations and retry once.
    - If still behind, DO NOT crash the whole service: log a warning and return.
      (This prevents Render restart loops while you fix migrations.)
    """
    db = SessionLocal()
    try:
        from app.models.tenant import Tenant
        from app.models.clinic_settings import ClinicSettings
        from app.models.licensing import Plan, Subscription
        from app.models.user import User

        # 1) Load default tenant (may crash if DB schema behind)
        try:
            t = db.query(Tenant).filter(Tenant.slug == "default").first()
        except ProgrammingError as e:
            if _is_missing_tenant_archive_columns(e):
                print("[seed_defaults] Missing tenants.is_archived; attempting migrations then retrying once...")
                run_migrations()
                try:
                    t = db.query(Tenant).filter(Tenant.slug == "default").first()
                except ProgrammingError as e2:
                    if _is_missing_tenant_archive_columns(e2):
                        print(
                            "[seed_defaults] WARNING: tenants archive columns still missing after migration attempt. "
                            "Service will start but seed_defaults is skipped. Run Alembic upgrade head."
                        )
                        return
                    raise
            else:
                raise

        # 2) Create tenant if not exists (safe)
        if not t:
            t = Tenant(id=str(uuid.uuid4()), slug="default", name="Default Tenant", status="active")
            if hasattr(t, "created_at"):
                t.created_at = datetime.utcnow()
            db.add(t)
            db.commit()
            db.refresh(t)

        # 3) Ensure clinic_settings exists
        cs = db.query(ClinicSettings).filter(ClinicSettings.tenant_id == t.id).first()
        if not cs:
            db.add(ClinicSettings(tenant_id=t.id))
            db.commit()

        # 4) Ensure plans exist
        def ensure_plan(code: str, name: str, days: int) -> Plan:
            p = db.query(Plan).filter(Plan.code == code).first()
            if not p:
                p = Plan(code=code, name=name, duration_days=days, features_json="{}")
                db.add(p)
                db.commit()
                db.refresh(p)
            return p

        p_trial = ensure_plan("TRIAL_7D", "7-day Trial", 7)
        ensure_plan("MONTHLY_30D", "Monthly (30 days)", 30)
        ensure_plan("YEARLY_365D", "Yearly (365 days)", 365)

        # 5) Ensure subscription exists
        sub = (
            db.query(Subscription)
            .filter(Subscription.tenant_id == t.id)
            .order_by(Subscription.ends_at.desc())
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
                    ends_at=datetime.utcnow() + timedelta(days=int(getattr(p_trial, "duration_days", 7) or 7)),
                    source="manual",
                )
            )
            db.commit()

        # 6) Optional bootstrap owner via env
        email = (os.getenv("BOOTSTRAP_OWNER_EMAIL") or "").strip().lower()
        pw = (os.getenv("BOOTSTRAP_OWNER_PASSWORD") or "").strip()
        if email and pw:
            u = db.query(User).filter(User.tenant_id == t.id, User.email == email).first()
            if not u:
                pw_hash = bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
                db.add(
                    User(
                        id=str(uuid.uuid4()),
                        tenant_id=t.id,
                        email=email,
                        password_hash=pw_hash,
                        role="owner",
                        is_active=True,
                        must_reset_password=True,
                    )
                )
                db.commit()
                print(f"BOOTSTRAP: created owner {email} for tenant {t.slug} (must reset password on first login)")
                print(
                    "BOOTSTRAP: IMPORTANT: remove BOOTSTRAP_OWNER_EMAIL and BOOTSTRAP_OWNER_PASSWORD from env after first login."
                )

    finally:
        db.close()


@app.on_event("startup")
def on_startup():
    # ✅ Attempt migrations first (env-gated; set RUN_MIGRATIONS_ON_STARTUP=0 on Render if entrypoint already migrates)
    run_migrations()

    # ✅ Seed (won't crash the whole service if DB is behind)
    seed_defaults()

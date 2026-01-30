from __future__ import annotations

from datetime import datetime, timedelta
import uuid
import os
import hashlib
from urllib.parse import unquote

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

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

from fastapi.responses import HTMLResponse, RedirectResponse


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def landing():
    # Simple production-safe landing:
    # - shows links to login and admin
    # - does not require DB schema to be up-to-date
    return HTMLResponse(
        """
        <!doctype html>
        <html>
          <head>
            <meta charset="utf-8"/>
            <meta name="viewport" content="width=device-width, initial-scale=1"/>
            <title>Calendo</title>
            <style>
              body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial; margin: 0; padding: 28px; background: #0b1220; color: #e5e7eb; }
              a { color: #60a5fa; text-decoration: none; }
              .card { max-width: 720px; margin: 0 auto; background: rgba(255,255,255,.06); border: 1px solid rgba(255,255,255,.10);
                      border-radius: 14px; padding: 18px; }
              .btn { display:inline-block; margin-top: 10px; padding: 10px 14px; border-radius: 10px; background: rgba(96,165,250,.18);
                     border: 1px solid rgba(96,165,250,.35); }
              .muted { color: rgba(229,231,235,.75); }
              code { background: rgba(255,255,255,.08); padding: 2px 6px; border-radius: 6px; }
            </style>
          </head>
          <body>
            <div class="card">
              <h2 style="margin:0 0 6px 0;">Calendo Portal</h2>
              <div class="muted">Multi-tenant clinic scheduling, billing, and SMS reminders.</div>

              <div style="margin-top:14px;">
                <div><a class="btn" href="/login">Log in</a></div>
                <div style="margin-top:10px;"><a class="btn" href="/admin">Admin</a></div>
              </div>

              <hr style="border:none;border-top:1px solid rgba(255,255,255,.12); margin:16px 0;"/>

              <div class="muted">
                Tenant login example:<br/>
                <code>/login?next=/t/default/suite</code>
              </div>
            </div>
          </body>
        </html>
        """
    )


@app.head("/", include_in_schema=False)
def root_head():
    return Response(status_code=200)


# Static
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# ----------------------------
# Sessions (CRITICAL for Admin key + logins)
# ----------------------------
SESSION_SECRET = (os.getenv("SESSION_SECRET") or "").strip()
if not SESSION_SECRET:
    # Dev fallback only. On Render you MUST set SESSION_SECRET.
    SESSION_SECRET = "dev-only-change-me-please"

HTTPS_ONLY = (os.getenv("HTTPS_ONLY") or "1").strip().lower() in ("1", "true", "yes", "on")

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site="lax",
    https_only=HTTPS_ONLY,
)

# Optional deterministic test users (ENABLE_TEST_USERS=1)
ensure_test_users()

# Routers
app.include_router(auth_router)
app.include_router(web_router)
app.include_router(admin_router)
app.include_router(public_alias_router)

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
def _sess(request: Request) -> dict:
    s = request.scope.get("session")
    return s if isinstance(s, dict) else {}


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


def _is_missing_tenant_archive_columns(err: Exception) -> bool:
    msg = str(err).lower()
    return ("undefinedcolumn" in msg and "tenants.is_archived" in msg) or (
        "does not exist" in msg and "tenants.is_archived" in msg
    )


# ----------------------------
# Seed defaults + bootstrap owner
# ----------------------------
def seed_defaults() -> None:
    """
    Seed core data safely.

    - If DB schema is behind (missing tenants.is_archived), rollback, try migrations, then retry ONCE
      using a fresh DB session (avoids InFailedSqlTransaction).
    - If still behind, DO NOT crash the service: log a warning and return.
    """
    db = SessionLocal()
    try:
        from app.models.tenant import Tenant
        from app.models.clinic_settings import ClinicSettings
        from app.models.licensing import Plan, Subscription
        from app.models.user import User

        def _load_default_tenant(session):
            return session.query(Tenant).filter(Tenant.slug == "default").first()

        # 1) Load default tenant (may fail if DB behind)
        try:
            t = _load_default_tenant(db)
        except ProgrammingError as e:
            if not _is_missing_tenant_archive_columns(e):
                raise

            print("[seed_defaults] Missing tenants.is_archived; rollback + attempt migrations then retry once...")
            try:
                db.rollback()
            except Exception:
                pass

            run_migrations()

            # Retry in a fresh session
            try:
                db.close()
            except Exception:
                pass
            db = SessionLocal()

            try:
                t = _load_default_tenant(db)
            except ProgrammingError as e2:
                if _is_missing_tenant_archive_columns(e2):
                    print(
                        "[seed_defaults] WARNING: tenants archive columns still missing. "
                        "Service will start but seeding is skipped. Run 'alembic upgrade head'."
                    )
                    try:
                        db.rollback()
                    except Exception:
                        pass
                    return
                raise

        # 2) Create tenant if not exists
        if not t:
            t = Tenant(id=str(uuid.uuid4()), slug="default", name="Default Tenant", status="active")
            if hasattr(t, "created_at"):
                t.created_at = datetime.utcnow()
            db.add(t)
            db.commit()
            db.refresh(t)

        # 3) Ensure clinic settings exists
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
                print(f"[seed_defaults] BOOTSTRAP: created owner {email} (must reset on first login).")

    finally:
        try:
            db.close()
        except Exception:
            pass


@app.on_event("startup")
def on_startup():
    # Attempt migrations first (env-gated; keep RUN_MIGRATIONS_ON_STARTUP=0 on Render if entrypoint migrates)
    run_migrations()
    # Seed safely
    seed_defaults()

from __future__ import annotations

from datetime import datetime, timedelta
import uuid
import os

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

import bcrypt
import sqlalchemy as sa
from sqlalchemy.exc import ProgrammingError

# Optional: Sentry
try:
    import sentry_sdk
except Exception:
    sentry_sdk = None

from app.db import SessionLocal
from app.startup_migrate import run_migrations  # env-gated safe runner
from app.routers.web import router as web_router
from app.routers.auth import router as auth_router
from app.routers.admin import router as admin_router
from app.api.public_alias import router as public_alias_router
from app.dev_seed import ensure_test_users
from app.config import settings


app = FastAPI(title="Calendo Portal", version="1.0.0")


# ----------------------------
# Security middleware
# ----------------------------
def _parse_allowed_hosts() -> list[str]:
    raw = (settings.ALLOWED_HOSTS or "").strip()
    if not raw:
        # Render / local dev: allow anything unless configured
        return ["*"]
    return [h.strip() for h in raw.split(",") if h.strip()]


ALLOWED_HOSTS_LIST = _parse_allowed_hosts()
if ALLOWED_HOSTS_LIST != ["*"]:
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=ALLOWED_HOSTS_LIST)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        # Basic hardening headers
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")

        # CSP tuned for built-in templates + inline CSS.
        # IMPORTANT: Turnstile requires:
        #   - loading JS from challenges.cloudflare.com
        #   - iframe from challenges.cloudflare.com
        #   - sometimes XHR/fetch to challenges.cloudflare.com
        csp = (
            "default-src 'self'; "
            "img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline' https://challenges.cloudflare.com; "
            "connect-src 'self' https://challenges.cloudflare.com; "
            "frame-src https://challenges.cloudflare.com; "
            "font-src 'self' data:; "
            "frame-ancestors 'none'; "
            "base-uri 'self'; "
            "form-action 'self'"
        )
        response.headers.setdefault("Content-Security-Policy", csp)

        # HSTS only when served over HTTPS (Render terminates TLS, but forwards proto)
        xf_proto = (request.headers.get("x-forwarded-proto") or "").lower()
        if xf_proto == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")

        return response


app.add_middleware(SecurityHeadersMiddleware)


# ----------------------------
# Landing
# ----------------------------
@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def landing():
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


# ----------------------------
# Static + sessions
# ----------------------------
app.mount("/static", StaticFiles(directory="app/static"), name="static")


SESSION_SECRET = (os.getenv("SESSION_SECRET") or "").strip() or (settings.SSO_SHARED_SECRET or settings.SECRET_KEY)
HTTPS_ONLY = bool(settings.COOKIE_SECURE)


def _is_weak_secret(value: str) -> bool:
    v = (value or "").strip().lower()
    return (not v) or v in {"change-me", "dev-only-change-me-please"} or len(v) < 32


# Refuse to start with weak secrets unless explicitly allowed (DEV only).
ALLOW_WEAK = (os.getenv("ALLOW_WEAK_SECRETS") or "").strip().lower() in ("1", "true", "yes", "on")
if _is_weak_secret(SESSION_SECRET) and not ALLOW_WEAK:
    raise RuntimeError("Weak SESSION_SECRET/SECRET_KEY detected. Set a strong SECRET_KEY (>=32 chars) in production.")

app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site=str(settings.COOKIE_SAMESITE or "lax").lower(),
    https_only=HTTPS_ONLY,
    max_age=int(settings.SESSION_MAX_AGE_SECONDS),
)

# Optional deterministic test users (ENABLE_TEST_USERS=1)
ensure_test_users()

# Routers
app.include_router(auth_router)
app.include_router(web_router)
app.include_router(admin_router)
app.include_router(public_alias_router)

# Optional Sentry
_SENTRY_DSN = (os.getenv("SENTRY_DSN") or "").strip()
if sentry_sdk and _SENTRY_DSN:
    sentry_sdk.init(
        dsn=_SENTRY_DSN,
        traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE") or "0.05"),
        environment=os.getenv("SENTRY_ENV") or os.getenv("RENDER_SERVICE_NAME") or "production",
    )


# ----------------------------
# Health
# ----------------------------
@app.get("/health", include_in_schema=False)
def health_get():
    return {"ok": True}


@app.head("/health", include_in_schema=False)
def health_head():
    return Response(status_code=200)


# ----------------------------
# Helpers
# ----------------------------
def _is_missing_tenant_archive_columns(err: Exception) -> bool:
    msg = str(err).lower()
    return ("undefinedcolumn" in msg and "tenants.is_archived" in msg) or (
        "does not exist" in msg and "tenants.is_archived" in msg
    )


def ensure_tenant_lifecycle_columns() -> None:
    """
    Ensure tenant lifecycle columns exist so ORM queries on Tenant never crash.

    Idempotent: uses Postgres ADD COLUMN IF NOT EXISTS.
    Safe to run on every startup.
    """
    db = SessionLocal()
    try:
        db.execute(sa.text("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS is_archived BOOLEAN NOT NULL DEFAULT FALSE;"))
        db.execute(sa.text("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP NULL;"))
        db.execute(sa.text("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP NULL;"))
        db.execute(sa.text("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS deleted_by VARCHAR(255) NULL;"))
        try:
            db.execute(sa.text("ALTER TABLE tenants ALTER COLUMN is_archived DROP DEFAULT;"))
        except Exception:
            pass
        db.commit()
    except Exception as e:
        db.rollback()
        print(f"[startup] WARNING: could not ensure tenant lifecycle columns: {e}")
    finally:
        db.close()


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

        if not t:
            t = Tenant(id=str(uuid.uuid4()), slug="default", name="Default Tenant", status="active")
            if hasattr(t, "created_at"):
                t.created_at = datetime.utcnow()
            db.add(t)
            db.commit()
            db.refresh(t)

        cs = db.query(ClinicSettings).filter(ClinicSettings.tenant_id == t.id).first()
        if not cs:
            db.add(ClinicSettings(tenant_id=t.id))
            db.commit()

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
    # env-gated migrations (optional)
    run_migrations()

    # ALWAYS ensure lifecycle columns exist (prevents login/admin crashes)
    ensure_tenant_lifecycle_columns()

    # seed safely (won't crash the app)
    seed_defaults()

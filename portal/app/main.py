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


from app.db import SessionLocal
from app.startup_migrate import run_migrations
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
    # SAFE: never triggers SessionMiddleware assertion
    s = request.scope.get("session")
    return s if isinstance(s, dict) else {}


def _set_sess(request: Request, key: str, value) -> None:
    s = request.scope.get("session")
    if isinstance(s, dict):
        s[key] = value


@app.get("/me")
def me(request: Request):
    """Debug endpoint: confirms session identity after login."""
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
    # Render-friendly display
    return dt.replace(microsecond=0).isoformat() + "Z"


def _get_subscription_until(tenant_slug: str) -> str | None:
    """
    Returns ISO string for subscription end date if subscription exists, else None.
    """
    db = SessionLocal()
    try:
        from app.models.tenant import Tenant
        from app.models.licensing import Subscription

        t = db.query(Tenant).filter(Tenant.slug == tenant_slug).first()
        if not t:
            return None

        sub = (
            db.query(Subscription)
            .filter(Subscription.tenant_id == t.id)
            .order_by(Subscription.ends_at.desc())
            .first()
        )
        if not sub or not getattr(sub, "ends_at", None):
            return None

        return _format_iso_utc(sub.ends_at)
    finally:
        db.close()


def _subscription_active(tenant_slug: str) -> bool:
    db = SessionLocal()
    try:
        from app.models.tenant import Tenant
        from app.models.licensing import Subscription

        t = db.query(Tenant).filter(Tenant.slug == tenant_slug).first()
        if not t:
            return False

        sub = (
            db.query(Subscription)
            .filter(Subscription.tenant_id == t.id)
            .order_by(Subscription.ends_at.desc())
            .first()
        )
        if not sub:
            return False
        if getattr(sub, "status", None) and str(sub.status).lower() in ("canceled", "expired"):
            return False
        return bool(getattr(sub, "ends_at", None) and sub.ends_at > datetime.utcnow())
    finally:
        db.close()


# Handy endpoint to display subscription label anywhere (suite JS/template can use this)
@app.get("/subscription", response_class=JSONResponse)
def subscription_status(request: Request, tenant: str = "default"):
    tenant_slug = (tenant or "default").strip().lower()
    until = _get_subscription_until(tenant_slug)
    return {"tenant": tenant_slug, "until": until, "active": bool(until and _subscription_active(tenant_slug))}


# ----------------------------
# Landing
# ----------------------------
@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    default_tenant = "default"
    next_url = f"/t/{default_tenant}/suite"
    login_url = f"/auth/login?next={quote(next_url)}"

    html = f"""
    <!doctype html>
    <html>
      <head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
        <title>Clinic Suite</title>
      </head>
      <body style="font-family:system-ui;background:#0b1220;color:#e5e7eb;margin:0;padding:40px;">
        <h2>Clinic Suite</h2>
        <p><a href="{login_url}" style="color:#60a5fa;">Log in</a></p>
        <p><a href="/me" style="color:#60a5fa;">Session</a></p>
      </body>
    </html>
    """
    return HTMLResponse(html)


# ----------------------------
# Activation redemption (REAL) + Success banner
# ----------------------------
_ERROR_MESSAGES = {
    "missing": "Please enter an activation code.",
    "invalid": "Invalid activation code.",
    "wrong_tenant": "This activation code belongs to a different tenant.",
    "revoked": "This activation code has been revoked.",
    "expired": "This activation code has expired.",
    "used": "This activation code has already been used.",
    "plan_missing": "The plan for this activation code could not be found.",
    "tenant_missing": "Tenant not found.",
    "internal": "Activation failed due to a server error.",
}


@app.get("/activate", response_class=HTMLResponse)
def activate_get(
    request: Request,
    tenant: str = "default",
    next: str = "/t/default/suite",
    error: str = "",
    success_until: str = "",
):
    next_path = _safe_next(next)

    # Error banner
    msg = _ERROR_MESSAGES.get((error or "").strip(), "")
    error_banner = ""
    if msg:
        error_banner = f"""
        <div style="margin:10px 0; padding:10px; border-radius:12px;
                    background:#3b0a0a; border:1px solid rgba(239,68,68,.5); color:#fecaca;">
          <b>Activation error:</b> {msg}
        </div>
        """

    # Success banner
    success_banner = ""
    if success_until:
        success_banner = f"""
        <div style="margin:10px 0; padding:10px; border-radius:12px;
                    background:#052e16; border:1px solid rgba(34,197,94,.45); color:#bbf7d0;">
          <b>Activated successfully.</b><br/>
          Subscription valid until:
          <code style="background:rgba(255,255,255,.10); padding:2px 6px; border-radius:6px;">{success_until}</code>
        </div>
        """

    html = f"""
    <!doctype html>
    <html>
      <head><meta charset="utf-8"/><meta name="viewport" content="width=device-width, initial-scale=1"/>
        <title>Activate</title>
        <style>
          body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial; background:#0b1220; color:#e5e7eb; margin:0;}}
          .wrap{{max-width:720px; margin:0 auto; padding:46px 18px;}}
          .card{{background:#101a2f; border:1px solid rgba(255,255,255,.08); border-radius:16px; padding:22px;}}
          input{{width:100%; padding:10px; border-radius:10px; border:1px solid rgba(255,255,255,.18); background:#0b1220; color:#e5e7eb;}}
          button{{margin-top:10px; padding:10px 14px; border-radius:10px; border:none; background:#2563eb; color:white; font-weight:900;}}
          .hint{{margin-top:10px; opacity:.8; font-size:13px;}}
          code{{background:rgba(255,255,255,.08); padding:2px 6px; border-radius:6px;}}
          a.cont{{display:inline-block; margin-top:10px; padding:10px 14px; border-radius:10px; background:#2563eb; color:white; text-decoration:none; font-weight:900;}}
        </style>
      </head>
      <body>
        <div class="wrap">
          <div class="card">
            <h2 style="margin:0 0 10px 0;">Activation</h2>
            <div class="hint">Tenant: <code>{tenant}</code></div>

            {success_banner}
            {error_banner}

            <form method="post" action="/activate">
              <input type="hidden" name="tenant" value="{tenant}"/>
              <input type="hidden" name="next" value="{quote(next_path)}"/>
              <input name="code" placeholder="Enter activation code" autocomplete="off"/>
              <button type="submit">Activate</button>
            </form>

            <div class="hint">Next: <code>{next_path}</code></div>
            {"<a class='cont' href='"+next_path+"'>Continue</a>" if success_until else ""}
          </div>
        </div>
      </body>
    </html>
    """
    return HTMLResponse(html)


@app.post("/activate")
def activate_post(
    request: Request,
    tenant: str = Form("default"),
    next: str = Form("/t/default/suite"),
    code: str = Form(""),
):
    tenant_slug = (tenant or "default").strip().lower()
    next_path = _safe_next(next)
    raw_code = (code or "").strip()

    def bounce(err: str):
        return RedirectResponse(
            url=f"/activate?tenant={tenant_slug}&next={quote(next_path)}&error={err}",
            status_code=303,
        )

    if not raw_code:
        return bounce("missing")

    db = SessionLocal()
    try:
        from app.models.tenant import Tenant
        from app.models.licensing import ActivationCode, Subscription, Plan, LicenseAuditLog

        t = db.query(Tenant).filter(Tenant.slug == tenant_slug).first()
        if not t:
            return bounce("tenant_missing")

        code_hash = _sha256(raw_code)
        ac = db.query(ActivationCode).filter(ActivationCode.code_hash == code_hash).first()
        if not ac:
            return bounce("invalid")

        if getattr(ac, "tenant_id", None) and ac.tenant_id != t.id:
            return bounce("wrong_tenant")

        if getattr(ac, "revoked_at", None):
            return bounce("revoked")

        redeem_by = getattr(ac, "redeem_by", None)
        if redeem_by and redeem_by < datetime.utcnow():
            return bounce("expired")

        max_red = int(getattr(ac, "max_redemptions", 1) or 1)
        redeemed = int(getattr(ac, "redeemed_count", 0) or 0)
        if redeemed >= max_red:
            return bounce("used")

        plan = db.query(Plan).filter(Plan.id == ac.plan_id).first()
        if not plan:
            return bounce("plan_missing")

        duration_days = int(getattr(plan, "duration_days", 0) or 0)
        if duration_days <= 0:
            duration_days = 7

        now = datetime.utcnow()

        current = (
            db.query(Subscription)
            .filter(Subscription.tenant_id == t.id)
            .order_by(Subscription.ends_at.desc())
            .first()
        )

        if current and getattr(current, "ends_at", None) and current.ends_at > now and str(getattr(current, "status", "active")).lower() == "active":
            starts_at = current.starts_at or now
            ends_at = current.ends_at + timedelta(days=duration_days)
        else:
            starts_at = now
            ends_at = now + timedelta(days=duration_days)

        new_sub = Subscription(
            id=str(uuid.uuid4()),
            tenant_id=t.id,
            plan_id=plan.id,
            status="active",
            starts_at=starts_at,
            ends_at=ends_at,
            source="activation_code",
        )
        db.add(new_sub)

        if hasattr(ac, "redeemed_count"):
            ac.redeemed_count = redeemed + 1
        if hasattr(ac, "redeemed_at"):
            ac.redeemed_at = now

        try:
            db.add(
                LicenseAuditLog(
                    id=str(uuid.uuid4()),
                    tenant_id=t.id,
                    event_type="redeem_success",
                    details_json=f'{{"plan":"{getattr(plan,"code","")}", "days":{duration_days}}}',
                    created_at=now,
                )
            )
        except Exception:
            pass

        db.commit()

        # Store subscription_until in session for suite UI label
        until_iso = _format_iso_utc(ends_at)
        _set_sess(request, "subscription_until", until_iso)

        # Redirect back to /activate with success banner
        return RedirectResponse(
            url=f"/activate?tenant={tenant_slug}&next={quote(next_path)}&success_until={quote(until_iso)}",
            status_code=303,
        )

    except Exception:
        db.rollback()
        return bounce("internal")
    finally:
        db.close()


# ----------------------------
# Gate middleware
# ----------------------------
class TenantGateMiddleware(BaseHTTPMiddleware):
    """Tenant auth + subscription gate.
    Also attaches Sentry tags if Sentry is enabled.
    """

    async def dispatch(self, request: Request, call_next):
        path = request.url.path or ""

        # Sentry context
        if sentry_sdk and _SENTRY_DSN:
            s = _sess(request)
            try:
                sentry_sdk.set_tag("path", path)
                if s.get("tenant_slug"):
                    sentry_sdk.set_tag("tenant_slug", s.get("tenant_slug"))
                if s.get("user_id"):
                    sentry_sdk.set_user({"id": s.get("user_id"), "email": s.get("email")})
            except Exception:
                pass

        # Public
        if (
            path.startswith("/static")
            or path.startswith("/health")
            or path.startswith("/auth/")
            or path.startswith("/activate")
            or path == "/"
            or path.startswith("/me")
            or path.startswith("/subscription")
        ):
            return await call_next(request)

        # Admin (protected in admin router)
        if path.startswith("/admin"):
            return await call_next(request)

        # Tenant routes
        if path.startswith("/t/"):
            if not _logged_in(request):
                return RedirectResponse(url=f"/auth/login?next={quote(path)}", status_code=303)

            tenant_slug = _extract_tenant_slug(path)
            if _session_tenant_slug(request) != tenant_slug:
                return HTMLResponse("Forbidden (tenant mismatch)", status_code=403)

            # Save subscription_until into session so suite can display it
            until = _get_subscription_until(tenant_slug)
            if until:
                _set_sess(request, "subscription_until", until)

            
            # Enforce password reset if required
            if _sess(request).get("must_reset_password"):
                return RedirectResponse(url=f"/auth/change-password?next={quote(path)}", status_code=303)

            if request.method in ("POST", "PUT", "PATCH", "DELETE"):
                if _user_role(request) not in ("owner", "admin"):
                    return HTMLResponse("Forbidden (insufficient role)", status_code=403)

            if not _subscription_active(tenant_slug):
                return RedirectResponse(url=f"/activate?tenant={tenant_slug}&next={quote(path)}", status_code=307)

        return await call_next(request)


# Add gate FIRST, sessions LAST (so sessions run first)
app.add_middleware(TenantGateMiddleware)

_session_secret = (os.getenv("SECRET_KEY") or "").strip() or "dev-secret-key-change-me"
app.add_middleware(SessionMiddleware, secret_key=_session_secret, same_site="lax", https_only=True)


# ----------------------------
# Seed defaults + bootstrap owner
# ----------------------------
def seed_defaults() -> None:
    db = SessionLocal()
    try:
        from app.models.tenant import Tenant
        from app.models.clinic_settings import ClinicSettings
        from app.models.licensing import Plan, Subscription
        from app.models.user import User

        # default tenant
        t = db.query(Tenant).filter(Tenant.slug == "default").first()
        if not t:
            t = Tenant(id=str(uuid.uuid4()), slug="default", name="Default Tenant", status="active")
            if hasattr(t, "created_at"):
                t.created_at = datetime.utcnow()
            db.add(t)
            db.commit()
            db.refresh(t)

        # settings row
        cs = db.query(ClinicSettings).filter(ClinicSettings.tenant_id == t.id).first()
        if not cs:
            db.add(ClinicSettings(tenant_id=t.id))
            db.commit()

        # plans
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

        # subscription if missing
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
                    ends_at=datetime.utcnow() + timedelta(days=int(p_trial.duration_days)),
                    source="manual",
                )
            )
            db.commit()

        # bootstrap owner user (one-time)
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
                print("BOOTSTRAP: IMPORTANT: remove BOOTSTRAP_OWNER_EMAIL and BOOTSTRAP_OWNER_PASSWORD from Render env vars after first successful login.")

    finally:
        db.close()


@app.on_event("startup")
def on_startup():
    # Ensure DB schema is up-to-date before any ORM queries (prevents startup crashes)
    run_migrations()
    seed_defaults()

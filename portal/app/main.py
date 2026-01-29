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
from app.routers.web import router as web_router
from app.routers.auth import router as auth_router
from app.routers.admin import router as admin_router

# ✅ IMPORTANT: use your project's import root ("app"), not "portal.app"
from app.api.public_alias import router as public_alias_router


app = FastAPI(title="Calendo Portal", version="1.0.0")

# Routers + static
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(auth_router)
app.include_router(web_router)
app.include_router(admin_router)

# ✅ Adds /api/clinic_settings and /api/license
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

# ... (rest of your file unchanged)
# Keep everything below exactly as you already have it.

from __future__ import annotations

from datetime import datetime, timedelta
import uuid
import os
from urllib.parse import quote, unquote

from fastapi import FastAPI, Request, Response, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

import bcrypt

from app.db import SessionLocal
from app.routers.web import router as web_router
from app.routers.auth import router as auth_router
from app.routers.admin import router as admin_router


app = FastAPI(title="Calendo Portal", version="1.0.0")

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(auth_router)
app.include_router(web_router)
app.include_router(admin_router)


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
    # SAFE: never triggers the SessionMiddleware assertion
    s = request.scope.get("session")
    return s if isinstance(s, dict) else {}


@app.get("/me")
def me(request: Request):
    s = _sess(request)
    return {
        "user_id": s.get("user_id"),
        "email": s.get("email"),
        "role": s.get("role"),
        "tenant_slug": s.get("tenant_slug"),
        "tenant_id": s.get("tenant_id"),
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
# Gate middleware
# ----------------------------
class TenantGateMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path or ""

        if (
            path.startswith("/static")
            or path.startswith("/health")
            or path.startswith("/auth/")
            or path.startswith("/activate")
            or path == "/"
            or path.startswith("/me")
        ):
            return await call_next(request)

        if path.startswith("/admin"):
            return await call_next(request)

        if path.startswith("/t/"):
            if not _logged_in(request):
                return RedirectResponse(url=f"/auth/login?next={quote(path)}", status_code=303)

            tenant_slug = _extract_tenant_slug(path)
            if _session_tenant_slug(request) != tenant_slug:
                return HTMLResponse("Forbidden (tenant mismatch)", status_code=403)

            if request.method in ("POST", "PUT", "PATCH", "DELETE"):
                if _user_role(request) not in ("owner", "admin"):
                    return HTMLResponse("Forbidden (insufficient role)", status_code=403)

            if not _subscription_active(tenant_slug):
                return RedirectResponse(url=f"/activate?tenant={tenant_slug}&next={quote(path)}", status_code=307)

        return await call_next(request)


# IMPORTANT: add gate FIRST
app.add_middleware(TenantGateMiddleware)

# IMPORTANT: add SessionMiddleware LAST so it runs FIRST
_session_secret = (os.getenv("SECRET_KEY") or "").strip() or "dev-secret-key-change-me"
app.add_middleware(SessionMiddleware, secret_key=_session_secret, same_site="lax", https_only=True)


@app.get("/activate", response_class=HTMLResponse)
def activate_get(request: Request, tenant: str = "default", next: str = "/t/default/suite"):
    next_path = _safe_next(next)
    return HTMLResponse(f"<h2>Activation required</h2><p>Tenant: {tenant}</p><p>Next: {next_path}</p>")


@app.post("/activate")
def activate_post(next: str = Form("/t/default/suite")):
    return RedirectResponse(url=_safe_next(next), status_code=303)


def seed_defaults() -> None:
    db = SessionLocal()
    try:
        from app.models.tenant import Tenant
        from app.models.clinic_settings import ClinicSettings
        from app.models.licensing import Plan, Subscription
        from app.models.user import User

        t = db.query(Tenant).filter(Tenant.slug == "default").first()
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
                    ends_at=datetime.utcnow() + timedelta(days=int(p_trial.duration_days)),
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
                    )
                )
                db.commit()
                print(f"BOOTSTRAP: created owner {email} for tenant {t.slug}")

    finally:
        db.close()


@app.on_event("startup")
def on_startup():
    seed_defaults()

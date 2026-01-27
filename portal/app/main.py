from __future__ import annotations

from datetime import datetime, timedelta
import uuid
import hashlib
import os
from urllib.parse import quote, unquote

from fastapi import FastAPI, Request, Response, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

import bcrypt

from app.db import SessionLocal
from app.routers.web import router as web_router
from app.routers.auth import router as auth_router
from app.routers.admin import router as admin_router


app = FastAPI(title="Calendo Portal", version="1.0.0")

_session_secret = (os.getenv("SECRET_KEY") or "").strip() or "dev-secret-key-change-me"
app.add_middleware(SessionMiddleware, secret_key=_session_secret, same_site="lax", https_only=True)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(auth_router)
app.include_router(web_router)
app.include_router(admin_router)


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


def _session(request: Request) -> dict:
    sess = request.scope.get("session")
    return sess if isinstance(sess, dict) else {}


def _user_role(request: Request) -> str:
    return str(_session(request).get("role") or "").lower()


def _logged_in(request: Request) -> bool:
    return bool(_session(request).get("user_id"))


def _extract_tenant_slug(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "t":
        return parts[1]
    return "default"


def _session_tenant_slug(request: Request) -> str:
    return str(_session(request).get("tenant_slug") or "default")


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
        <style>
          body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial; background:#0b1220; color:#e5e7eb; margin:0;}}
          .wrap{{max-width:920px; margin:0 auto; padding:46px 18px;}}
          .card{{background:#101a2f; border:1px solid rgba(255,255,255,.08); border-radius:16px; padding:22px;}}
          a.btn{{display:inline-block; padding:10px 14px; border-radius:10px; text-decoration:none; font-weight:800;}}
          a.primary{{background:#2563eb; color:white;}}
          a.ghost{{border:1px solid rgba(255,255,255,.18); color:#e5e7eb;}}
          .row{{display:flex; gap:12px; flex-wrap:wrap; margin-top:14px;}}
          .hint{{margin-top:14px; font-size:13px; opacity:.75;}}
          code{{background:rgba(255,255,255,.08); padding:2px 6px; border-radius:6px;}}
        </style>
      </head>
      <body>
        <div class="wrap">
          <div class="card">
            <h1 style="margin:0 0 8px 0;">Clinic Suite</h1>
            <div style="opacity:.9;">Please sign in to access services.</div>
            <div class="row">
              <a class="btn primary" href="{login_url}">Log in</a>
              <a class="btn ghost" href="/health" target="_blank">System health</a>
              {"<a class='btn ghost' href='/auth/logout'>Log out</a>" if _logged_in(request) else ""}
            </div>
            <div class="hint">Default tenant: <code>{default_tenant}</code></div>
          </div>
        </div>
      </body>
    </html>
    """
    return HTMLResponse(html)


@app.middleware("http")
async def tenant_authz_and_subscription_gate(request: Request, call_next):
    path = request.url.path or ""

    # Public
    if (
        path.startswith("/static")
        or path.startswith("/health")
        or path.startswith("/auth/")
        or path.startswith("/activate")
        or path == "/"
    ):
        return await call_next(request)

    # Super-admin area is protected inside admin.py (session/header) -> let through
    if path.startswith("/admin"):
        return await call_next(request)

    # Tenant routes require login + tenant match + subscription
    if path.startswith("/t/"):
        if not _logged_in(request):
            return RedirectResponse(url=f"/auth/login?next={quote(path)}", status_code=303)

        tenant_slug = _extract_tenant_slug(path)
        if _session_tenant_slug(request) != tenant_slug:
            return HTMLResponse("Forbidden (tenant mismatch)", status_code=403)

        # Only owner/admin can perform writes
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            if _user_role(request) not in ("owner", "admin"):
                return HTMLResponse("Forbidden (insufficient role)", status_code=403)

        if not _subscription_active(tenant_slug):
            return RedirectResponse(url=f"/activate?tenant={tenant_slug}&next={quote(path)}", status_code=307)

    return await call_next(request)


# ---------------------------------------------------------
# Activation (minimal placeholder so redirects don't 404)
# You can later wire this to activation_codes redemption.
# ---------------------------------------------------------
@app.get("/activate", response_class=HTMLResponse)
def activate_get(request: Request, tenant: str = "default", next: str = "/t/default/suite", error: str = ""):
    next_path = _safe_next(next)
    msg = ""
    if error:
        msg = f"<div style='margin:10px 0;color:#fecaca;'><b>{error}</b></div>"

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
        </style>
      </head>
      <body>
        <div class="wrap">
          <div class="card">
            <h2 style="margin:0 0 10px 0;">Activation required</h2>
            <div class="hint">Tenant: <code>{tenant}</code></div>
            <div class="hint">Your subscription is expired.</div>
            {msg}
            <form method="post" action="/activate">
              <input type="hidden" name="tenant" value="{tenant}"/>
              <input type="hidden" name="next" value="{quote(next_path)}"/>
              <input name="code" placeholder="Enter activation code"/>
              <button type="submit">Activate</button>
            </form>
            <div class="hint">After activation you will return to: <code>{next_path}</code></div>
          </div>
        </div>
      </body>
    </html>
    """
    return HTMLResponse(html)


@app.post("/activate")
def activate_post(tenant: str = Form("default"), next: str = Form("/t/default/suite"), code: str = Form("")):
    # Placeholder: keep the activation page functional without breaking routing.
    # If you want, I can wire this to ActivationCode/Subcription (like earlier).
    next_path = _safe_next(next)
    return RedirectResponse(url=next_path, status_code=303)


# ---------------------------------------------------------
# Seed defaults + Bootstrap owner user
# ---------------------------------------------------------
def seed_defaults() -> None:
    db = SessionLocal()
    try:
        from app.models.tenant import Tenant
        from app.models.clinic_settings import ClinicSettings
        from app.models.licensing import Plan, Subscription
        from app.models.user import User

        # Default tenant
        t = db.query(Tenant).filter(Tenant.slug == "default").first()
        if not t:
            t = Tenant(id=str(uuid.uuid4()), slug="default", name="Default Tenant", status="active")
            if hasattr(t, "created_at"):
                setattr(t, "created_at", datetime.utcnow())
            db.add(t)
            db.commit()
            db.refresh(t)

        # Settings row
        cs = db.query(ClinicSettings).filter(ClinicSettings.tenant_id == t.id).first()
        if not cs:
            db.add(ClinicSettings(tenant_id=t.id))
            db.commit()

        # Plans
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

        # Subscription for default tenant (trial if missing)
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

        # ---- BOOTSTRAP OWNER USER (the missing part) ----
        email = (os.getenv("BOOTSTRAP_OWNER_EMAIL") or "").strip().lower()
        pw = (os.getenv("BOOTSTRAP_OWNER_PASSWORD") or "").strip()

        if email and pw:
            existing = db.query(User).filter(User.tenant_id == t.id, User.email == email).first()
            if not existing:
                pw_hash = bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
                owner = User(
                    id=str(uuid.uuid4()),
                    tenant_id=t.id,
                    email=email,
                    password_hash=pw_hash,
                    role="owner",
                    is_active=True,
                )
                db.add(owner)
                db.commit()
                print(f"BOOTSTRAP: created owner user {email} for tenant {t.slug}")
        else:
            print("BOOTSTRAP: BOOTSTRAP_OWNER_EMAIL/PASSWORD not set (skipping owner creation)")
    finally:
        db.close()


@app.on_event("startup")
def on_startup():
    seed_defaults()

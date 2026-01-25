from __future__ import annotations

from datetime import datetime, timedelta
import uuid
import hashlib
from urllib.parse import quote, unquote

from fastapi import FastAPI, Request, Response, Form
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

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
# Health endpoints (Render-friendly)
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
# Public landing page (Clinic Suite first)
# ----------------------------
@app.get("/", response_class=HTMLResponse)
def landing(request: Request):
    default_tenant = "default"
    next_url = f"/t/{default_tenant}/suite"
    login_url = f"/login?next={quote(next_url)}"

    html = f"""
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8"/>
        <meta name="viewport" content="width=device-width, initial-scale=1"/>
        <title>Clinic Suite</title>
        <style>
          body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,Arial; background:#0b1220; color:#e5e7eb; margin:0;}}
          .wrap{{max-width:920px; margin:0 auto; padding:46px 18px;}}
          .card{{background:#101a2f; border:1px solid rgba(255,255,255,.08); border-radius:16px; padding:22px;}}
          h1{{margin:0 0 8px 0; font-size:28px;}}
          p{{margin:0 0 14px 0; opacity:.9; line-height:1.5;}}
          .row{{display:flex; gap:12px; flex-wrap:wrap; margin-top:14px;}}
          a.btn{{display:inline-block; padding:10px 14px; border-radius:10px; text-decoration:none; font-weight:700;}}
          a.primary{{background:#2563eb; color:white;}}
          a.ghost{{border:1px solid rgba(255,255,255,.18); color:#e5e7eb;}}
          .hint{{margin-top:14px; font-size:13px; opacity:.75;}}
          code{{background:rgba(255,255,255,.08); padding:2px 6px; border-radius:6px;}}
        </style>
      </head>
      <body>
        <div class="wrap">
          <div class="card">
            <h1>Clinic Suite</h1>
            <p>Welcome. Please log in to access the Therapy Portal, Calendar, Billing, and SMS services.</p>
            <div class="row">
              <a class="btn primary" href="{login_url}">Log in</a>
              <a class="btn ghost" href="/health" target="_blank">System health</a>
            </div>
            <div class="hint">
              Default tenant: <code>{default_tenant}</code>. After login you will be redirected to
              <code>{next_url}</code>.
            </div>
          </div>
        </div>
      </body>
    </html>
    """
    return HTMLResponse(html)


# ----------------------------
# Subscription enforcement
# ----------------------------
def _extract_tenant_slug(path: str) -> str:
    parts = [p for p in path.split("/") if p]
    if len(parts) >= 2 and parts[0] == "t":
        return parts[1]
    return "default"


def _is_subscription_active(tenant_slug: str) -> bool:
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


@app.middleware("http")
async def subscription_gate(request: Request, call_next):
    path = request.url.path or ""

    # Allowlist (never gate these)
    if (
        path.startswith("/static")
        or path.startswith("/health")
        or path.startswith("/login")
        or path.startswith("/logout")
        or path.startswith("/admin")
        or path.startswith("/activate")
        or path == "/"
    ):
        return await call_next(request)

    # Only enforce on tenant routes
    if not path.startswith("/t/"):
        return await call_next(request)

    tenant_slug = _extract_tenant_slug(path)

    if not _is_subscription_active(tenant_slug):
        next_url = quote(path)
        return RedirectResponse(url=f"/activate?tenant={tenant_slug}&next={next_url}", status_code=307)

    return await call_next(request)


# ----------------------------
# Activation page + error messaging
# ----------------------------
_ERROR_MESSAGES = {
    "missing": "Please enter an activation code.",
    "not_found": "This tenant was not found. Check the tenant slug.",
    "invalid": "Invalid activation code. Please check and try again.",
    "wrong_tenant": "That activation code belongs to a different tenant.",
    "revoked": "This activation code has been revoked.",
    "expired": "This activation code has expired and can’t be redeemed.",
    "used": "This activation code has already been used.",
    "plan_missing": "Plan information for this code is missing. Contact support.",
    "internal": "Activation failed due to a server error. Try again or contact support.",
}


@app.get("/activate", response_class=HTMLResponse)
def activate_page(request: Request, tenant: str = "default", next: str = "/", error: str = ""):
    msg = _ERROR_MESSAGES.get((error or "").strip(), "")
    banner = ""
    if msg:
        banner = f"""
        <div style="margin:10px 0; padding:10px; border-radius:12px;
                    background:#3b0a0a; border:1px solid rgba(239,68,68,.5); color:#fecaca;">
          <b>Activation error:</b> {msg}
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
          button{{margin-top:10px; padding:10px 14px; border-radius:10px; border:none; background:#2563eb; color:white; font-weight:800;}}
          .hint{{margin-top:10px; opacity:.8; font-size:13px;}}
          code{{background:rgba(255,255,255,.08); padding:2px 6px; border-radius:6px;}}
        </style>
      </head>
      <body>
        <div class="wrap">
          <div class="card">
            <h2 style="margin:0 0 10px 0;">Activation required</h2>
            <div class="hint">Tenant: <b>{tenant}</b></div>
            <div class="hint">Your subscription is expired. Enter an activation code to continue.</div>
            {banner}

            <form method="post" action="/activate">
              <input type="hidden" name="tenant" value="{tenant}"/>
              <input type="hidden" name="next" value="{next}"/>
              <input name="code" placeholder="Enter activation code" autocomplete="off"/>
              <button type="submit">Activate</button>
            </form>

            <div class="hint">After activation, you will be redirected to: <code>{next}</code></div>
          </div>
        </div>
      </body>
    </html>
    """
    return HTMLResponse(html)


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


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


@app.post("/activate")
def activate_post(
    tenant: str = Form("default"),
    next: str = Form("/"),
    code: str = Form(""),
):
    tenant_slug = (tenant or "default").strip().lower()
    raw_code = (code or "").strip()
    next_path = _safe_next(next)

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
            return bounce("not_found")

        code_hash = _hash_code(raw_code)
        ac = db.query(ActivationCode).filter(ActivationCode.code_hash == code_hash).first()
        if not ac:
            return bounce("invalid")

        if hasattr(ac, "tenant_id") and ac.tenant_id != t.id:
            return bounce("wrong_tenant")

        if getattr(ac, "revoked_at", None):
            return bounce("revoked")

        redeem_by = getattr(ac, "redeem_by", None)
        if redeem_by and redeem_by < datetime.utcnow():
            return bounce("expired")

        max_red = int(getattr(ac, "max_redemptions", 1) or 1)
        redeemed_count = int(getattr(ac, "redeemed_count", 0) or 0)
        if redeemed_count >= max_red:
            return bounce("used")

        plan = None
        plan_id = getattr(ac, "plan_id", None)
        if plan_id is not None:
            plan = db.query(Plan).filter(Plan.id == plan_id).first()
        if not plan:
            plan = db.query(Plan).filter(Plan.code == "TRIAL_7D").first()
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
            new_ends = current.ends_at + timedelta(days=duration_days)
        else:
            starts_at = now
            new_ends = now + timedelta(days=duration_days)

        new_sub = Subscription(
            id=str(uuid.uuid4()),
            tenant_id=t.id,
            plan_id=plan.id,
            status="active",
            starts_at=starts_at,
            ends_at=new_ends,
            source="activation_code",
        )
        db.add(new_sub)

        if hasattr(ac, "redeemed_count"):
            ac.redeemed_count = redeemed_count + 1
        if hasattr(ac, "redeemed_at"):
            ac.redeemed_at = now
        if hasattr(ac, "last_redeemed_at"):
            ac.last_redeemed_at = now

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
        return RedirectResponse(url=next_path, status_code=303)

    except Exception:
        # Any unexpected exception should show an explicit error banner instead of silent reload.
        return bounce("internal")
    finally:
        db.close()


# ----------------------------
# Seed defaults (Alembic handles schema)
# ----------------------------
def seed_defaults() -> None:
    db = SessionLocal()
    try:
        from app.models.tenant import Tenant
        from app.models.clinic_settings import ClinicSettings
        from app.models.licensing import Plan, Subscription

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

        cs = db.query(ClinicSettings).filter(ClinicSettings.tenant_id == t.id).first()
        if not cs:
            cs = ClinicSettings(tenant_id=t.id)
            db.add(cs)
            db.commit()

        def ensure_plan(code: str, days: int) -> Plan:
            p = db.query(Plan).filter(Plan.code == code).first()
            if not p:
                p = Plan(code=code, duration_days=days, features_json="{}")
                db.add(p)
                db.commit()
                db.refresh(p)
            return p

        p_trial = ensure_plan("TRIAL_7D", 7)
        ensure_plan("MONTHLY_30D", 30)
        ensure_plan("YEARLY_365D", 365)

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
    finally:
        db.close()


@app.on_event("startup")
def on_startup():
    seed_defaults()

from __future__ import annotations

import os
import secrets
import hashlib
from datetime import datetime, timedelta
from typing import Optional

import bcrypt
import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.config import settings
from app.models.tenant import Tenant
from app.models.licensing import Plan, Subscription, ActivationCode, LicenseAuditLog
from app.utils.security import generate_temp_password

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _session(request: Request) -> dict:
    s = request.scope.get("session")
    return s if isinstance(s, dict) else {}


def _expected_admin_key() -> str:
    return (settings.ADMIN_KEY or os.getenv("ADMIN_KEY") or "").strip()


def _get_admin_key_from_request(request: Request) -> str:
    hdr = (request.headers.get("X-Admin-Key") or request.headers.get("x-admin-key") or "").strip()
    if hdr:
        return hdr
    sess_key = (_session(request).get("admin_key") or "").strip()
    if sess_key:
        return sess_key
    if getattr(settings, "ALLOW_ADMIN_KEY_QUERY", False):
        return (request.query_params.get("admin_key") or "").strip()
    return ""


def _set_admin_session_key(request: Request, key: str) -> None:
    s = request.scope.get("session")
    if isinstance(s, dict):
        s["admin_key"] = key


def _clear_admin_session_key(request: Request) -> None:
    s = request.scope.get("session")
    if isinstance(s, dict):
        s.pop("admin_key", None)


def _require_admin(request: Request) -> None:
    expected = _expected_admin_key()
    if not expected:
        raise HTTPException(status_code=403, detail="ADMIN_KEY not configured")
    got = _get_admin_key_from_request(request)
    if got != expected:
        raise HTTPException(status_code=403, detail="Forbidden")


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def ensure_default_plans(db: Session) -> None:
    defaults = [
        ("TRIAL_7D", "7-day Trial", 7),
        ("MONTHLY_30D", "Monthly (30 days)", 30),
        ("YEARLY_365D", "Yearly (365 days)", 365),
    ]
    for code, name, days in defaults:
        p = db.query(Plan).filter(Plan.code == code).first()
        if not p:
            db.add(Plan(code=code, name=name, duration_days=days, features_json="{}"))
    db.commit()


def _renew_subscription_for_tenant(db: Session, tenant_id: str, plan: Plan, actor: str) -> tuple[datetime, datetime]:
    now = datetime.utcnow()

    sub = (
        db.query(Subscription)
        .filter(Subscription.tenant_id == tenant_id)
        .order_by(Subscription.ends_at.desc())
        .first()
    )

    old_end = sub.ends_at if (sub and sub.ends_at) else now
    base = sub.ends_at if (sub and sub.ends_at and sub.ends_at > now) else now
    new_end = base + timedelta(days=int(plan.duration_days or 0))

    if sub:
        if not sub.starts_at:
            sub.starts_at = now
        sub.ends_at = new_end
        sub.status = "active"
        sub.plan_id = plan.id
        sub.source = getattr(sub, "source", None) or "admin"
        db.add(sub)
    else:
        db.add(
            Subscription(
                id=secrets.token_hex(16),
                tenant_id=tenant_id,
                plan_id=plan.id,
                status="active",
                starts_at=now,
                ends_at=new_end,
                source="admin",
            )
        )

    try:
        db.add(
            LicenseAuditLog(
                id=secrets.token_hex(16),
                tenant_id=tenant_id,
                event_type="renew",
                details_json=f'{{"plan":"{plan.code}","old_end":"{old_end.isoformat()}","new_end":"{new_end.isoformat()}","actor":"{actor}"}}',
                created_at=now,
            )
        )
    except Exception:
        pass

    db.commit()
    return old_end, new_end


@router.get("/admin", response_class=HTMLResponse)
def admin_index(request: Request):
    expected = _expected_admin_key()
    if not expected:
        return HTMLResponse("<h2>ADMIN_KEY not configured</h2>", status_code=403)

    got = _get_admin_key_from_request(request)
    if got == expected:
        _set_admin_session_key(request, expected)
        return RedirectResponse(url="/admin/tenants", status_code=303)

    return templates.TemplateResponse("admin/index.html", {"request": request, "authenticated": False})


@router.post("/admin")
def admin_index_post(request: Request, admin_key: str = Form("")):
    expected = _expected_admin_key()
    if not expected:
        return RedirectResponse(url="/admin?err=not_configured", status_code=303)
    if (admin_key or "").strip() != expected:
        return RedirectResponse(url="/admin?err=invalid", status_code=303)
    _set_admin_session_key(request, expected)
    return RedirectResponse(url="/admin/tenants", status_code=303)


@router.get("/admin/logout")
def admin_logout(request: Request):
    _clear_admin_session_key(request)
    return RedirectResponse(url="/admin", status_code=303)


@router.get("/admin/tenants", response_class=HTMLResponse)
def admin_tenants(request: Request, db: Session = Depends(get_db)):
    _require_admin(request)

    from app.models.user import User
    role_rank = sa.case((User.role == "owner", 0), (User.role == "admin", 1), else_=2)
    owner_email_sq = (
        sa.select(User.email)
        .where(User.tenant_id == Tenant.id, User.is_active.is_(True), User.role.in_(["owner", "admin"]))
        .order_by(role_rank.asc(), User.email.asc())
        .limit(1)
        .scalar_subquery()
    )

    rows = db.query(Tenant, owner_email_sq.label("owner_email")).order_by(Tenant.created_at.desc()).all()
    tenants = []
    for t, owner_email in rows:
        tenants.append(
            {
                "slug": t.slug,
                "name": t.name,
                "status": t.status,
                "owner_email": owner_email or "",
            }
        )

    return templates.TemplateResponse("admin/tenants.html", {"request": request, "tenants": tenants, "base_url": str(request.base_url).rstrip("/")})


@router.post("/admin/tenants/{slug}/renew")
def admin_tenant_renew(request: Request, slug: str, plan_code: str = Form(...), db: Session = Depends(get_db)):
    _require_admin(request)
    ensure_default_plans(db)

    t = db.query(Tenant).filter(Tenant.slug == slug).first()
    if not t:
        raise HTTPException(status_code=404, detail="Tenant not found")

    plan = db.query(Plan).filter(Plan.code == plan_code).first()
    if not plan:
        raise HTTPException(status_code=400, detail="Plan not found")

    actor = (_session(request).get("email") or "admin")
    _renew_subscription_for_tenant(db, tenant_id=t.id, plan=plan, actor=str(actor))
    return RedirectResponse(url=f"/admin/licensing?tenant={slug}", status_code=303)


@router.get("/admin/tenants/{slug}/reset_password", response_class=HTMLResponse)
def admin_reset_password_page(request: Request, slug: str, db: Session = Depends(get_db)):
    _require_admin(request)

    row = db.execute(sa.text("SELECT id, slug, name FROM tenants WHERE slug=:s LIMIT 1"), {"s": slug}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Tenant not found")

    tenant_id, tenant_slug, tenant_name = row[0], row[1], row[2]

    from app.models.user import User
    role_rank = sa.case((User.role == "owner", 0), (User.role == "admin", 1), else_=2)
    target = (
        db.query(User)
        .filter(User.tenant_id == tenant_id, User.is_active.is_(True))
        .order_by(role_rank.asc(), User.email.asc())
        .first()
    )

    return templates.TemplateResponse(
        "admin/reset_password.html",
        {"request": request, "tenant": {"id": tenant_id, "slug": tenant_slug, "name": tenant_name}, "target_user": target, "done": False},
    )


@router.post("/admin/tenants/{slug}/reset_password", response_class=HTMLResponse)
def admin_reset_password_do(request: Request, slug: str, user_email: str = Form(...), db: Session = Depends(get_db)):
    _require_admin(request)

    row = db.execute(sa.text("SELECT id, slug, name FROM tenants WHERE slug=:s LIMIT 1"), {"s": slug}).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Tenant not found")

    tenant_id, tenant_slug, tenant_name = row[0], row[1], row[2]

    from app.models.user import User
    email_lc = (user_email or "").strip().lower()
    if not email_lc:
        raise HTTPException(status_code=400, detail="Email is required")

    temp_pw = generate_temp_password()
    pw_hash = bcrypt.hashpw(temp_pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

    user = db.query(User).filter(User.tenant_id == tenant_id, sa.func.lower(User.email) == email_lc).first()
    created = False
    if not user:
        user = User(
            id=secrets.token_hex(16),
            tenant_id=tenant_id,
            email=email_lc,
            password_hash=pw_hash,
            role="owner",
            is_active=True,
            must_reset_password=True,
        )
        db.add(user)
        created = True
    else:
        user.email = email_lc
        user.password_hash = pw_hash
        user.is_active = True
        user.must_reset_password = True
        db.add(user)

    try:
        db.add(
            LicenseAuditLog(
                id=secrets.token_hex(16),
                tenant_id=tenant_id,
                event_type="reset_password",
                details_json=f'{{"user":"{email_lc}","created":{str(created).lower()}}}',
                created_at=datetime.utcnow(),
            )
        )
    except Exception:
        pass

    db.commit()

    login_url = f"/auth/login?next=/t/{tenant_slug}/suite"
    return templates.TemplateResponse(
        "admin/reset_password.html",
        {
            "request": request,
            "tenant": {"id": tenant_id, "slug": tenant_slug, "name": tenant_name},
            "target_user": user,
            "done": True,
            "temp_password": temp_pw,
            "login_url": login_url,
        },
    )


@router.get("/admin/licensing", response_class=HTMLResponse)
def admin_licensing(request: Request, tenant: Optional[str] = None, db: Session = Depends(get_db)):
    _require_admin(request)
    ensure_default_plans(db)

    tenants = db.query(Tenant).order_by(Tenant.slug.asc()).all()
    plans = db.query(Plan).order_by(Plan.duration_days.asc()).all()

    selected = None
    current_sub = None
    if tenant:
        selected = db.query(Tenant).filter(Tenant.slug == tenant).first()
        if selected:
            current_sub = (
                db.query(Subscription)
                .filter(Subscription.tenant_id == selected.id)
                .order_by(Subscription.ends_at.desc())
                .first()
            )

    code = request.query_params.get("code") or ""
    return templates.TemplateResponse(
        "admin/licensing.html",
        {"request": request, "tenants": tenants, "plans": plans, "selected": selected, "current_sub": current_sub, "code": code},
    )


@router.post("/admin/licensing/generate")
def admin_generate_code(request: Request, tenant_slug: str = Form(...), plan_code: str = Form(...), db: Session = Depends(get_db)):
    _require_admin(request)
    ensure_default_plans(db)

    t = db.query(Tenant).filter(Tenant.slug == tenant_slug).first()
    if not t:
        raise HTTPException(status_code=404, detail="Tenant not found")

    plan = db.query(Plan).filter(Plan.code == plan_code).first()
    if not plan:
        raise HTTPException(status_code=400, detail="Plan not found")

    raw = f"{tenant_slug.upper()}-{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}"
    code_hash = _hash_code(raw)

    db.add(
        ActivationCode(
            id=secrets.token_hex(16),
            tenant_id=t.id,
            plan_id=plan.id,
            code_hash=code_hash,
            issued_at=datetime.utcnow(),
            redeem_by=datetime.utcnow() + timedelta(days=90),
            max_redemptions=1,
            redeemed_count=0,
            revoked_at=None,
            note="",
        )
    )
    try:
        db.add(
            LicenseAuditLog(
                id=secrets.token_hex(16),
                tenant_id=t.id,
                event_type="code_generated",
                details_json=f'{{"plan":"{plan.code}"}}',
                created_at=datetime.utcnow(),
            )
        )
    except Exception:
        pass

    db.commit()
    return RedirectResponse(url=f"/admin/licensing?tenant={tenant_slug}&code={raw}", status_code=303)


@router.post("/admin/licensing/renew")
def admin_renew_from_licensing(request: Request, tenant_slug: str = Form(...), plan_code: str = Form(...), db: Session = Depends(get_db)):
    _require_admin(request)
    ensure_default_plans(db)

    t = db.query(Tenant).filter(Tenant.slug == tenant_slug).first()
    if not t:
        raise HTTPException(status_code=404, detail="Tenant not found")

    plan = db.query(Plan).filter(Plan.code == plan_code).first()
    if not plan:
        raise HTTPException(status_code=400, detail="Plan not found")

    actor = (_session(request).get("email") or "admin")
    _renew_subscription_for_tenant(db, tenant_id=t.id, plan=plan, actor=str(actor))
    return RedirectResponse(url=f"/admin/licensing?tenant={tenant_slug}", status_code=303)

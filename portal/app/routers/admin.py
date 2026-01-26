from __future__ import annotations

import os
import secrets
import hashlib
from datetime import datetime, timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.tenant import Tenant
from app.models.clinic_settings import ClinicSettings
from app.models.licensing import Plan, Subscription, ActivationCode, LicenseAuditLog


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _require_admin(request: Request) -> None:
    expected = (os.getenv("ADMIN_KEY") or "").strip()
    if not expected:
        raise HTTPException(status_code=403, detail="ADMIN_KEY not configured")

    got = (request.query_params.get("admin_key") or "").strip()
    if not got:
        got = (request.headers.get("x-admin-key") or request.headers.get("X-Admin-Key") or "").strip()

    if got != expected:
        raise HTTPException(status_code=403, detail="Forbidden")


def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _portal_base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _sms_base() -> str:
    return (os.getenv("SMS_APP_URL") or "").strip().rstrip("/")


def _sms_url_for_tenant(slug: str) -> str:
    base = _sms_base()
    if not base:
        return ""
    if base.endswith("/sms"):
        return f"{base}?tenant={slug}"
    return f"{base}/sms?tenant={slug}"


def ensure_default_plans(db: Session) -> None:
    """
    Ensure the plan codes used by the UI exist.
    This prevents Create Tenant from failing if plan_code isn't found.
    """
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


# ---------------------------------------------------------------------------
# NEW: Single admin dashboard entry point
# ---------------------------------------------------------------------------
@router.get("/admin", response_class=HTMLResponse)
def admin_index(request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    base_url = _portal_base(request)
    ak = request.query_params.get("admin_key", "")

    return templates.TemplateResponse(
        "admin/index.html",
        {
            "request": request,
            "admin_key": ak,
            "base_url": base_url,
            "sms_base": _sms_base(),
            "tenants_url": f"{base_url}/admin/tenants?admin_key={ak}",
            "licensing_url": f"{base_url}/admin/licensing?admin_key={ak}",
            "links_url": f"{base_url}/admin/links?admin_key={ak}",
        },
    )


@router.get("/admin/tenants", response_class=HTMLResponse)
def admin_tenants(request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    tenants = db.query(Tenant).order_by(Tenant.created_at.desc()).all()
    base_url = _portal_base(request)
    return templates.TemplateResponse(
        "admin/tenants.html",
        {
            "request": request,
            "tenants": tenants,
            "base_url": base_url,
            "admin_key": request.query_params.get("admin_key", ""),
        },
    )


@router.get("/admin/tenants/new", response_class=HTMLResponse)
def admin_tenants_new(request: Request, db: Session = Depends(get_db)):
    _require_admin(request)

    ensure_default_plans(db)

    plans = db.query(Plan).order_by(Plan.duration_days.asc()).all()
    return templates.TemplateResponse(
        "admin/tenant_new.html",
        {"request": request, "plans": plans, "admin_key": request.query_params.get("admin_key", "")},
    )


@router.post("/admin/tenants/new")
def admin_tenants_create(
    request: Request,
    slug: str = Form(...),
    name: str = Form(...),
    plan_code: str = Form("TRIAL_7D"),
    db: Session = Depends(get_db),
):
    _require_admin(request)

    ensure_default_plans(db)

    slug = slug.strip().lower()
    if not slug or " " in slug:
        raise HTTPException(400, "Invalid slug (no spaces)")

    if db.query(Tenant).filter(Tenant.slug == slug).first():
        raise HTTPException(400, "Slug already exists")

    try:
        t = Tenant(id=secrets.token_hex(16), slug=slug, name=name.strip(), status="active")
        if hasattr(t, "created_at"):
            setattr(t, "created_at", datetime.utcnow())

        db.add(t)
        db.commit()
        db.refresh(t)

        cs = ClinicSettings(tenant_id=t.id)
        db.add(cs)
        db.commit()

        plan = db.query(Plan).filter(Plan.code == plan_code).first()
        if not plan:
            raise HTTPException(400, f"Unknown plan: {plan_code}")

        sub = Subscription(
            id=secrets.token_hex(16),
            tenant_id=t.id,
            plan_id=plan.id,
            status="active",
            starts_at=datetime.utcnow(),
            ends_at=datetime.utcnow() + timedelta(days=int(plan.duration_days)),
            source="manual",
        )
        db.add(sub)

        db.add(
            LicenseAuditLog(
                id=secrets.token_hex(16),
                tenant_id=t.id,
                event_type="tenant_created",
                details_json=f'{{"slug":"{slug}","plan":"{plan.code}"}}',
                created_at=datetime.utcnow(),
            )
        )
        db.commit()

        ak = request.query_params.get("admin_key", "")
        return RedirectResponse(url=f"/admin/tenants?admin_key={ak}", status_code=303)

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Create tenant failed: {type(e).__name__}: {e}")


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

    return templates.TemplateResponse(
        "admin/licensing.html",
        {
            "request": request,
            "tenants": tenants,
            "plans": plans,
            "selected": selected,
            "current_sub": current_sub,
            "admin_key": request.query_params.get("admin_key", ""),
        },
    )


@router.post("/admin/licensing/generate")
def admin_generate_code(
    request: Request,
    tenant_slug: str = Form(...),
    plan_code: str = Form(...),
    db: Session = Depends(get_db),
):
    _require_admin(request)

    ensure_default_plans(db)

    t = db.query(Tenant).filter(Tenant.slug == tenant_slug).first()
    if not t:
        raise HTTPException(404, "Tenant not found")

    plan = db.query(Plan).filter(Plan.code == plan_code).first()
    if not plan:
        raise HTTPException(400, "Plan not found")

    raw = f"{tenant_slug.upper()}-{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}"
    code_hash = _hash_code(raw)

    try:
        ac = ActivationCode(
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
        db.add(ac)
        db.add(
            LicenseAuditLog(
                id=secrets.token_hex(16),
                tenant_id=t.id,
                event_type="code_generated",
                details_json=f'{{"plan":"{plan.code}"}}',
                created_at=datetime.utcnow(),
            )
        )
        db.commit()

        ak = request.query_params.get("admin_key", "")
        return RedirectResponse(url=f"/admin/licensing?tenant={tenant_slug}&admin_key={ak}&code={raw}", status_code=303)

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Generate code failed: {type(e).__name__}: {e}")


@router.get("/admin/links", response_class=HTMLResponse)
def admin_links(request: Request, db: Session = Depends(get_db)):
    _require_admin(request)

    base_url = _portal_base(request)
    ak = request.query_params.get("admin_key", "")
    tenants = db.query(Tenant).order_by(Tenant.slug.asc()).all()

    rows = []
    for t in tenants:
        rows.append(
            {
                "slug": t.slug,
                "name": t.name,
                "suite_url": f"{base_url}/t/{t.slug}/suite",
                "sms_url": _sms_url_for_tenant(t.slug),
            }
        )

    return templates.TemplateResponse(
        "admin/links.html",
        {
            "request": request,
            "admin_key": ak,
            "base_url": base_url,
            "sms_base": _sms_base(),
            "admin_tenants_url": f"{base_url}/admin/tenants?admin_key={ak}",
            "admin_licensing_url": f"{base_url}/admin/licensing?admin_key={ak}",
            "admin_links_url": f"{base_url}/admin/links?admin_key={ak}",
            "tenants": rows,
        },
    )

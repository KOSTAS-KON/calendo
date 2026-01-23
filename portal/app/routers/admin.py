from __future__ import annotations

import hashlib
import os
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Request, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.db import get_db
from app.models.tenant import Tenant
from app.models.licensing import Plan, Subscription, ActivationCode
from app.tenancy import get_or_create_tenant
from app.routers.web import templates  # reuse Jinja environment

router = APIRouter(prefix="/admin", tags=["admin"])

def _require_admin(request: Request) -> None:
    key = (request.headers.get("X-Admin-Key") or request.query_params.get("admin_key") or "").strip()
    if not settings.ADMIN_KEY or key != settings.ADMIN_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")

def _render(request: Request, name: str, ctx: dict):
    return templates.TemplateResponse(name, {"request": request, **(ctx or {})})

@router.get("/tenants", response_class=HTMLResponse)
def tenants_list(request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    tenants = db.query(Tenant).order_by(Tenant.created_at.desc()).all()
    return _render(request, "admin/tenants_list.html", {"tenants": tenants})

@router.get("/tenants/new", response_class=HTMLResponse)
def tenant_new_form(request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    return _render(request, "admin/tenant_new.html", {})

@router.post("/tenants/new")
def tenant_new(request: Request, slug: str = Form(...), name: str = Form(""), db: Session = Depends(get_db)):
    _require_admin(request)
    slug = slug.strip().lower()
    if not slug:
        raise HTTPException(400, "slug required")
    if db.query(Tenant).filter(Tenant.slug == slug).first():
        raise HTTPException(400, "slug already exists")
    t = Tenant(id=str(uuid.uuid4()), slug=slug, name=(name or slug.title()), status="active", created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    db.add(t)
    db.commit()

    # Create default trial subscription if plans exist
    plan = db.query(Plan).filter(Plan.code == "TRIAL_7D").first()
    if plan:
        sub = Subscription(
            id=str(uuid.uuid4()),
            tenant_id=t.id,
            plan_id=plan.id,
            status="active",
            starts_at=datetime.utcnow(),
            ends_at=datetime.utcnow() + timedelta(days=plan.duration_days),
            source="manual",
        )
        db.add(sub)
        db.commit()

    return RedirectResponse(url="/admin/tenants?admin_key=" + settings.ADMIN_KEY, status_code=303)

@router.get("/licensing", response_class=HTMLResponse)
def licensing_overview(request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    tenants = db.query(Tenant).order_by(Tenant.slug.asc()).all()
    plans = db.query(Plan).order_by(Plan.id.asc()).all()
    # latest subscription per tenant
    subs = {}
    for t in tenants:
        sub = db.query(Subscription).filter(Subscription.tenant_id == t.id).order_by(Subscription.starts_at.desc()).first()
        subs[t.id] = sub
    return _render(request, "admin/licensing.html", {"tenants": tenants, "plans": plans, "subs": subs})

@router.post("/licensing/generate")
def licensing_generate_code(
    request: Request,
    tenant_slug: str = Form(...),
    plan_code: str = Form(...),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    _require_admin(request)
    t = db.query(Tenant).filter(Tenant.slug == tenant_slug).first()
    if not t:
        raise HTTPException(404, "tenant not found")
    plan = db.query(Plan).filter(Plan.code == plan_code).first()
    if not plan:
        raise HTTPException(404, "plan not found")

    raw = f"{tenant_slug}-{uuid.uuid4().hex[:6]}-{uuid.uuid4().hex[:6]}".upper()
    code_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()

    ac = ActivationCode(
        id=str(uuid.uuid4()),
        tenant_id=t.id,
        plan_id=plan.id,
        code_hash=code_hash,
        issued_at=datetime.utcnow(),
        max_redemptions=1,
        redeemed_count=0,
        note=note or "",
    )
    db.add(ac)
    db.commit()

    # Show once page
    return _render(request, "admin/code_created.html", {"raw_code": raw, "tenant": t, "plan": plan})


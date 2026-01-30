from __future__ import annotations

import os
import secrets
import hashlib
import uuid
from datetime import datetime, timedelta
from typing import Optional

import bcrypt
import sqlalchemy as sa
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


def _flash_set(request: Request, key: str, value):
    s = request.scope.get("session")
    if isinstance(s, dict):
        s[key] = value


def _flash_pop(request: Request, key: str, default=None):
    s = request.scope.get("session")
    if isinstance(s, dict):
        return s.pop(key, default)
    return default


def _expected_admin_key() -> str:
    return (os.getenv("ADMIN_KEY") or "").strip()


def _session(request: Request) -> dict:
    s = request.scope.get("session")
    return s if isinstance(s, dict) else {}


def _get_admin_key_from_request(request: Request) -> str:
    """
    Order:
    1) Header X-Admin-Key
    2) Session 'admin_key'
    3) Query param '?admin_key=' (one-time bootstrap, optional)
    """
    hdr = (request.headers.get("X-Admin-Key") or request.headers.get("x-admin-key") or "").strip()
    if hdr:
        return hdr

    sess_key = (_session(request).get("admin_key") or "").strip()
    if sess_key:
        return sess_key

    # Optional one-time bootstrap fallback
    qp = (request.query_params.get("admin_key") or "").strip()
    if qp:
        return qp

    return ""


def _require_admin(request: Request) -> None:
    expected = _expected_admin_key()
    if not expected:
        raise HTTPException(status_code=403, detail="ADMIN_KEY not configured")
    got = _get_admin_key_from_request(request)
    if got != expected:
        raise HTTPException(status_code=403, detail="Forbidden")


def _set_admin_session_key(request: Request, key: str) -> None:
    s = request.scope.get("session")
    if isinstance(s, dict):
        s["admin_key"] = key


def _clear_admin_session_key(request: Request) -> None:
    s = request.scope.get("session")
    if isinstance(s, dict):
        s.pop("admin_key", None)


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


@router.get("/admin", response_class=HTMLResponse)
def admin_index(request: Request):
    base_url = _portal_base(request)
    sms_base = _sms_base()

    expected = _expected_admin_key()
    if not expected:
        return HTMLResponse("<h2>ADMIN_KEY not configured</h2>", status_code=403)

    got = _get_admin_key_from_request(request)
    authenticated = (got == expected)

    # If user passed ?admin_key=... and it is valid, store it once and redirect to tenants
    qp = (request.query_params.get("admin_key") or "").strip()
    if qp and qp == expected:
        _set_admin_session_key(request, qp)
        return RedirectResponse(url="/admin/tenants", status_code=303)

    # If already authenticated, go directly to tenants (better UX)
    if authenticated:
        # ensure session contains key so subsequent requests work
        if (_session(request).get("admin_key") or "").strip() != expected:
            _set_admin_session_key(request, expected)
        return RedirectResponse(url="/admin/tenants", status_code=303)

    return templates.TemplateResponse(
        "admin/index.html",
        {
            "request": request,
            "base_url": base_url,
            "sms_base": sms_base,
            "authenticated": False,
            "tenants_url": f"{base_url}/admin/tenants",
            "licensing_url": f"{base_url}/admin/licensing",
            "links_url": f"{base_url}/admin/links",
        },
    )


@router.post("/admin")
def admin_index_post(request: Request, admin_key: str = Form("")):
    expected = _expected_admin_key()
    if not expected:
        raise HTTPException(status_code=403, detail="ADMIN_KEY not configured")

    admin_key = (admin_key or "").strip()
    if admin_key != expected:
        return RedirectResponse(url="/admin?msg=invalid", status_code=303)

    _set_admin_session_key(request, admin_key)
    # ✅ Redirect to tenants so it feels like "unlock then enter admin"
    return RedirectResponse(url="/admin/tenants", status_code=303)


@router.get("/admin/logout")
def admin_logout(request: Request):
    _clear_admin_session_key(request)
    return RedirectResponse(url="/admin", status_code=303)


@router.get("/admin/tenants", response_class=HTMLResponse)
def admin_tenants(
    request: Request,
    q: str = "",
    status: str = "active",  # active|archived|deleted|all
    page: int = 1,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    _require_admin(request)
    base_url = _portal_base(request)

    page = max(1, int(page or 1))
    limit = int(limit or 50)
    if limit < 10:
        limit = 10
    if limit > 200:
        limit = 200
    offset = (page - 1) * limit

    from app.models.user import User

    role_rank = sa.case(
        (User.role == "owner", 0),
        (User.role == "admin", 1),
        else_=2,
    )

    owner_email_sq = (
        sa.select(User.email)
        .where(
            User.tenant_id == Tenant.id,
            User.is_active.is_(True),
            User.role.in_(["owner", "admin"]),
        )
        .order_by(role_rank.asc(), User.email.asc())
        .limit(1)
        .scalar_subquery()
    )

    query = db.query(Tenant, owner_email_sq.label("owner_email"))

    q_clean = (q or "").strip().lower()
    if q_clean:
        query = query.filter(
            sa.or_(
                sa.func.lower(Tenant.slug).contains(q_clean),
                sa.func.lower(Tenant.name).contains(q_clean),
                sa.func.lower(owner_email_sq).contains(q_clean),
            )
        )

    status = (status or "active").strip().lower()
    if status == "archived":
        query = query.filter(Tenant.deleted_at.is_(None), Tenant.is_archived.is_(True))
    elif status == "deleted":
        query = query.filter(Tenant.deleted_at.is_not(None))
    elif status == "all":
        pass
    else:
        query = query.filter(Tenant.deleted_at.is_(None), Tenant.is_archived.is_(False))

    total = query.count()

    rows = (
        query.order_by(Tenant.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )

    tenants = []
    for t, owner_email in rows:
        tenants.append(
            {
                "slug": t.slug,
                "name": t.name,
                "status": t.status,
                "is_archived": bool(getattr(t, "is_archived", False)),
                "deleted_at": getattr(t, "deleted_at", None),
                "owner_email": owner_email or "",
                "suite_url": f"{base_url}/t/{t.slug}/suite",
                "sms_url": _sms_url_for_tenant(t.slug),
            }
        )

    onboard = {
        "tenant_slug": _flash_pop(request, "onboard_tenant_slug", ""),
        "owner_email": _flash_pop(request, "onboard_owner_email", ""),
        "temp_password": _flash_pop(request, "onboard_temp_password", ""),
    }

    return templates.TemplateResponse(
        "admin/tenants.html",
        {
            "request": request,
            "tenants": tenants,
            "base_url": base_url,
            "onboard": onboard,
            "q": q_clean,
            "status": status,
            "page": page,
            "limit": limit,
            "total": total,
            "page_start": (offset + 1) if total else 0,
            "page_end": min(offset + limit, total),
            "pages": (total + limit - 1) // limit if limit else 1,
        },
    )


@router.get("/admin/tenants/new", response_class=HTMLResponse)
def admin_tenants_new(request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    ensure_default_plans(db)
    plans = db.query(Plan).order_by(Plan.duration_days.asc()).all()
    return templates.TemplateResponse("admin/tenant_new.html", {"request": request, "plans": plans})


@router.post("/admin/tenants/new")
def admin_tenants_create(
    request: Request,
    slug: str = Form(...),
    name: str = Form(...),
    owner_email: str = Form(...),
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

    owner_email = (owner_email or "").strip().lower()
    if not owner_email or "@" not in owner_email:
        raise HTTPException(400, "Owner email is required")

    try:
        t = Tenant(id=secrets.token_hex(16), slug=slug, name=name.strip(), status="active")
        if hasattr(t, "created_at"):
            t.created_at = datetime.utcnow()

        db.add(t)
        db.commit()
        db.refresh(t)

        db.add(ClinicSettings(tenant_id=t.id))
        db.commit()

        plan = db.query(Plan).filter(Plan.code == plan_code).first()
        if not plan:
            raise HTTPException(400, f"Unknown plan: {plan_code}")

        db.add(
            Subscription(
                id=secrets.token_hex(16),
                tenant_id=t.id,
                plan_id=plan.id,
                status="active",
                starts_at=datetime.utcnow(),
                ends_at=datetime.utcnow() + timedelta(days=int(plan.duration_days)),
                source="manual",
            )
        )

        from app.models.user import User

        temp_password = secrets.token_urlsafe(10)
        pw_hash = bcrypt.hashpw(temp_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

        owner = User(
            id=str(uuid.uuid4()),
            tenant_id=t.id,
            email=owner_email,
            password_hash=pw_hash,
            role="owner",
            is_active=True,
            must_reset_password=True,
        )
        db.add(owner)

        db.add(
            LicenseAuditLog(
                id=secrets.token_hex(16),
                tenant_id=t.id,
                event_type="tenant_onboarded",
                details_json=f'{{"slug":"{slug}","plan":"{plan.code}","owner":"{owner_email}"}}',
                created_at=datetime.utcnow(),
            )
        )

        db.commit()

        _flash_set(request, "onboard_tenant_slug", slug)
        _flash_set(request, "onboard_owner_email", owner_email)
        _flash_set(request, "onboard_temp_password", temp_password)

        return RedirectResponse(url="/admin/tenants", status_code=303)

    except HTTPException:
        db.rollback()
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Create tenant failed: {type(e).__name__}: {e}")


@router.post("/admin/tenants/bulk")
def admin_tenants_bulk(
    request: Request,
    action: str = Form(...),
    slugs: list[str] = Form([]),
    db: Session = Depends(get_db),
):
    _require_admin(request)
    action = (action or "").strip().lower()
    slugs = [s.strip().lower() for s in (slugs or []) if s and s.strip()]
    if not slugs:
        raise HTTPException(400, "No tenants selected")

    now = datetime.utcnow()
    admin_actor = (_session(request).get("email") or "admin")

    tenants = db.query(Tenant).filter(Tenant.slug.in_(slugs)).all()
    if not tenants:
        raise HTTPException(404, "Tenants not found")

    if action == "archive":
        for t in tenants:
            if t.deleted_at is None:
                t.is_archived = True
                t.archived_at = now
    elif action == "unarchive":
        for t in tenants:
            if t.deleted_at is None:
                t.is_archived = False
                t.archived_at = None
    elif action == "delete":
        for t in tenants:
            t.deleted_at = now
            t.deleted_by = str(admin_actor)
            t.is_archived = True
            t.archived_at = now
    elif action == "restore":
        for t in tenants:
            t.deleted_at = None
            t.deleted_by = None
    else:
        raise HTTPException(400, "Invalid action")

    db.commit()
    return RedirectResponse(url="/admin/tenants", status_code=303)


@router.post("/admin/tenants/{slug}/archive")
def admin_tenant_archive(request: Request, slug: str, db: Session = Depends(get_db)):
    _require_admin(request)
    t = db.query(Tenant).filter(Tenant.slug == slug).first()
    if not t:
        raise HTTPException(404, "Tenant not found")
    if t.deleted_at is None:
        t.is_archived = True
        t.archived_at = datetime.utcnow()
        db.commit()
    return RedirectResponse(url="/admin/tenants?status=active", status_code=303)


@router.post("/admin/tenants/{slug}/unarchive")
def admin_tenant_unarchive(request: Request, slug: str, db: Session = Depends(get_db)):
    _require_admin(request)
    t = db.query(Tenant).filter(Tenant.slug == slug).first()
    if not t:
        raise HTTPException(404, "Tenant not found")
    if t.deleted_at is None:
        t.is_archived = False
        t.archived_at = None
        db.commit()
    return RedirectResponse(url="/admin/tenants?status=archived", status_code=303)


@router.post("/admin/tenants/{slug}/delete")
def admin_tenant_delete(request: Request, slug: str, db: Session = Depends(get_db)):
    _require_admin(request)
    t = db.query(Tenant).filter(Tenant.slug == slug).first()
    if not t:
        raise HTTPException(404, "Tenant not found")
    now = datetime.utcnow()
    actor = (_session(request).get("email") or "admin")
    t.deleted_at = now
    t.deleted_by = str(actor)
    t.is_archived = True
    t.archived_at = now
    db.commit()
    return RedirectResponse(url="/admin/tenants?status=active", status_code=303)


@router.post("/admin/tenants/{slug}/restore")
def admin_tenant_restore(request: Request, slug: str, db: Session = Depends(get_db)):
    _require_admin(request)
    t = db.query(Tenant).filter(Tenant.slug == slug).first()
    if not t:
        raise HTTPException(404, "Tenant not found")
    t.deleted_at = None
    t.deleted_by = None
    db.commit()
    return RedirectResponse(url="/admin/tenants?status=deleted", status_code=303)


@router.get("/admin/licensing", response_class=HTMLResponse)
def admin_licensing(request: Request, tenant: Optional[str] = None, db: Session = Depends(get_db)):
    _require_admin(request)
    ensure_default_plans(db)

    tenants = db.query(Tenant).filter(Tenant.deleted_at.is_(None)).order_by(Tenant.slug.asc()).all()
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
        {"request": request, "tenants": tenants, "plans": plans, "selected": selected, "current_sub": current_sub},
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

    return RedirectResponse(url=f"/admin/licensing?tenant={tenant_slug}&code={raw}", status_code=303)


@router.get("/admin/links", response_class=HTMLResponse)
def admin_links(request: Request, db: Session = Depends(get_db)):
    _require_admin(request)

    base_url = _portal_base(request)
    tenants = db.query(Tenant).filter(Tenant.deleted_at.is_(None)).order_by(Tenant.slug.asc()).all()

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
            "base_url": base_url,
            "sms_base": _sms_base(),
            "admin_tenants_url": f"{base_url}/admin/tenants",
            "admin_licensing_url": f"{base_url}/admin/licensing",
            "admin_links_url": f"{base_url}/admin/links",
            "admin_key": "",
            "tenants": rows,
        },
    )

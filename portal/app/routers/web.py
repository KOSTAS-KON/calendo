"""
Portal web routes (HTML pages + internal JSON endpoints).

Notes:
- Keep these routes tolerant to missing query params and older link formats.
- Prefer redirecting to canonical tenant-scoped URLs under /t/{tenant_slug}/...
- Internal endpoints are protected by INTERNAL_API_KEY / PORTAL_INTERNAL_KEY (when set).
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer
from jinja2 import TemplateNotFound
from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Appointment, Child, ClinicSettings, LicenseAuditLog, SmsOutbox, Subscription, Therapist
from app.tenant import resolve_tenant

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# -----------------------------
# Helpers
# -----------------------------

def _rp(request: Request) -> str:
    rp = request.scope.get("root_path") or ""
    return rp.rstrip("/")


def _bool_from_form(v: Optional[str]) -> bool:
    # HTML checkbox returns "on" or value; missing -> None
    if v is None:
        return False
    return str(v).strip().lower() in {"1", "true", "on", "yes"}


def _internal_key_expected() -> str:
    return (
        os.getenv("INTERNAL_API_KEY")
        or os.getenv("PORTAL_INTERNAL_KEY")
        or os.getenv("INTERNAL_KEY")
        or ""
    ).strip()


def _require_internal_key(request: Request) -> None:
    expected = _internal_key_expected()
    if not expected:
        return

    got = (
        request.headers.get("x-internal-key")
        or request.headers.get("X-Internal-Key")
        or request.query_params.get("internal_key")
        or request.query_params.get("key")
        or ""
    ).strip()

    if got != expected:
        raise HTTPException(status_code=403, detail="Forbidden")


def _infobip_token_expected() -> str:
    # Optional extra guard for endpoints that expose Infobip credentials.
    return (os.getenv("INTERNAL_TOKEN") or "").strip()


def _require_infobip_token(request: Request) -> None:
    expected = _infobip_token_expected()
    if not expected:
        # If not configured, fall back to internal key
        _require_internal_key(request)
        return

    got = (
        request.headers.get("x-internal-token")
        or request.headers.get("X-Internal-Token")
        or request.query_params.get("internal_token")
        or ""
    ).strip()

    if got != expected:
        raise HTTPException(status_code=403, detail="Forbidden")


def _sms_base_url() -> str:
    # Render service for SMS calendar (separate service)
    url = (os.getenv("SMS_APP_URL") or os.getenv("CALENDO_SMS_URL") or "").strip()
    return url.rstrip("/")


def _sms_sso_secret() -> str:
    return (os.getenv("SMS_SSO_SECRET") or os.getenv("CALENDO_SMS_SSO_SECRET") or "").strip()


def _make_sms_sso(tenant_slug: str) -> str:
    secret = _sms_sso_secret()
    if not secret:
        return ""
    s = URLSafeTimedSerializer(secret_key=secret, salt="calendo-sms-sso-v1")
    return s.dumps({"tenant": tenant_slug})


def _sms_calendar_url(tenant_slug: str) -> str:
    base = _sms_base_url()
    if not base:
        return ""
    sso = _make_sms_sso(tenant_slug)
    # Keep the path flexible (SMS app may run at / or /sms)
    return f"{base}?tenant={quote_plus(tenant_slug)}&sso={quote_plus(sso)}"


def _tenant_slug_param(request: Request, explicit: Optional[str]) -> Optional[str]:
    if explicit:
        return explicit
    return request.query_params.get("tenant") or request.query_params.get("t")


def _login_redirect(request: Request) -> RedirectResponse:
    rp = _rp(request)
    nxt = request.url.path
    if request.url.query:
        nxt = f"{nxt}?{request.url.query}"
    return RedirectResponse(url=f"{rp}/auth/login?next={quote_plus(nxt)}", status_code=303)


def _require_login(db: Session, request: Request, tenant_slug: Optional[str]) -> Any:
    tctx = resolve_tenant(db, request, tenant_slug=tenant_slug)
    if not getattr(tctx, "user", None):
        raise HTTPException(status_code=401, detail="Login required")
    return tctx


def _require_login_or_redirect(db: Session, request: Request, tenant_slug: Optional[str]):
    tctx = resolve_tenant(db, request, tenant_slug=tenant_slug)
    if not getattr(tctx, "user", None):
        return tctx, _login_redirect(request)
    return tctx, None


def _get_or_create_clinic_settings(db: Session, tenant_id: int) -> ClinicSettings:
    cs = db.query(ClinicSettings).filter_by(tenant_id=tenant_id).one_or_none()
    if cs is None:
        cs = ClinicSettings(tenant_id=tenant_id)
        db.add(cs)
        db.commit()
        db.refresh(cs)
    return cs


def _license_status(db: Session, tenant_id: int) -> Dict[str, Any]:
    """
    Best-effort license/trial status used by Suite/Settings.
    Keep tolerant if Subscription model differs.
    """
    sub = None
    try:
        sub = (
            db.query(Subscription)
            .filter_by(tenant_id=tenant_id)
            .order_by(Subscription.created_at.desc())
            .first()
        )
    except Exception:
        sub = None

    now = datetime.utcnow()
    # If we have a subscription with an ends_at, consider it active if ends_at in future.
    if sub is not None and hasattr(sub, "ends_at") and getattr(sub, "ends_at"):
        ends_at = getattr(sub, "ends_at")
        return {
            "mode": "LICENSE",
            "active": bool(ends_at and ends_at > now),
            "ends_at": ends_at,
        }

    # Otherwise, fall back to last audit log event for visibility.
    last = None
    try:
        last = (
            db.query(LicenseAuditLog)
            .filter_by(tenant_id=tenant_id)
            .order_by(LicenseAuditLog.created_at.desc())
            .first()
        )
    except Exception:
        last = None

    return {
        "mode": "TRIAL" if last is None else "UNKNOWN",
        "active": True if last is None else True,
        "ends_at": None,
        "last_event": getattr(last, "event", None) if last else None,
        "last_at": getattr(last, "created_at", None) if last else None,
    }


def _base_context(db: Session, request: Request, tenant_slug: Optional[str]) -> Dict[str, Any]:
    rp = _rp(request)
    tctx = resolve_tenant(db, request, tenant_slug=tenant_slug)
    cs = _get_or_create_clinic_settings(db, tctx.tenant_id)
    lic = _license_status(db, tctx.tenant_id)
    return {
        "request": request,
        "rp": rp,
        "tenant_slug": tctx.tenant_slug,
        "tenant_id": tctx.tenant_id,
        "user": getattr(tctx, "user", None),
        "user_email": getattr(tctx, "user_email", None),
        "clinic": cs,
        "license": lic,
        "sms_app_url": _sms_calendar_url(tctx.tenant_slug),
        "sms_sso": _make_sms_sso(tctx.tenant_slug),
        "now": datetime.utcnow(),
    }


def _render(request: Request, template_name: str, ctx: Dict[str, Any]) -> HTMLResponse:
    try:
        return templates.TemplateResponse(template_name, ctx)
    except TemplateNotFound:
        # Minimal fallback for safety; avoids hard 500 on missing templates.
        body = f"<h1>Template not found</h1><p>{template_name}</p>"
        return HTMLResponse(content=body, status_code=200)


# -----------------------------
# Public HTML pages
# -----------------------------

@router.get("/suite", response_class=HTMLResponse)
def suite_alias(request: Request, tenant: Optional[str] = None, db: Session = Depends(get_db)):
    # Compatibility alias used by older links and some SMS app builds.
    tenant_slug = _tenant_slug_param(request, tenant)
    if tenant_slug:
        return RedirectResponse(url=f"{_rp(request)}/t/{tenant_slug}/suite", status_code=303)

    # If logged in, try to resolve default tenant and redirect.
    tctx = resolve_tenant(db, request, tenant_slug=None)
    if getattr(tctx, "tenant_slug", None):
        return RedirectResponse(url=f"{_rp(request)}/t/{tctx.tenant_slug}/suite", status_code=303)

    # Not logged in: go to login.
    return _login_redirect(request)



@router.get("/therapy/", response_class=HTMLResponse)
def therapy_root_alias(request: Request, tenant: Optional[str] = None, db: Session = Depends(get_db)):
    # Compatibility path (older reverse-proxy deployments).
    return suite_alias(request=request, tenant=tenant, db=db)

@router.get("/therapy/suite", response_class=HTMLResponse)
def therapy_suite_alias(request: Request, tenant: Optional[str] = None, db: Session = Depends(get_db)):
    return suite_alias(request=request, tenant=tenant, db=db)

@router.get("/outbox", response_class=HTMLResponse)
def outbox_alias(request: Request, tenant: Optional[str] = None):
    # Some UIs used /outbox for the SMS outbox list.
    tenant_slug = _tenant_slug_param(request, tenant)
    url = f"{_rp(request)}/sms-outbox"
    if tenant_slug:
        url += f"?tenant={quote_plus(tenant_slug)}"
    return RedirectResponse(url=url, status_code=303)

@router.get("/t/{tenant_slug}/suite", response_class=HTMLResponse)
def suite_page(
    request: Request,
    tenant_slug: str,
    tab: str = Query(default="overview"),
    db: Session = Depends(get_db),
):
    tctx, redirect = _require_login_or_redirect(db, request, tenant_slug)
    if redirect:
        return redirect

    ctx = _base_context(db, request, tenant_slug)
    ctx["active_tab"] = tab
    return _render(request, "pages/suite.html", ctx)


@router.get("/settings", response_class=HTMLResponse)
def settings_page(
    request: Request,
    tenant: Optional[str] = None,
    tab: str = Query(default="overview"),
    db: Session = Depends(get_db),
):
    tenant_slug = _tenant_slug_param(request, tenant)
    tctx, redirect = _require_login_or_redirect(db, request, tenant_slug)
    if redirect:
        return redirect

    ctx = _base_context(db, request, tctx.tenant_slug)
    ctx["active_tab"] = tab
    return _render(request, "pages/settings.html", ctx)


@router.get("/setup", response_class=HTMLResponse)
def setup_alias(
    request: Request,
    tenant: Optional[str] = None,
    tab: str = Query(default="overview"),
    db: Session = Depends(get_db),
):
    # Older links used /setup
    return settings_page(request=request, tenant=tenant, tab=tab, db=db)


@router.post("/settings/clinic")
def update_clinic_settings(
    request: Request,
    tenant: Optional[str] = None,
    clinic_name: str = Form(default=""),
    timezone: str = Form(default="UTC"),
    locale: str = Form(default="en"),
    sms_sender_id: str = Form(default=""),
    enable_24h: Optional[str] = Form(default=None),
    enable_2h: Optional[str] = Form(default=None),
    reminder_hours_24: int = Form(default=24),
    reminder_hours_2: int = Form(default=2),
    db: Session = Depends(get_db),
):
    tenant_slug = _tenant_slug_param(request, tenant)
    tctx, redirect = _require_login_or_redirect(db, request, tenant_slug)
    if redirect:
        return redirect

    cs = _get_or_create_clinic_settings(db, tctx.tenant_id)

    # Assign only if the model actually has those attributes.
    if hasattr(cs, "clinic_name"):
        cs.clinic_name = clinic_name.strip()
    if hasattr(cs, "timezone"):
        cs.timezone = timezone.strip() or "UTC"
    if hasattr(cs, "locale"):
        cs.locale = locale.strip() or "en"
    if hasattr(cs, "sms_sender_id"):
        cs.sms_sender_id = sms_sender_id.strip()
    if hasattr(cs, "enable_24h"):
        cs.enable_24h = _bool_from_form(enable_24h)
    if hasattr(cs, "enable_2h"):
        cs.enable_2h = _bool_from_form(enable_2h)
    if hasattr(cs, "reminder_hours_24"):
        cs.reminder_hours_24 = int(reminder_hours_24)
    if hasattr(cs, "reminder_hours_2"):
        cs.reminder_hours_2 = int(reminder_hours_2)

    db.add(cs)
    db.commit()

    return RedirectResponse(url=f"{_rp(request)}/settings?tenant={tctx.tenant_slug}&tab=clinic", status_code=303)


@router.post("/settings/infobib")  # common typo alias
@router.post("/settings/infobip")
def update_infobip_settings(
    request: Request,
    tenant: Optional[str] = None,
    infobip_base_url: str = Form(default=""),
    infobip_api_key: str = Form(default=""),
    infobip_sender: str = Form(default=""),
    db: Session = Depends(get_db),
):
    tenant_slug = _tenant_slug_param(request, tenant)
    tctx, redirect = _require_login_or_redirect(db, request, tenant_slug)
    if redirect:
        return redirect

    cs = _get_or_create_clinic_settings(db, tctx.tenant_id)

    if hasattr(cs, "infobip_base_url"):
        cs.infobip_base_url = infobip_base_url.strip()
    if hasattr(cs, "infobip_api_key"):
        cs.infobip_api_key = infobip_api_key.strip()
    if hasattr(cs, "infobip_sender"):
        cs.infobip_sender = infobip_sender.strip()

    db.add(cs)
    db.commit()

    return RedirectResponse(url=f"{_rp(request)}/settings?tenant={tctx.tenant_slug}&tab=infobip", status_code=303)


@router.get("/children", response_class=HTMLResponse)
def children_list(
    request: Request,
    tenant: Optional[str] = None,
    q: str = Query(default=""),
    db: Session = Depends(get_db),
):
    tenant_slug = _tenant_slug_param(request, tenant)
    tctx, redirect = _require_login_or_redirect(db, request, tenant_slug)
    if redirect:
        return redirect

    query = db.query(Child).filter_by(tenant_id=tctx.tenant_id)
    if q:
        # Child model typically has full_name; if not, fall back.
        if hasattr(Child, "full_name"):
            query = query.filter(Child.full_name.ilike(f"%{q}%"))
    children = query.order_by(getattr(Child, "full_name", Child.id)).all()

    ctx = _base_context(db, request, tctx.tenant_slug)
    ctx.update({"children": children, "q": q})
    return _render(request, "pages/children_list.html", ctx)


@router.get("/children/create", response_class=HTMLResponse)
def children_create_get(
    request: Request,
    tenant: Optional[str] = None,
    db: Session = Depends(get_db),
):
    # Compatibility: some UIs link to /children/create (GET). Redirect to /children
    tenant_slug = _tenant_slug_param(request, tenant)
    return RedirectResponse(url=f"{_rp(request)}/children?tenant={quote_plus(tenant_slug or '')}", status_code=303)


@router.post("/children/create")
def children_create_post(
    request: Request,
    tenant: Optional[str] = None,
    full_name: str = Form(...),
    db: Session = Depends(get_db),
):
    tenant_slug = _tenant_slug_param(request, tenant)
    tctx, redirect = _require_login_or_redirect(db, request, tenant_slug)
    if redirect:
        return redirect

    name = full_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="full_name required")

    child = Child(tenant_id=tctx.tenant_id, full_name=name)
    db.add(child)
    db.commit()
    db.refresh(child)

    return RedirectResponse(url=f"{_rp(request)}/children?tenant={tctx.tenant_slug}", status_code=303)


@router.get("/children/{child_id}", response_class=HTMLResponse)
def child_detail(
    request: Request,
    child_id: int,
    tenant: Optional[str] = None,
    db: Session = Depends(get_db),
):
    tenant_slug = _tenant_slug_param(request, tenant)
    tctx, redirect = _require_login_or_redirect(db, request, tenant_slug)
    if redirect:
        return redirect

    child = db.query(Child).filter_by(id=child_id, tenant_id=tctx.tenant_id).one_or_none()
    if child is None:
        raise HTTPException(status_code=404, detail="Child not found")

    # Keep details minimal; templates may enrich via other routers.
    ctx = _base_context(db, request, tctx.tenant_slug)
    ctx.update({"child": child})
    return _render(request, "pages/child_detail.html", ctx)


@router.get("/therapists", response_class=HTMLResponse)
def therapists_list(
    request: Request,
    tenant: Optional[str] = None,
    q: str = Query(default=""),
    db: Session = Depends(get_db),
):
    tenant_slug = _tenant_slug_param(request, tenant)
    tctx, redirect = _require_login_or_redirect(db, request, tenant_slug)
    if redirect:
        return redirect

    query = db.query(Therapist).filter_by(tenant_id=tctx.tenant_id)
    if q and hasattr(Therapist, "full_name"):
        query = query.filter(Therapist.full_name.ilike(f"%{q}%"))
    therapists = query.order_by(getattr(Therapist, "full_name", Therapist.id)).all()

    ctx = _base_context(db, request, tctx.tenant_slug)
    ctx.update({"therapists": therapists, "q": q})
    return _render(request, "pages/therapists_list.html", ctx)


@router.post("/therapists/create")
def therapists_create(
    request: Request,
    tenant: Optional[str] = None,
    full_name: str = Form(...),
    db: Session = Depends(get_db),
):
    tenant_slug = _tenant_slug_param(request, tenant)
    tctx, redirect = _require_login_or_redirect(db, request, tenant_slug)
    if redirect:
        return redirect

    name = full_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="full_name required")

    th = Therapist(tenant_id=tctx.tenant_id, full_name=name)
    db.add(th)
    db.commit()
    return RedirectResponse(url=f"{_rp(request)}/therapists?tenant={tctx.tenant_slug}", status_code=303)


@router.get("/billing", response_class=HTMLResponse)
def billing_page(
    request: Request,
    tenant: Optional[str] = None,
    db: Session = Depends(get_db),
):
    # Minimal page so old links don't 404. Real billing UI may live elsewhere.
    tenant_slug = _tenant_slug_param(request, tenant)
    tctx, redirect = _require_login_or_redirect(db, request, tenant_slug)
    if redirect:
        return redirect

    ctx = _base_context(db, request, tctx.tenant_slug)
    return _render(request, "pages/billing.html", ctx)


@router.get("/sms-outbox", response_class=HTMLResponse)
def sms_outbox_page(
    request: Request,
    tenant: Optional[str] = None,
    db: Session = Depends(get_db),
):
    tenant_slug = _tenant_slug_param(request, tenant)
    tctx, redirect = _require_login_or_redirect(db, request, tenant_slug)
    if redirect:
        return redirect

    msgs = (
        db.query(SmsOutbox)
        .filter_by(tenant_id=tctx.tenant_id)
        .order_by(getattr(SmsOutbox, "created_at", SmsOutbox.id).desc())
        .limit(200)
        .all()
    )

    ctx = _base_context(db, request, tctx.tenant_slug)
    ctx.update({"messages": msgs})
    return _render(request, "pages/sms_outbox.html", ctx)


@router.post("/sms-outbox/delete")
def sms_outbox_delete(
    request: Request,
    tenant: Optional[str] = None,
    msg_id: int = Form(...),
    db: Session = Depends(get_db),
):
    tenant_slug = _tenant_slug_param(request, tenant)
    tctx, redirect = _require_login_or_redirect(db, request, tenant_slug)
    if redirect:
        return redirect

    msg = db.query(SmsOutbox).filter_by(id=msg_id, tenant_id=tctx.tenant_id).one_or_none()
    if msg is not None:
        db.delete(msg)
        db.commit()

    return RedirectResponse(url=f"{_rp(request)}/sms-outbox?tenant={tctx.tenant_slug}", status_code=303)


# -----------------------------
# Public JSON (safe)
# -----------------------------

@router.get("/api/clinic_settings")
def api_clinic_settings_public(
    request: Request,
    tenant: str = Query(...),
    db: Session = Depends(get_db),
):
    # Public read (no secrets)
    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    cs = _get_or_create_clinic_settings(db, tctx.tenant_id)

    payload: Dict[str, Any] = {}
    for k in ["clinic_name", "timezone", "locale", "sms_sender_id", "enable_24h", "enable_2h", "reminder_hours_24", "reminder_hours_2"]:
        if hasattr(cs, k):
            payload[k] = getattr(cs, k)
    return payload


# -----------------------------
# Internal JSON endpoints
# -----------------------------

@router.get("/api/internal/clinic_settings")
@router.get("/api/internal/clinic-settings")
def api_internal_clinic_settings_get(
    request: Request,
    tenant: str = Query(...),
    db: Session = Depends(get_db),
):
    _require_internal_key(request)
    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    cs = _get_or_create_clinic_settings(db, tctx.tenant_id)

    payload: Dict[str, Any] = {}
    # Include Infobip fields too in internal view (may be needed by SMS service).
    for k in [
        "clinic_name",
        "timezone",
        "locale",
        "sms_sender_id",
        "enable_24h",
        "enable_2h",
        "reminder_hours_24",
        "reminder_hours_2",
        "infobip_base_url",
        "infobip_sender",
    ]:
        if hasattr(cs, k):
            payload[k] = getattr(cs, k)
    return payload


@router.post("/api/internal/clinic_settings")
@router.post("/api/internal/clinic-settings")
async def api_internal_clinic_settings_set(
    request: Request,
    tenant: str = Query(...),
    db: Session = Depends(get_db),
):
    _require_internal_key(request)
    data = await request.json()
    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    cs = _get_or_create_clinic_settings(db, tctx.tenant_id)

    # Update allowed keys only.
    allowed = {
        "clinic_name",
        "timezone",
        "locale",
        "sms_sender_id",
        "enable_24h",
        "enable_2h",
        "reminder_hours_24",
        "reminder_hours_2",
    }
    for k, v in (data or {}).items():
        if k in allowed and hasattr(cs, k):
            setattr(cs, k, v)

    db.add(cs)
    db.commit()
    return {"ok": True}


@router.get("/api/internal/infobip")
async def api_internal_infobip_get(
    request: Request,
    tenant: str = Query(...),
    db: Session = Depends(get_db),
):
    _require_infobip_token(request)
    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    cs = _get_or_create_clinic_settings(db, tctx.tenant_id)

    payload: Dict[str, Any] = {}
    for k in ["infobip_base_url", "infobip_sender", "infobip_api_key"]:
        if hasattr(cs, k):
            payload[k] = getattr(cs, k)
    return payload


@router.post("/api/internal/infobip")
async def api_internal_infobip_set(
    request: Request,
    tenant: str = Query(...),
    db: Session = Depends(get_db),
):
    _require_infobip_token(request)
    data = await request.json()
    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    cs = _get_or_create_clinic_settings(db, tctx.tenant_id)

    for k in ["infobip_base_url", "infobip_sender", "infobip_api_key"]:
        if k in (data or {}) and hasattr(cs, k):
            setattr(cs, k, (data or {}).get(k) or "")

    db.add(cs)
    db.commit()
    return {"ok": True}


@router.get("/api/license")
def api_license_status(
    request: Request,
    tenant: str = Query(...),
    db: Session = Depends(get_db),
):
    # Used by SMS app diagnostics / UI.
    _require_internal_key(request)
    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    return _license_status(db, tctx.tenant_id)


@router.get("/api/internal/children")
def api_internal_children(
    request: Request,
    tenant: str = Query(...),
    q: str = Query(default=""),
    db: Session = Depends(get_db),
):
    _require_internal_key(request)
    tctx = resolve_tenant(db, request, tenant_slug=tenant)

    query = db.query(Child).filter_by(tenant_id=tctx.tenant_id)
    if q and hasattr(Child, "full_name"):
        query = query.filter(Child.full_name.ilike(f"%{q}%"))
    children = query.order_by(getattr(Child, "full_name", Child.id)).all()

    out: List[Dict[str, Any]] = []
    for c in children:
        out.append(
            {
                "id": getattr(c, "id", None),
                "full_name": getattr(c, "full_name", None),
            }
        )
    return {"children": out}


@router.post("/api/internal/children")
async def api_internal_children_create(
    request: Request,
    tenant: str = Query(...),
    db: Session = Depends(get_db),
):
    _require_internal_key(request)
    data = await request.json()
    tctx = resolve_tenant(db, request, tenant_slug=tenant)

    full_name = (data or {}).get("full_name") or (data or {}).get("name") or ""
    full_name = str(full_name).strip()
    if not full_name:
        raise HTTPException(status_code=400, detail="full_name required")

    child = Child(tenant_id=tctx.tenant_id, full_name=full_name)

    # Best-effort optional fields (only if model supports them).
    for attr, key in [
        ("phone", "phone"),
        ("phone_number", "phone"),
        ("parent_phone", "phone"),
        ("guardian_phone", "phone"),
        ("notes", "notes"),
    ]:
        if key in (data or {}) and hasattr(child, attr):
            setattr(child, attr, (data or {}).get(key))

    db.add(child)
    db.commit()
    db.refresh(child)

    return {"ok": True, "id": getattr(child, "id", None)}


@router.get("/api/internal/appointments")
def api_internal_appointments(
    request: Request,
    tenant: str = Query(...),
    days: int = Query(default=60, ge=1, le=365),
    db: Session = Depends(get_db),
):
    _require_internal_key(request)
    tctx = resolve_tenant(db, request, tenant_slug=tenant)

    start = datetime.utcnow() - timedelta(days=1)
    end = datetime.utcnow() + timedelta(days=days)

    q = db.query(Appointment).filter(
        and_(
            Appointment.tenant_id == tctx.tenant_id,
            Appointment.starts_at >= start,
            Appointment.starts_at <= end,
        )
    )

    appts = q.order_by(Appointment.starts_at.asc()).all()

    out: List[Dict[str, Any]] = []
    for a in appts:
        # Child may be nullable.
        child_id = getattr(a, "child_id", None)
        child_name = None
        if child_id:
            c = db.query(Child).filter_by(id=child_id, tenant_id=tctx.tenant_id).one_or_none()
            child_name = getattr(c, "full_name", None) if c else None

        out.append(
            {
                "id": getattr(a, "id", None),
                "child_id": child_id,
                "child_name": child_name,
                "starts_at": getattr(a, "starts_at", None).isoformat() if getattr(a, "starts_at", None) else None,
                "ends_at": getattr(a, "ends_at", None).isoformat() if getattr(a, "ends_at", None) else None,
                "therapist_name": getattr(a, "therapist_name", None),
                "procedure": getattr(a, "procedure", None),
                "attendance_status": getattr(a, "attendance_status", None),
            }
        )

    return {"appointments": out}
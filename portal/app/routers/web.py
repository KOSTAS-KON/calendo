from __future__ import annotations

import os
from datetime import datetime
import uuid
from urllib.parse import quote_plus, quote

from fastapi import APIRouter, Depends, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app.db import get_db
from app.config import settings
from app.tenancy import resolve_tenant

from app.models.child import Child
from app.models.therapist import Therapist
from app.models.appointment import Appointment
from app.models.clinic_settings import ClinicSettings, AppLicense
from app.models.sms_outbox import SmsOutbox

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _rp(request: Request) -> str:
    return request.scope.get("root_path", "") or ""


def _session(request: Request) -> dict:
    s = request.scope.get("session")
    return s if isinstance(s, dict) else {}


def _sso_serializer() -> URLSafeTimedSerializer:
    secret = (settings.SSO_SHARED_SECRET or settings.SECRET_KEY or "").strip()
    return URLSafeTimedSerializer(secret_key=secret, salt="calendo-sms-sso-v1")


def _make_sms_sso_token(request: Request, tenant_slug: str) -> str:
    s = _session(request)
    payload = {
        "tenant": (tenant_slug or "default").strip().lower(),
        "user_id": s.get("user_id"),
        "role": s.get("role"),
        "email": s.get("email"),
    }
    return _sso_serializer().dumps(payload)


def _require_login_for_tenant(request: Request, tenant_slug: str) -> RedirectResponse | None:
    """
    Enforce that the user is logged in and belongs to this tenant.

    Returns a RedirectResponse if access should be denied, else None.
    """
    s = _session(request)
    user_id = s.get("user_id")
    sess_tenant = (s.get("tenant_slug") or "default").strip().lower()
    tenant_slug = (tenant_slug or "default").strip().lower()

    if not user_id:
        # Not logged in -> go to login and return here
        rp = _rp(request)
        return RedirectResponse(url=f"{rp}/login?next=/t/{tenant_slug}/suite", status_code=303)

    # Logged in but for a different tenant
    if sess_tenant != tenant_slug:
        rp = _rp(request)
        return RedirectResponse(url=f"{rp}/login?next=/t/{tenant_slug}/suite", status_code=303)

    return None


def _require_internal(request: Request) -> None:
    """Internal calls from SMS app.

    Accept either:
      - X-Internal-Key matches INTERNAL_API_KEY (preferred)
      - x-internal-token matches SECRET_KEY (legacy)
    """
    hdr_new = (request.headers.get("x-internal-key") or request.headers.get("X-Internal-Key") or "").strip()
    hdr_old = (request.headers.get("x-internal-token") or "").strip()

    ok = False
    expected_new = (settings.INTERNAL_API_KEY or "").strip()
    expected_old = (settings.SECRET_KEY or "").strip()

    if expected_new and hdr_new and hdr_new == expected_new:
        ok = True
    if expected_old and hdr_old and hdr_old == expected_old:
        ok = True

    if not ok:
        raise HTTPException(status_code=403, detail="Forbidden")


def _get_settings(db: Session, tenant_id: str) -> ClinicSettings:
    cs = db.query(ClinicSettings).filter(ClinicSettings.tenant_id == tenant_id).first()
    if not cs:
        cs = ClinicSettings(tenant_id=tenant_id)
        db.add(cs)
        db.commit()
        db.refresh(cs)
    return cs


def _get_license(db: Session) -> AppLicense:
    lic = db.get(AppLicense, 1)
    if not lic:
        lic = AppLicense(id=1, product_mode="BOTH")
        db.add(lic)
        db.commit()
        db.refresh(lic)
    return lic


def _render(request: Request, template_name: str, ctx: dict, db: Session, tenant_slug: str):
    tctx = resolve_tenant(db, request, tenant_slug=tenant_slug)
    cs = _get_settings(db, tctx.tenant_id)
    lic = _get_license(db)

    sms_url = (settings.SMS_APP_URL or "").strip() or "/sms"
    if sms_url.endswith("/"):
        sms_url = sms_url[:-1]

    # Short-lived SSO token for the SMS app (prevents direct URL access without auth)
    sso = _make_sms_sso_token(request, tctx.tenant_slug)

    # Render deployments often mount Streamlit at /sms/ with a route under /sms
    # Keep compatibility with both on-prem gateway and Render paths.
    if "onrender.com" in sms_url:
        sms_link = f"{sms_url}/sms?tenant={tctx.tenant_slug}&sso={sso}"
    else:
        sms_link = f"{sms_url}?tenant={tctx.tenant_slug}&sso={sso}"

    base = {
        "request": request,
        "tenant_slug": tctx.tenant_slug,
        "tenant_name": tctx.tenant_name,
        "clinic": cs,
        "license": lic,
        "sms_app_url": sms_link,
    }
    base.update(ctx or {})
    return templates.TemplateResponse(template_name, base)


@router.get("/suite", response_class=HTMLResponse)
def suite_default(request: Request, db: Session = Depends(get_db)):
    # Require login for default tenant
    redirect = _require_login_for_tenant(request, "default")
    if redirect:
        return redirect
    return _render(request, "pages/suite.html", {}, db, tenant_slug="default")


@router.get("/t/{tenant_slug}/suite", response_class=HTMLResponse)
def suite_tenant(request: Request, tenant_slug: str, db: Session = Depends(get_db)):
    redirect = _require_login_for_tenant(request, tenant_slug)
    if redirect:
        return redirect
    return _render(request, "pages/suite.html", {}, db, tenant_slug=tenant_slug)


@router.get("/sms-outbox", response_class=HTMLResponse)
def sms_outbox_view(request: Request, tenant: str = "default", db: Session = Depends(get_db)):
    redirect = _require_login_for_tenant(request, tenant)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    items = (
        db.query(SmsOutbox)
        .filter(SmsOutbox.tenant_id == tctx.tenant_id)
        .order_by(SmsOutbox.scheduled_at.desc())
        .limit(50)
        .all()
    )
    return _render(
        request,
        "pages/sms_outbox.html",
        {"outbox": items},
        db,
        tenant_slug=tctx.tenant_slug,
    )


@router.post("/sms-outbox/test")
def sms_outbox_test_send(
    request: Request,
    tenant: str = Form("default"),
    to_phone: str = Form(""),
    message: str = Form("Test SMS from Clinic Suite"),
    db: Session = Depends(get_db),
):
    redirect = _require_login_for_tenant(request, tenant)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant)

    to_phone = (to_phone or "").strip()
    message = (message or "").strip() or "Test SMS from Clinic Suite"

    rp = _rp(request)
    if not to_phone:
        return RedirectResponse(url=f"{rp}/sms-outbox?tenant={tctx.tenant_slug}", status_code=303)

    row = SmsOutbox(
        id=str(uuid.uuid4()),
        tenant_id=tctx.tenant_id,
        to_phone=to_phone,
        message=message,
        scheduled_at=datetime.utcnow(),
        status="queued",
        attempts=0,
        last_error=None,
        provider_message_id=None,
    )
    db.add(row)
    db.commit()
    return RedirectResponse(url=f"{rp}/sms-outbox?tenant={tctx.tenant_slug}", status_code=303)


@router.get("/children", response_class=HTMLResponse)
def children_list(request: Request, tenant: str = "default", q: str = "", db: Session = Depends(get_db)):
    redirect = _require_login_for_tenant(request, tenant)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    query = db.query(Child).filter(Child.tenant_id == tctx.tenant_id)
    if q:
        query = query.filter(Child.full_name.ilike(f"%{q}%"))
    children = query.order_by(Child.full_name.asc()).all()
    return _render(request, "pages/children_list.html", {"children": children, "q": q}, db, tenant_slug=tctx.tenant_slug)


@router.post("/children/create")
def children_create(
    request: Request,
    tenant: str = Form("default"),
    full_name: str = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
):
    redirect = _require_login_for_tenant(request, tenant)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    c = Child(tenant_id=tctx.tenant_id, full_name=full_name.strip(), notes=(notes or "").strip() or None)
    db.add(c)
    db.commit()
    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/children?tenant={tctx.tenant_slug}", status_code=303)


@router.get("/therapists", response_class=HTMLResponse)
def therapists_list(request: Request, tenant: str = "default", db: Session = Depends(get_db)):
    redirect = _require_login_for_tenant(request, tenant)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    therapists = db.query(Therapist).filter(Therapist.tenant_id == tctx.tenant_id).order_by(Therapist.name.asc()).all()
    return _render(request, "pages/therapists.html", {"therapists": therapists}, db, tenant_slug=tctx.tenant_slug)


@router.post("/therapists/create")
def therapists_create(
    request: Request,
    tenant: str = Form("default"),
    name: str = Form(...),
    phone: str = Form(""),
    email: str = Form(""),
    role: str = Form(""),
    db: Session = Depends(get_db),
):
    redirect = _require_login_for_tenant(request, tenant)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    t = Therapist(
        tenant_id=tctx.tenant_id,
        name=name.strip(),
        phone=(phone or "").strip() or None,
        email=(email or "").strip() or None,
        role=(role or "").strip() or None,
    )
    db.add(t)
    db.commit()
    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/therapists?tenant={tctx.tenant_slug}", status_code=303)


@router.get("/settings", response_class=HTMLResponse)
def settings_view(request: Request, tenant: str = "default", db: Session = Depends(get_db)):
    redirect = _require_login_for_tenant(request, tenant)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    cs = _get_settings(db, tctx.tenant_id)
    lic = _get_license(db)

    link = (getattr(cs, "google_maps_link", "") or "").strip()
    if not link:
        addr = (cs.address or "").strip()
        if addr:
            link = f"https://www.google.com/maps/search/?api=1&query={quote_plus(addr)}"

    return _render(
        request,
        "pages/settings.html",
        {"clinic": cs, "license": lic, "google_maps_link": link},
        db,
        tenant_slug=tctx.tenant_slug,
    )


@router.post("/settings/clinic")
def settings_update_clinic(
    request: Request,
    tenant: str = Form("default"),
    clinic_name: str = Form(""),
    address: str = Form(""),
    google_maps_link: str = Form(""),
    db: Session = Depends(get_db),
):
    redirect = _require_login_for_tenant(request, tenant)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    cs = _get_settings(db, tctx.tenant_id)
    cs.clinic_name = (clinic_name or "").strip()
    cs.address = (address or "").strip()
    cs.google_maps_link = (google_maps_link or "").strip()
    cs.updated_at = datetime.utcnow()
    db.add(cs)
    db.commit()
    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/settings?tenant={tctx.tenant_slug}", status_code=303)


@router.post("/settings/infobip")
def settings_update_infobip(
    request: Request,
    tenant: str = Form("default"),
    infobip_base_url: str = Form(""),
    infobip_api_key: str = Form(""),
    infobip_sender: str = Form(""),
    db: Session = Depends(get_db),
):
    redirect = _require_login_for_tenant(request, tenant)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    cs = _get_settings(db, tctx.tenant_id)
    cs.sms_provider = "infobip"
    cs.infobip_base_url = (infobip_base_url or "").strip() or "https://api.infobip.com"
    cs.infobip_api_key = (infobip_api_key or "").strip()
    cs.infobip_sender = (infobip_sender or "").strip()
    cs.updated_at = datetime.utcnow()
    db.add(cs)
    db.commit()
    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/settings?tenant={tctx.tenant_slug}", status_code=303)


# --- Internal endpoints for SMS service (tenant-aware via ?tenant=slug) ---
@router.get("/api/internal/clinic_settings")
def api_internal_clinic_settings(request: Request, tenant: str = "default", db: Session = Depends(get_db)):
    _require_internal(request)
    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    cs = _get_settings(db, tctx.tenant_id)
    lic = _get_license(db)
    return {
        "tenant_slug": tctx.tenant_slug,
        "clinic_name": cs.clinic_name,
        "clinic_address": cs.address,
        "google_maps_link": cs.google_maps_link,
        "sms_provider": cs.sms_provider,
        "infobip_sender": cs.infobip_sender,
        "license": {
            "product_mode": lic.product_mode,
            "trial_end": lic.trial_end.isoformat() if lic.trial_end else None,
        },
    }


@router.get("/api/internal/infobip")
def api_internal_infobip(request: Request, tenant: str = "default", db: Session = Depends(get_db)):
    _require_internal(request)
    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    cs = _get_settings(db, tctx.tenant_id)
    return {
        "tenant_slug": tctx.tenant_slug,
        "sms_provider": cs.sms_provider,
        "infobip_base_url": cs.infobip_base_url,
        "infobip_api_key": cs.infobip_api_key,
        "infobip_sender": cs.infobip_sender,
    }

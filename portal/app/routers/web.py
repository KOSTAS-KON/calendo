"""
Portal web routes (HTML pages + internal JSON endpoints).

Notes:
- Keep these routes tolerant to missing query params and older link formats.
- Prefer redirecting to canonical tenant-scoped URLs under /t/{tenant_slug}/...
- Internal endpoints are protected by INTERNAL_API_KEY / PORTAL_INTERNAL_KEY (when set).
"""

from __future__ import annotations

<<<<<<< Updated upstream
=======
from datetime import datetime, date, timedelta, timezone
import hashlib
>>>>>>> Stashed changes
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

<<<<<<< Updated upstream

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
=======
    google_maps_link = cs.map_url if cs else None

    # Handy .env template for the SMS service (and local dev).
    # NOTE: Values are intentionally minimal; copy-paste and adjust.
    portal_base = (str(request.base_url).rstrip("/") + _rp(request)).rstrip("/")
    env_preview = "\n".join(
        [
            f"DATABASE_URL={settings.DATABASE_URL or ''}",
            f"PORTAL_BASE_URL={portal_base}",
            f"PORTAL_APP_URL={portal_base}",
            f"INTERNAL_TOKEN={settings.INTERNAL_API_KEY or ''}",
            f"INTERNAL_API_KEY={settings.INTERNAL_API_KEY or ''}",
            f"SSO_SHARED_SECRET={settings.SSO_SHARED_SECRET or ''}",
            "",
            "# Optional / provider settings",
            f"SMS_PROVIDER={(cs.sms_provider if cs else '')}",
            f"INFOBIP_BASE_URL={(cs.infobip_base_url if cs else '')}",
            f"INFOBIP_FROM={(cs.infobip_sender if cs else '')}",
            f"INFOBIP_API_KEY={(cs.infobip_api_key if cs else '')}",
        ]
    )

    return _render(
        request,
        "pages/settings.html",
        {
            "need": need,
            "subscription_active": active,
            "subscription_until": until,
            "subscription_plan": plan,
            "clinic": cs,
            "license": lic,
            "google_maps_link": google_maps_link,
            "env_preview": env_preview,
        },
        db,
        tenant_slug=tctx.tenant_slug,
    )


@router.post("/settings/clinic")
def settings_save_clinic(
    request: Request,
    clinic_name: str = Form(""),
    address: str = Form(""),
    timezone: str = Form(""),  # currently informational; kept for forward compatibility
    lat: str = Form(""),
    lng: str = Form(""),
    db: Session = Depends(get_db),
):
    """Save clinic identity + map info.

    The Settings UI does not include the tenant as a hidden input, so we derive it from the
    authenticated session.
    """

    tenant_slug = _session_tenant_slug(request)
    redirect = _require_login_for_tenant(request, tenant_slug)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant_slug)
    cs = _get_settings(db, tctx.tenant_id)

    cs.clinic_name = (clinic_name or "").strip()
    cs.address = (address or "").strip()

    def _to_float(v: str) -> Optional[float]:
        v = (v or "").strip()
        if not v:
            return None
        try:
            return float(v)
        except Exception:
            return None

    cs.lat = _to_float(lat)
    cs.lng = _to_float(lng)

    # Store a convenient google maps link.
    if cs.lat is not None and cs.lng is not None:
        cs.google_maps_link = f"https://www.google.com/maps?q={cs.lat},{cs.lng}"
    elif cs.address:
        cs.google_maps_link = f"https://www.google.com/maps/search/?api=1&query={quote_plus(cs.address)}"
    else:
        cs.google_maps_link = None

    cs.updated_at = datetime.utcnow()
    db.add(cs)
    db.commit()

    _toast_set(request, "success", "Clinic settings saved")
    return RedirectResponse(url=f"{_rp(request)}/settings?tenant={tctx.tenant_slug}", status_code=303)


@router.post("/settings/infobip")
def settings_save_infobip(
    request: Request,
    sms_provider: str = Form(""),
    infobip_base_url: str = Form(""),
    infobip_sender: str = Form(""),
    infobip_api_key: str = Form(""),
    db: Session = Depends(get_db),
):
    tenant_slug = _session_tenant_slug(request)
    redirect = _require_login_for_tenant(request, tenant_slug)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant_slug)
    cs = _get_settings(db, tctx.tenant_id)

    cs.sms_provider = (sms_provider or "").strip() or cs.sms_provider or "infobip"
    cs.infobip_base_url = (infobip_base_url or "").strip()
    cs.infobip_sender = (infobip_sender or "").strip()
    cs.infobip_api_key = (infobip_api_key or "").strip()
    cs.updated_at = datetime.utcnow()

    db.add(cs)
    db.commit()

    _toast_set(request, "success", "SMS provider settings saved")
    return RedirectResponse(url=f"{_rp(request)}/settings?tenant={tctx.tenant_slug}", status_code=303)


@router.get("/settings/env")
def settings_env_download(request: Request, tenant: str = "", db: Session = Depends(get_db)):
    """Download a minimal env file for the SMS service."""

    tenant_slug = (tenant or "").strip().lower() or _session_tenant_slug(request)
    redirect = _require_login_for_tenant(request, tenant_slug)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant_slug)
    cs = _get_settings(db, tctx.tenant_id)

    portal_base = (str(request.base_url).rstrip("/") + _rp(request)).rstrip("/")
    env_text = "\n".join(
        [
            f"PORTAL_BASE_URL={portal_base}",
            f"PORTAL_APP_URL={portal_base}",
            f"INTERNAL_API_KEY={settings.INTERNAL_API_KEY or ''}",
            f"INTERNAL_TOKEN={settings.INTERNAL_API_KEY or ''}",
            f"SSO_SHARED_SECRET={settings.SSO_SHARED_SECRET or ''}",
            f"SMS_PROVIDER={(cs.sms_provider if cs else '')}",
            f"INFOBIP_BASE_URL={(cs.infobip_base_url if cs else '')}",
            f"INFOBIP_FROM={(cs.infobip_sender if cs else '')}",
            f"INFOBIP_API_KEY={(cs.infobip_api_key if cs else '')}",
            "",
        ]
    )

    filename = f"calendo_{tctx.tenant_slug}.env"
    return Response(
        env_text,
        media_type="text/plain",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@router.post("/settings/activate")
def settings_activate_code(
    request: Request,
    activation_code: str = Form(""),
    db: Session = Depends(get_db),
):
    """Redeem an activation code and extend/create the tenant subscription."""

    tenant_slug = _session_tenant_slug(request)
    redirect = _require_login_for_tenant(request, tenant_slug)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant_slug)
    code = (activation_code or "").strip()
    if not code:
        _toast_set(request, "error", "Please enter an activation code")
        return RedirectResponse(url=f"{_rp(request)}/settings?tenant={tctx.tenant_slug}", status_code=303)

    code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    ac = (
        db.query(ActivationCode)
        .filter(
            ActivationCode.tenant_id == tctx.tenant_id,
            ActivationCode.code_hash == code_hash,
            ActivationCode.revoked_at.is_(None),
        )
        .first()
    )
    if not ac:
        _toast_set(request, "error", "Invalid or revoked activation code")
        return RedirectResponse(url=f"{_rp(request)}/settings?tenant={tctx.tenant_slug}", status_code=303)

    if ac.max_redemptions is not None and ac.redeemed_count >= ac.max_redemptions:
        _toast_set(request, "error", "This activation code has no remaining redemptions")
        return RedirectResponse(url=f"{_rp(request)}/settings?tenant={tctx.tenant_slug}", status_code=303)

    plan = db.get(Plan, ac.plan_id)
    if not plan:
        _toast_set(request, "error", "Activation code plan missing")
        return RedirectResponse(url=f"{_rp(request)}/settings?tenant={tctx.tenant_slug}", status_code=303)

    now = datetime.now(timezone.utc)
    duration = int(plan.duration_days or 30)

    sub = (
        db.query(Subscription)
        .filter(Subscription.tenant_id == tctx.tenant_id, Subscription.status == "active")
        .order_by(Subscription.ends_at.desc().nullslast())
        .first()
    )

    if sub and sub.ends_at and sub.ends_at > now:
        sub.ends_at = sub.ends_at + timedelta(days=duration)
        sub.plan_id = plan.id
    else:
        sub = Subscription(
            tenant_id=tctx.tenant_id,
            plan_id=plan.id,
            status="active",
            starts_at=now,
            ends_at=now + timedelta(days=duration),
        )
        db.add(sub)

    ac.redeemed_count += 1
    db.add(ac)
    db.commit()

    _toast_set(request, "success", f"Activated: {plan.name} (+{duration} days)")
    return RedirectResponse(url=f"{_rp(request)}/settings?tenant={tctx.tenant_slug}", status_code=303)


@router.post("/settings/license")
def settings_save_license(
    request: Request,
    product_mode: str = Form("BOTH"),
    trial_end: str = Form(""),
    license_end: str = Form(""),
    db: Session = Depends(get_db),
):
    """Allow editing the global license object (product mode + optional dates).

    Production licensing is enforced per-tenant via Subscriptions, but this lets you set a
    global product mode (SMS / Portal / Both) from the Settings UI.
    """

    tenant_slug = _session_tenant_slug(request)
    redirect = _require_login_for_tenant(request, tenant_slug)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant_slug)
    lic = _get_license(db)
    lic.product_mode = (product_mode or "BOTH").strip().upper()

    def _parse_date(d: str) -> Optional[datetime]:
        d = (d or "").strip()
        if not d:
            return None
        try:
            # HTML date input -> YYYY-MM-DD
            return datetime.fromisoformat(d).replace(tzinfo=timezone.utc)
        except Exception:
            return None

    lic.trial_end = _parse_date(trial_end)
    lic.license_end = _parse_date(license_end)
    lic.updated_at = datetime.utcnow()

    db.add(lic)
    db.commit()

    _toast_set(request, "success", "License settings saved")
    return RedirectResponse(url=f"{_rp(request)}/settings?tenant={tctx.tenant_slug}", status_code=303)


# =============================================================================
# Internal calendar sync API (SMS reads/writes Portal appointments)
# =============================================================================
@router.get("/api/internal/children")
def api_internal_children(request: Request, tenant: str = "default", db: Session = Depends(get_db)):
    _require_internal(request)
    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    rows = (
        db.query(Child)
        .filter(Child.tenant_id == tctx.tenant_id)
        .order_by(Child.full_name.asc())
        .all()
    )
    out = []
    for c in rows:
        out.append(
            {
                "id": c.id,
                "full_name": c.full_name,
                "date_of_birth": c.date_of_birth.isoformat() if getattr(c, "date_of_birth", None) else None,
                "parent1_phone": getattr(c, "parent1_phone", None),
                "parent1_email": getattr(c, "parent1_email", None),
                "parent2_phone": getattr(c, "parent2_phone", None),
                "parent2_email": getattr(c, "parent2_email", None),
            }
        )
    return {"tenant": tctx.tenant_slug, "children": out}


@router.post("/api/internal/children/create")
def api_internal_children_create(
    request: Request,
    tenant: str = "default",
    full_name: str = Form(...),
    date_of_birth: str = Form(""),
    notes: str = Form(""),
    parent_phone: str = Form(""),
    parent_email: str = Form(""),
    db: Session = Depends(get_db),
):
    """Create a Child from the SMS service (internal key protected)."""
    _require_internal(request)
    tctx = resolve_tenant(db, request, tenant_slug=tenant)

    name = (full_name or "").strip()
    if not name:
        return {"ok": False, "error": "full_name is required"}

    dob: Optional[date] = None
    dob_s = (date_of_birth or "").strip()
    if dob_s:
        try:
            dob = date.fromisoformat(dob_s)
        except Exception:
            return {"ok": False, "error": "date_of_birth must be YYYY-MM-DD"}

    c = Child(
        tenant_id=tctx.tenant_id,
        full_name=name,
        date_of_birth=dob,
        notes=(notes or "").strip() or None,
        parent1_phone=(parent_phone or "").strip() or None,
        parent1_email=(parent_email or "").strip() or None,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(c)
    db.commit()
    return {"ok": True, "child_id": c.id}


@router.get("/api/internal/clinic_settings")
def api_internal_clinic_settings(request: Request, tenant: str = "default", db: Session = Depends(get_db)):
    """Return clinic/SMS provider settings for a tenant (internal key protected)."""
    _require_internal(request)
    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    cs = _get_settings(db, tctx.tenant_id)
    return {
        "tenant": tctx.tenant_slug,
        "clinic_settings": {
            "clinic_name": cs.clinic_name,
            "address": cs.address,
            "google_maps_link": cs.google_maps_link,
            "lat": cs.lat,
            "lng": cs.lng,
            "sms_provider": cs.sms_provider,
            "infobip_base_url": cs.infobip_base_url,
            "infobip_sender": cs.infobip_sender,
            # The SMS service needs the API key to send; do not expose this endpoint publicly.
            "infobip_api_key": cs.infobip_api_key,
        },
    }


@router.get("/api/internal/appointments")
def api_internal_appointments(request: Request, tenant: str = "default", days: int = 60, db: Session = Depends(get_db)):
    _require_internal(request)
    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    days = max(1, min(365, int(days or 60)))
    now = datetime.utcnow()
    end = now + timedelta(days=days)

    q = (
        db.query(Appointment, Child)
        .join(Child, Child.id == Appointment.child_id)
        .filter(
            Appointment.tenant_id == tctx.tenant_id,
            Appointment.starts_at >= now - timedelta(days=3),
            Appointment.starts_at <= end,
        )
        .order_by(Appointment.starts_at.asc())
    )

    out = []
    for appt, child in q.all():
        att = (appt.attendance_status or "").upper()
        status = "cancelled" if att == "CANCELLED" else "active"
        out.append({
            "id": appt.id,
            "child_id": appt.child_id,
            "child_name": child.full_name,
            "to_phone": getattr(child, "parent1_phone", "") or getattr(child, "parent2_phone", "") or "",
            "starts_at": appt.starts_at.replace(tzinfo=timezone.utc).isoformat(),
            "ends_at": appt.ends_at.replace(tzinfo=timezone.utc).isoformat(),
            "procedure": appt.procedure,
            "therapist_name": appt.therapist_name,
            "status": status,
            "attendance_status": appt.attendance_status,
        })
    return {"tenant": tctx.tenant_slug, "appointments": out}


@router.post("/api/internal/appointments/create")
def api_internal_appointment_create(
>>>>>>> Stashed changes
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

<<<<<<< Updated upstream
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
=======
    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    gate = _require_active_subscription(request, db, tctx.tenant_slug, tctx.tenant_id)
    if gate:
        return gate

    query = db.query(Child).filter(Child.tenant_id == tctx.tenant_id)
    if q:
        query = query.filter(Child.full_name.ilike(f"%{q}%"))

    children = query.order_by(Child.full_name.asc()).all()
    meta = {
        "total": len(children),
    }

    return _render(
        request,
        "pages/children_list.html",
        {"children": children, "q": q, "meta": meta},
        db,
        tenant_slug=tctx.tenant_slug,
    )
>>>>>>> Stashed changes


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

<<<<<<< Updated upstream
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
=======

@router.get("/children/{child_id}", response_class=HTMLResponse)
def child_detail(request: Request, child_id: int, tenant: str = "default", tab: str = "overview", db: Session = Depends(get_db)):
    redirect = _require_login_for_tenant(request, tenant)
    if redirect:
        return redirect

>>>>>>> Stashed changes
    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    cs = _get_or_create_clinic_settings(db, tctx.tenant_id)

<<<<<<< Updated upstream
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
=======
    child = (
        db.query(Child)
        .filter(Child.tenant_id == tctx.tenant_id, Child.id == child_id)
        .first()
    )
    if not child:
        raise HTTPException(status_code=404, detail="Not Found")

    appointments = (
        db.query(Appointment)
        .filter(Appointment.tenant_id == tctx.tenant_id, Appointment.child_id == child.id)
        .order_by(Appointment.starts_at.desc())
        .all()
    )
    attachments = (
        db.query(Attachment)
        .filter(Attachment.tenant_id == tctx.tenant_id, Attachment.child_id == child.id)
        .order_by(Attachment.created_at.desc())
        .all()
    )
    timeline = (
        db.query(TimelineEvent)
        .filter(TimelineEvent.tenant_id == tctx.tenant_id, TimelineEvent.child_id == child.id)
        .order_by(TimelineEvent.occurred_at.desc())
        .all()
    )

    return _render(
        request,
        "pages/child_detail.html",
        {
            "child": child,
            "appointments": appointments,
            "attachments": attachments,
            "timeline": timeline,
            "tab": (tab or "overview").lower(),
        },
        db,
        tenant_slug=tctx.tenant_slug,
    )


# ----------------------------
# SMS Outbox (portal view)
# ----------------------------
@router.get("/sms-outbox", response_class=HTMLResponse)
def sms_outbox_page(request: Request, tenant: str = "default", db: Session = Depends(get_db)):
    redirect = _require_login_for_tenant(request, tenant)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    gate = _require_active_subscription(request, db, tctx.tenant_slug, tctx.tenant_id)
    if gate:
        return gate

    rows = (
        db.query(SmsOutbox)
        .filter(SmsOutbox.tenant_id == tctx.tenant_id)
        .order_by(SmsOutbox.created_at.desc())
        .limit(250)
        .all()
    )

    # Map DB rows to the field names expected by the template.
    outbox = [
        {
            "scheduled_at": (r.next_attempt_at or r.created_at),
            "to_phone": r.to_number,
            "message": r.body,
            "status": r.status,
            "attempts": r.attempts,
            "provider_message_id": r.provider_message_id,
            "last_error": r.error,
        }
        for r in rows
    ]

    return _render(
        request,
        "pages/sms_outbox.html",
        {"outbox": outbox},
        db,
        tenant_slug=tctx.tenant_slug,
    )


@router.post("/sms-outbox/test")
def sms_outbox_test(
    request: Request,
    tenant: str = Form("default"),
    to_phone: str = Form(""),
    message: str = Form(""),
    db: Session = Depends(get_db),
):
    redirect = _require_login_for_tenant(request, tenant)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    gate = _require_active_subscription(request, db, tctx.tenant_slug, tctx.tenant_id)
    if gate:
        return gate

    to_phone = (to_phone or "").strip()
    message = (message or "").strip()
    if not to_phone or not message:
        _toast_set(request, "error", "Please provide a phone number and a message")
        return RedirectResponse(url=f"{_rp(request)}/sms-outbox?tenant={tctx.tenant_slug}", status_code=303)

    row = SmsOutbox(
        tenant_id=tctx.tenant_id,
        to_number=to_phone,
        body=message,
        status="queued",
        provider="",
        next_attempt_at=datetime.utcnow(),
    )
    db.add(row)
    db.commit()
    _toast_set(request, "success", "Message queued")
    return RedirectResponse(url=f"{_rp(request)}/sms-outbox?tenant={tctx.tenant_slug}", status_code=303)


# ----------------------------
# Legacy compatibility routes (avoid 404s from old buttons)
# ----------------------------
>>>>>>> Stashed changes


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
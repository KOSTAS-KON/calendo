from __future__ import annotations

from datetime import datetime, date, timedelta, timezone
import os
import uuid
import sqlalchemy as sa
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Request, Form, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy.orm import Session

from app.db import get_db
from app.config import settings
from app.tenancy import resolve_tenant

from app.models.child import Child
from app.models.therapist import Therapist
from app.models.appointment import Appointment
from app.models.billing import BillingItem
from app.models.session_note import SessionNote
from app.models.clinic_settings import ClinicSettings, AppLicense
from app.models.sms_outbox import SmsOutbox

try:
    from app.models.attachment import Attachment  # type: ignore
except Exception:
    Attachment = None  # type: ignore

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# ----------------------------
# Helpers
# ----------------------------
def _rp(request: Request) -> str:
    return request.scope.get("root_path", "") or ""


def _session(request: Request) -> dict:
    s = request.scope.get("session")
    return s if isinstance(s, dict) else {}


def _toast_set(request: Request, kind: str, text: str) -> None:
    s = request.scope.get("session")
    if isinstance(s, dict):
        s["toast"] = {"kind": kind, "text": text, "at": datetime.utcnow().isoformat()}


def _toast_pop(request: Request):
    s = request.scope.get("session")
    if isinstance(s, dict):
        return s.pop("toast", None)
    return None

def _parse_date(val: str | None) -> date | None:
    if not val:
        return None
    v = val.strip()
    if not v:
        return None
    try:
        return date.fromisoformat(v)
    except Exception:
        return None



def _session_tenant_slug(request: Request) -> str:
    s = _session(request)
    return (s.get("tenant_slug") or "default").strip().lower()


def _sso_serializer() -> URLSafeTimedSerializer:
    secret = (settings.SSO_SHARED_SECRET or "").strip()
    if not secret:
        raise RuntimeError("Security is not configured: missing SSO_SHARED_SECRET. Please set it in the environment.")
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


def _sms_link_for(request: Request, tenant_slug: str) -> str:
    sms_url = (settings.SMS_APP_URL or "").strip() or "/sms"
    if sms_url.endswith("/"):
        sms_url = sms_url[:-1]
    sso = _make_sms_sso_token(request, tenant_slug)
    if "onrender.com" in sms_url:
        return f"{sms_url}/sms?tenant={tenant_slug}&sso={sso}"
    return f"{sms_url}?tenant={tenant_slug}&sso={sso}"


def _require_login_for_tenant(request: Request, tenant_slug: str) -> RedirectResponse | None:
    s = _session(request)
    user_id = s.get("user_id")
    sess_tenant = (s.get("tenant_slug") or "default").strip().lower()
    tenant_slug = (tenant_slug or "default").strip().lower()

    if not user_id:
        rp = _rp(request)
        return RedirectResponse(url=f"{rp}/login?next=/t/{tenant_slug}/suite", status_code=303)

    if sess_tenant != tenant_slug:
        rp = _rp(request)
        return RedirectResponse(url=f"{rp}/login?next=/t/{tenant_slug}/suite", status_code=303)

    return None


def _subscription_status(db: Session, tenant_id: str) -> tuple[bool, datetime | None, str]:
    try:
        from app.models.licensing import Subscription, Plan
        sub = (
            db.query(Subscription)
            .filter(Subscription.tenant_id == tenant_id)
            .order_by(Subscription.ends_at.desc())
            .first()
        )
        if not sub or not getattr(sub, "ends_at", None):
            return False, None, ""
        until = sub.ends_at
        active = bool(until > datetime.utcnow() and str(getattr(sub, "status", "active")).lower() == "active")
        plan_code = ""
        try:
            p = db.query(Plan).filter(Plan.id == sub.plan_id).first()
            if p:
                plan_code = getattr(p, "code", "") or ""
        except Exception:
            pass
        return active, until, plan_code
    except Exception:
        return False, None, ""


def _require_active_subscription(request: Request, db: Session, tenant_slug: str, tenant_id: str) -> RedirectResponse | None:
    active, _until, _plan = _subscription_status(db, tenant_id)
    if active:
        return None
    rp = _rp(request)
    # ✅ important: /settings exists now
    return RedirectResponse(url=f"{rp}/settings?tenant={tenant_slug}&need=license", status_code=303)


def _require_internal(request: Request) -> None:
    hdr = (request.headers.get("x-internal-key") or request.headers.get("X-Internal-Key") or "").strip()
    expected = (settings.INTERNAL_API_KEY or "").strip()
    if not expected or hdr != expected:
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
    active, until, plan = _subscription_status(db, tctx.tenant_id)

    base = {
        "request": request,
        "tenant_slug": tctx.tenant_slug,
        "tenant_name": tctx.tenant_name,
        "clinic": cs,
        "license": lic,
        "subscription_active": active,
        "subscription_until": until,
        "subscription_plan": plan,
        "sms_app_url": _sms_link_for(request, tctx.tenant_slug),
        "toast": _toast_pop(request),
        "now": datetime.utcnow(),
        "rp": _rp(request),
    }
    base.update(ctx or {})
    return templates.TemplateResponse(template_name, base)


def _child_or_404(db: Session, tenant_id: str, child_id: int) -> Child:
    child = db.query(Child).filter(Child.tenant_id == tenant_id, Child.id == child_id).first()
    if not child:
        raise HTTPException(status_code=404, detail="Not Found")
    return child


# =============================================================================
# Settings page (FIXES /settings 404)
# =============================================================================
@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, tenant: str = "default", need: str = "", db: Session = Depends(get_db)):
    # Login check
    redirect = _require_login_for_tenant(request, tenant)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    cs = _get_settings(db, tctx.tenant_id)
    lic = _get_license(db)
    active, until, plan = _subscription_status(db, tctx.tenant_id)

    return _render(
        request,
        "pages/setup.html" if (Path("app/templates/pages/setup.html")).exists() else "pages/suite.html",
        {"need": need, "subscription_active": active, "subscription_until": until, "subscription_plan": plan, "clinic": cs, "license": lic},
        db,
        tenant_slug=tctx.tenant_slug,
    )


@router.get("/setup", response_class=HTMLResponse)
def setup_alias(request: Request, tenant: str = "default", need: str = "", db: Session = Depends(get_db)):
    # Alias so /setup works too
    return settings_page(request=request, tenant=tenant, need=need, db=db)


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
        out.append({
            "id": c.id,
            "full_name": c.full_name,
            "parent1_phone": getattr(c, "parent1_phone", None),
            "parent2_phone": getattr(c, "parent2_phone", None),
        })
    return {"tenant": tctx.tenant_slug, "children": out}


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
    request: Request,
    tenant: str = Form("default"),
    child_id: int = Form(...),
    starts_at_iso: str = Form(...),
    ends_at_iso: str = Form(...),
    therapist_name: str = Form(""),
    procedure: str = Form("Session"),
    db: Session = Depends(get_db),
):
    _require_internal(request)
    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    _child_or_404(db, tctx.tenant_id, child_id)

    sdt = datetime.fromisoformat(starts_at_iso.replace("Z", "+00:00"))
    edt = datetime.fromisoformat(ends_at_iso.replace("Z", "+00:00"))
    if edt <= sdt:
        raise HTTPException(status_code=400, detail="Invalid times")

    appt = Appointment(
        tenant_id=tctx.tenant_id,
        child_id=child_id,
        starts_at=sdt.replace(tzinfo=None),
        ends_at=edt.replace(tzinfo=None),
        therapist_name=(therapist_name or "").strip(),
        procedure=(procedure or "Session").strip() or "Session",
        attendance_status="UNCONFIRMED",
    )
    db.add(appt)
    db.commit()
    db.refresh(appt)
    return {"ok": True, "appointment_id": appt.id}


@router.post("/api/internal/appointments/{appointment_id}/move")
def api_internal_appointment_move(
    request: Request,
    appointment_id: int,
    tenant: str = Form("default"),
    starts_at_iso: str = Form(...),
    ends_at_iso: str = Form(...),
    db: Session = Depends(get_db),
):
    _require_internal(request)
    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    appt = db.query(Appointment).filter(Appointment.tenant_id == tctx.tenant_id, Appointment.id == appointment_id).first()
    if not appt:
        raise HTTPException(status_code=404, detail="Not Found")

    sdt = datetime.fromisoformat(starts_at_iso.replace("Z", "+00:00"))
    edt = datetime.fromisoformat(ends_at_iso.replace("Z", "+00:00"))
    if edt <= sdt:
        raise HTTPException(status_code=400, detail="Invalid times")

    appt.starts_at = sdt.replace(tzinfo=None)
    appt.ends_at = edt.replace(tzinfo=None)
    db.add(appt)
    db.commit()
    return {"ok": True}


@router.post("/api/internal/appointments/{appointment_id}/cancel")
def api_internal_appointment_cancel(
    request: Request,
    appointment_id: int,
    tenant: str = Form("default"),
    db: Session = Depends(get_db),
):
    _require_internal(request)
    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    appt = db.query(Appointment).filter(Appointment.tenant_id == tctx.tenant_id, Appointment.id == appointment_id).first()
    if not appt:
        raise HTTPException(status_code=404, detail="Not Found")

    appt.attendance_status = "CANCELLED"
    db.add(appt)
    db.commit()
    return {"ok": True}


# =============================================================================
# Suite + key pages (license gated)
# =============================================================================
@router.get("/t/{tenant_slug}/suite", response_class=HTMLResponse)
def suite_tenant(request: Request, tenant_slug: str, db: Session = Depends(get_db)):
    redirect = _require_login_for_tenant(request, tenant_slug)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant_slug)
    gate = _require_active_subscription(request, db, tctx.tenant_slug, tctx.tenant_id)
    if gate:
        return gate

    return _render(request, "pages/suite.html", {}, db, tenant_slug=tctx.tenant_slug)


@router.get("/children", response_class=HTMLResponse)
def children_list(request: Request, tenant: str = "default", q: str = "", db: Session = Depends(get_db)):
    redirect = _require_login_for_tenant(request, tenant)
    if redirect:
        return redirect


@router.post("/children/create")
def children_create(
    request: Request,
    tenant: str = Form("default"),
    full_name: str = Form(...),
    date_of_birth: str = Form(""),
    notes: str = Form(""),
    parent1_name: str = Form(""),
    parent1_phone: str = Form(""),
    parent2_name: str = Form(""),
    parent2_phone: str = Form(""),
    db: Session = Depends(get_db),
):
    # Create a new child record (tenant-safe)
    redirect = _require_login_for_tenant(request, tenant)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    gate = _require_active_subscription(request, db, tctx.tenant_slug, tctx.tenant_id)
    if gate:
        return gate

    c = Child(
        tenant_id=tctx.tenant_id,
        full_name=(full_name or "").strip(),
        date_of_birth=_parse_date(date_of_birth),
        notes=(notes or "").strip(),
        parent1_name=(parent1_name or "").strip(),
        parent1_phone=(parent1_phone or "").strip(),
        parent2_name=(parent2_name or "").strip(),
        parent2_phone=(parent2_phone or "").strip(),
    )
    db.add(c)
    db.commit()
    db.refresh(c)
    _toast_set(request, "success", "Child created")
    return RedirectResponse(url=f"{_rp(request)}/children/{c.id}?tab=overview", status_code=303)

    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    gate = _require_active_subscription(request, db, tctx.tenant_slug, tctx.tenant_id)
    if gate:
        return gate

    query = db.query(Child).filter(Child.tenant_id == tctx.tenant_id)
    if q:
        query = query.filter(Child.full_name.ilike(f"%{q}%"))
    children = query.order_by(Child.full_name.asc()).all()

    return _render(request, "pages/children_list.html", {"children": children, "q": q}, db, tenant_slug=tctx.tenant_slug)


# ----------------------------
# Legacy compatibility routes (avoid 404s from old buttons)
# ----------------------------
@router.get("/sms-outbox")
def legacy_sms_outbox(request: Request, tenant: str = "default"):
    rp = _rp(request)
    t = (tenant or "default").strip().lower()
    return RedirectResponse(url=f"{rp}/t/{t}/suite#sms-outbox", status_code=303)


@router.get("/billing")
def legacy_billing(request: Request):
    rp = _rp(request)
    t = _session_tenant_slug(request)
    return RedirectResponse(url=f"{rp}/t/{t}/suite#billing", status_code=303)


@router.get("/appointments")
def legacy_appointments(request: Request):
    rp = _rp(request)
    t = _session_tenant_slug(request)
    return RedirectResponse(url=f"{rp}/t/{t}/suite#appointments", status_code=303)


@router.get("/timeline")
def legacy_timeline(request: Request):
    rp = _rp(request)
    t = _session_tenant_slug(request)
    return RedirectResponse(url=f"{rp}/children?tenant={t}", status_code=303)

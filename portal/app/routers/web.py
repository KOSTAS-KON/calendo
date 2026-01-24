from __future__ import annotations

from datetime import datetime, timedelta
import json
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.config import settings
from app.tenancy import resolve_tenant

from app.models.child import Child
from app.models.appointment import Appointment
from app.models.therapist import Therapist
from app.models.attachment import Attachment
from app.models.session_note import SessionNote
from app.models.timeline import TimelineEvent
from app.models.billing import BillingItem
from app.models.billing_plan import BillingPlan
from app.models.clinic_settings import ClinicSettings, AppLicense

from app.services.storage import save_upload, delete_file


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# -------------------------
# Helpers
# -------------------------

def _rp(request: Request) -> str:
    """
    root_path prefix when served behind reverse proxy (e.g. nginx /therapy).
    """
    return request.scope.get("root_path", "") or ""


def _prefix_with_rp(url: str, rp: str) -> str:
    if not url:
        return url
    if not rp:
        return url
    if not url.startswith("/"):
        return url
    if url.startswith(rp + "/") or url == rp:
        return url
    return rp + url


def _get_singletons(db: Session, tenant_id: str) -> tuple[ClinicSettings, AppLicense]:
    """
    Per-tenant clinic settings + legacy license row (AppLicense id=1).
    """
    clinic = db.query(ClinicSettings).filter(ClinicSettings.tenant_id == tenant_id).first()
    if not clinic:
        clinic = ClinicSettings(tenant_id=tenant_id)
        db.add(clinic)
        db.commit()
        db.refresh(clinic)

    lic = db.get(AppLicense, 1)
    if not lic:
        lic = AppLicense(id=1, product_mode="BOTH")
        db.add(lic)
        db.commit()
        db.refresh(lic)

    return clinic, lic


def _tenant_ctx(db: Session, request: Request, tenant_slug: str | None) -> object:
    # If no tenant slug in path, default to "default"
    return resolve_tenant(db, request, tenant_slug=tenant_slug or "default")


def _render(request: Request, template_name: str, ctx: dict, db: Session, tenant_slug: str | None):
    tctx = _tenant_ctx(db, request, tenant_slug)
    clinic, lic = _get_singletons(db, tctx.tenant_id)

    base_ctx = {
        "request": request,
        "clinic": clinic,
        "license": lic,
        "tenant_slug": tctx.tenant_slug,
        "tenant_name": tctx.tenant_name,
        # SMS app served on separate origin; pass tenant via querystring
        "sms_app_url": (settings.SMS_APP_URL or "/sms").rstrip("/") + f"/sms?tenant={tctx.tenant_slug}"
        if (settings.SMS_APP_URL or "").strip().endswith(".onrender.com")
        else (settings.SMS_APP_URL or "/sms").rstrip("/") + f"?tenant={tctx.tenant_slug}",
    }
    base_ctx.update(ctx or {})
    return templates.TemplateResponse(template_name, base_ctx)


def _require_internal(request: Request) -> None:
    """
    Internal API auth:
      - X-Internal-Key must match INTERNAL_API_KEY OR
      - x-internal-token must match SECRET_KEY (legacy)
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


def status_badge(status: str) -> str:
    s = (status or "").upper()
    m = {
        "CONFIRMED": ("C", "b-confirmed"),
        "UNCONFIRMED": ("U", "b-unconfirmed"),
        "CANCELLED_PROVIDER": ("D", "b-cancel-provider"),
        "CANCELLED_ME": ("P", "b-cancel-me"),
        "MISSED": ("M", "b-missed"),
        "ATTENDED": ("A", "b-attended"),
    }
    letter, cls = m.get(s, ("?", "b-unconfirmed"))
    return f'<span class="badge {cls}" title="{s}">{letter}</span>'


def status_chip(status: str) -> str:
    s = (status or "").upper()
    return f'<span class="chip">{status_badge(s)}<span>{s}</span></span>'


templates.env.globals["status_badge"] = status_badge
templates.env.globals["status_chip"] = status_chip


# -------------------------
# Routes (Tenant-aware)
# -------------------------

@router.get("/", response_class=HTMLResponse)
def root(request: Request, db: Session = Depends(get_db)):
    # Default tenant landing
    return RedirectResponse(url="/suite", status_code=307)


@router.get("/suite", response_class=HTMLResponse)
def suite_default(request: Request, db: Session = Depends(get_db)):
    return _render(request, "pages/suite.html", {}, db, tenant_slug="default")


@router.get("/t/{tenant_slug}/suite", response_class=HTMLResponse)
def suite_tenant(request: Request, tenant_slug: str, db: Session = Depends(get_db)):
    return _render(request, "pages/suite.html", {}, db, tenant_slug=tenant_slug)


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), tenant: str | None = None):
    tctx = _tenant_ctx(db, request, tenant)
    past = (
        db.query(Appointment)
        .filter(Appointment.tenant_id == tctx.tenant_id)
        .order_by(Appointment.starts_at.desc())
        .limit(25)
        .all()
    )
    return _render(
        request,
        "pages/dashboard.html",
        {
            "past": past,
            "children_count": db.query(Child).filter(Child.tenant_id == tctx.tenant_id).count(),
            "appt_count": db.query(Appointment).filter(Appointment.tenant_id == tctx.tenant_id).count(),
        },
        db,
        tenant_slug=tctx.tenant_slug,
    )


@router.get("/calendar", response_class=HTMLResponse)
def calendar_view(request: Request, child_id: int | None = None, tenant: str | None = None, db: Session = Depends(get_db)):
    tctx = _tenant_ctx(db, request, tenant)
    children = db.query(Child).filter(Child.tenant_id == tctx.tenant_id).order_by(Child.full_name.asc()).all()
    therapists = db.query(Therapist).filter(Therapist.tenant_id == tctx.tenant_id).order_by(Therapist.name.asc()).all()
    return _render(
        request,
        "pages/calendar.html",
        {"children": children, "selected_child_id": child_id, "therapists": therapists},
        db,
        tenant_slug=tctx.tenant_slug,
    )


@router.get("/api/calendar_events")
def calendar_events(
    request: Request,
    start: str | None = None,
    end: str | None = None,
    child_id: int | None = None,
    tenant: str | None = None,
    db: Session = Depends(get_db),
):
    tctx = _tenant_ctx(db, request, tenant)
    start_dt = datetime.fromisoformat(start) if start else datetime.now() - timedelta(days=30)
    end_dt = datetime.fromisoformat(end) if end else datetime.now() + timedelta(days=90)

    rp = _rp(request)
    events: list[dict] = []

    aq = (
        db.query(Appointment)
        .filter(Appointment.tenant_id == tctx.tenant_id)
        .filter(Appointment.starts_at >= start_dt, Appointment.starts_at < end_dt)
    )
    if child_id is not None:
        aq = aq.filter(Appointment.child_id == child_id)

    for a in aq.all():
        status = (a.attendance_status or "UNCONFIRMED").upper()
        color = {
            "ATTENDED": "#16a34a",
            "MISSED": "#dc2626",
            "CONFIRMED": "#2563eb",
            "UNCONFIRMED": "#6b7280",
            "CANCELLED_PROVIDER": "#f59e0b",
            "CANCELLED_ME": "#f97316",
        }.get(status, "#6b7280")
        title_child = getattr(a.child, "full_name", "Child")
        events.append(
            {
                "id": f"appt_{a.id}",
                "title": f"Appt: {title_child} · {a.procedure}",
                "start": a.starts_at.isoformat(),
                "url": _prefix_with_rp(f"/appointments/{a.id}", rp),
                "backgroundColor": color,
                "borderColor": color,
            }
        )

    return JSONResponse(events)


# -------------------------
# Children (tenant-scoped)
# -------------------------

@router.get("/children", response_class=HTMLResponse)
def children_list(request: Request, q: str | None = None, tenant: str | None = None, db: Session = Depends(get_db)):
    tctx = _tenant_ctx(db, request, tenant)

    query = db.query(Child).filter(Child.tenant_id == tctx.tenant_id)
    if q:
        query = query.filter(Child.full_name.ilike(f"%{q}%"))
    children = query.order_by(Child.full_name.asc()).limit(300).all()

    return _render(request, "pages/children_list.html", {"children": children, "q": q or ""}, db, tenant_slug=tctx.tenant_slug)


@router.post("/children/create")
def children_create(
    request: Request,
    full_name: str = Form(...),
    date_of_birth: str = Form(""),
    notes: str = Form(""),
    parent1_name: str = Form(""),
    parent1_phone: str = Form(""),
    parent2_name: str = Form(""),
    parent2_phone: str = Form(""),
    tenant: str | None = None,
    db: Session = Depends(get_db),
):
    tctx = _tenant_ctx(db, request, tenant)

    dob = None
    if date_of_birth.strip():
        dob = datetime.strptime(date_of_birth, "%Y-%m-%d").date()

    c = Child(
        tenant_id=tctx.tenant_id,
        full_name=full_name.strip(),
        date_of_birth=dob,
        notes=notes.strip() or None,
        parent1_name=parent1_name.strip() or None,
        parent1_phone=parent1_phone.strip() or None,
        parent2_name=parent2_name.strip() or None,
        parent2_phone=parent2_phone.strip() or None,
    )
    db.add(c)
    db.commit()

    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/children?tenant={tctx.tenant_slug}", status_code=303)


@router.get("/children/{child_id}", response_class=HTMLResponse)
def child_detail(request: Request, child_id: int, tenant: str | None = None, db: Session = Depends(get_db)):
    tctx = _tenant_ctx(db, request, tenant)

    child = db.get(Child, child_id)
    if not child or child.tenant_id != tctx.tenant_id:
        raise HTTPException(404, "Child not found")

    appts = (
        db.query(Appointment)
        .filter(Appointment.tenant_id == tctx.tenant_id)
        .filter(Appointment.child_id == child_id)
        .order_by(Appointment.starts_at.desc())
        .limit(200)
        .all()
    )
    therapists = db.query(Therapist).filter(Therapist.tenant_id == tctx.tenant_id).order_by(Therapist.name.asc()).all()

    uploads = db.query(Attachment).filter(Attachment.child_id == child_id).order_by(Attachment.created_at.desc()).limit(200).all()
    bills = db.query(BillingItem).filter(BillingItem.child_id == child_id).order_by(BillingItem.billing_due.asc()).limit(200).all()
    timeline = db.query(TimelineEvent).filter(TimelineEvent.child_id == child_id).order_by(TimelineEvent.occurred_at.desc()).limit(20).all()

    return _render(
        request,
        "pages/child_detail.html",
        {
            "child": child,
            "appts": appts,
            "uploads": uploads,
            "bills": bills,
            "timeline": timeline,
            "therapists": therapists,
        },
        db,
        tenant_slug=tctx.tenant_slug,
    )


@router.post("/children/{child_id}/upload")
def upload_file(request: Request, child_id: int, file: UploadFile = File(...), tenant: str | None = None, db: Session = Depends(get_db)):
    tctx = _tenant_ctx(db, request, tenant)
    child = db.get(Child, child_id)
    if not child or child.tenant_id != tctx.tenant_id:
        raise HTTPException(404, "Child not found")

    saved = save_upload(child_id=child_id, upload=file)
    db.add(saved)
    db.commit()

    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/children/{child_id}?tenant={tctx.tenant_slug}", status_code=303)


@router.post("/attachments/{attachment_id}/delete")
def delete_attachment(request: Request, attachment_id: int, tenant: str | None = None, db: Session = Depends(get_db)):
    tctx = _tenant_ctx(db, request, tenant)

    a = db.get(Attachment, attachment_id)
    if not a:
        raise HTTPException(404, "File not found")

    # Attachment doesn't carry tenant_id; we validate through its child
    child = db.get(Child, a.child_id)
    if not child or child.tenant_id != tctx.tenant_id:
        raise HTTPException(403, "Forbidden")

    delete_file(a.storage_path)
    db.delete(a)
    db.commit()

    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/children/{a.child_id}?tenant={tctx.tenant_slug}", status_code=303)


# -------------------------
# Therapists (tenant-scoped)
# -------------------------

@router.get("/therapists", response_class=HTMLResponse)
def therapists_list(request: Request, tenant: str | None = None, db: Session = Depends(get_db)):
    tctx = _tenant_ctx(db, request, tenant)
    therapists = db.query(Therapist).filter(Therapist.tenant_id == tctx.tenant_id).order_by(Therapist.name.asc()).all()
    return _render(request, "pages/therapists.html", {"therapists": therapists}, db, tenant_slug=tctx.tenant_slug)


@router.post("/therapists/create")
async def therapist_create(request: Request, tenant: str | None = None, db: Session = Depends(get_db)):
    tctx = _tenant_ctx(db, request, tenant)
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Name is required")

    # FIXED: tenant_id specified ONCE (was duplicated in your broken file)
    t = Therapist(
        tenant_id=tctx.tenant_id,
        name=name,
        phone=(form.get("phone") or "").strip() or None,
        email=(form.get("email") or "").strip() or None,
        role=(form.get("role") or "").strip() or None,
        availability_json=json.dumps({}),
        annual_leave_json=json.dumps([]),
    )
    db.add(t)
    db.commit()

    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/therapists?tenant={tctx.tenant_slug}", status_code=303)


# -------------------------
# Settings (tenant-scoped)
# -------------------------

@router.get("/settings", response_class=HTMLResponse)
def settings_view(request: Request, tenant: str | None = None, db: Session = Depends(get_db)):
    tctx = _tenant_ctx(db, request, tenant)
    clinic, lic = _get_singletons(db, tctx.tenant_id)

    def maps_link() -> str:
        link = (getattr(clinic, "google_maps_link", "") or "").strip()
        if link:
            return link
        addr = (getattr(clinic, "address", "") or "").strip()
        if not addr:
            return ""
        return f"https://www.google.com/maps/search/?api=1&query={quote_plus(addr)}"

    return _render(
        request,
        "pages/settings.html",
        {
            "clinic": clinic,
            "license": lic,
            "google_maps_link": maps_link(),
        },
        db,
        tenant_slug=tctx.tenant_slug,
    )


@router.post("/settings/clinic")
def settings_update_clinic(
    request: Request,
    clinic_name: str = Form(""),
    address: str = Form(""),
    google_maps_link: str = Form(""),
    tenant: str | None = None,
    db: Session = Depends(get_db),
):
    tctx = _tenant_ctx(db, request, tenant)
    clinic, _lic = _get_singletons(db, tctx.tenant_id)

    clinic.clinic_name = (clinic_name or "").strip()
    clinic.address = (address or "").strip()
    if hasattr(clinic, "google_maps_link"):
        clinic.google_maps_link = (google_maps_link or "").strip()

    clinic.updated_at = datetime.utcnow()
    db.add(clinic)
    db.commit()

    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/settings?tenant={tctx.tenant_slug}", status_code=303)


@router.post("/settings/infobip")
def settings_update_infobip(
    request: Request,
    infobip_base_url: str = Form(""),
    infobip_api_key: str = Form(""),
    infobip_sender: str = Form(""),
    infobip_username: str = Form(""),
    infobip_userkey: str = Form(""),
    tenant: str | None = None,
    db: Session = Depends(get_db),
):
    tctx = _tenant_ctx(db, request, tenant)
    clinic, _lic = _get_singletons(db, tctx.tenant_id)

    clinic.infobip_base_url = (infobip_base_url or "").strip()
    clinic.infobip_api_key = (infobip_api_key or "").strip()
    clinic.infobip_sender = (infobip_sender or "").strip()

    if hasattr(clinic, "infobip_username"):
        clinic.infobip_username = (infobip_username or "").strip()
    if hasattr(clinic, "infobip_userkey"):
        clinic.infobip_userkey = (infobip_userkey or "").strip()
    if hasattr(clinic, "sms_provider"):
        clinic.sms_provider = "infobip"

    clinic.updated_at = datetime.utcnow()
    db.add(clinic)
    db.commit()

    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/settings?tenant={tctx.tenant_slug}", status_code=303)


# -------------------------
# Internal endpoints for SMS app (tenant-aware)
# -------------------------

@router.get("/api/internal/clinic_settings")
def api_internal_clinic_settings(request: Request, tenant: str | None = None, db: Session = Depends(get_db)):
    _require_internal(request)
    tctx = _tenant_ctx(db, request, tenant)
    clinic, lic = _get_singletons(db, tctx.tenant_id)

    return {
        "tenant_slug": tctx.tenant_slug,
        "clinic_name": getattr(clinic, "clinic_name", "") or "",
        "address": getattr(clinic, "address", "") or "",
        "google_maps_link": getattr(clinic, "google_maps_link", "") or "",
        "sms_provider": getattr(clinic, "sms_provider", "infobip") or "infobip",
        "infobip_sender": getattr(clinic, "infobip_sender", "") or "",
        "license": {
            "product_mode": getattr(lic, "product_mode", "") or "",
            "trial_end": lic.trial_end.isoformat() if getattr(lic, "trial_end", None) else None,
        },
    }


@router.get("/api/internal/infobip")
def api_internal_infobip(request: Request, tenant: str | None = None, db: Session = Depends(get_db)):
    _require_internal(request)
    tctx = _tenant_ctx(db, request, tenant)
    clinic, _lic = _get_singletons(db, tctx.tenant_id)

    return {
        "tenant_slug": tctx.tenant_slug,
        "sms_provider": getattr(clinic, "sms_provider", "infobip") or "infobip",
        "infobip_base_url": getattr(clinic, "infobip_base_url", "") or "",
        "infobip_api_key": getattr(clinic, "infobip_api_key", "") or "",
        "infobip_sender": getattr(clinic, "infobip_sender", "") or "",
        "infobip_username": getattr(clinic, "infobip_username", "") or "",
        "infobip_userkey": getattr(clinic, "infobip_userkey", "") or "",
    }

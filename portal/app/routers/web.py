from __future__ import annotations

from datetime import datetime, date, timedelta
import uuid
import sqlalchemy as sa
from urllib.parse import quote_plus

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
from app.models.billing import BillingItem
from app.models.session_note import SessionNote
from app.models.clinic_settings import ClinicSettings, AppLicense
from app.models.sms_outbox import SmsOutbox

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
    """
    Enforce that user is logged in AND belongs to this tenant.
    """
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


def _require_internal(request: Request) -> None:
    hdr_new = (request.headers.get("x-internal-key") or request.headers.get("X-Internal-Key") or "").strip()
    hdr_old = (request.headers.get("x-internal-token") or "").strip()

    expected_new = (settings.INTERNAL_API_KEY or "").strip()
    expected_old = (settings.SECRET_KEY or "").strip()

    ok = False
    if expected_new and hdr_new == expected_new:
        ok = True
    if expected_old and hdr_old == expected_old:
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

    base = {
        "request": request,
        "tenant_slug": tctx.tenant_slug,
        "tenant_name": tctx.tenant_name,
        "clinic": cs,
        "license": lic,
        "sms_app_url": _sms_link_for(request, tctx.tenant_slug),
    }
    base.update(ctx or {})
    return templates.TemplateResponse(template_name, base)


def _fmt_dt(v) -> str:
    if not v:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M")
    return str(v)


def _parse_dt(val: str | None) -> datetime | None:
    if not val:
        return None
    v = val.strip()
    if not v:
        return None
    try:
        return datetime.fromisoformat(v)
    except Exception:
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


def _yn(val: str | None) -> str:
    v = (val or "").strip().upper()
    return "YES" if v in ("YES", "Y", "TRUE", "1", "ON") else "NO"


def _child_or_404(db: Session, tenant_id: str, child_id: int) -> Child:
    child = db.query(Child).filter(Child.tenant_id == tenant_id, Child.id == child_id).first()
    if not child:
        raise HTTPException(status_code=404, detail="Not Found")
    return child


def _appointment_or_404(db: Session, tenant_id: str, child_id: int, appt_id: int) -> Appointment:
    appt = (
        db.query(Appointment)
        .filter(
            Appointment.tenant_id == tenant_id,
            Appointment.child_id == child_id,
            Appointment.id == appt_id,
        )
        .first()
    )
    if not appt:
        raise HTTPException(status_code=404, detail="Not Found")
    return appt


# ----------------------------
# Suite
# ----------------------------
@router.get("/suite", response_class=HTMLResponse)
def suite_default(request: Request, db: Session = Depends(get_db)):
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


# ----------------------------
# Legacy compatibility routes
# ----------------------------
@router.get("/billing")
def billing_legacy(request: Request):
    tenant_slug = _session_tenant_slug(request)
    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/t/{tenant_slug}/suite", status_code=303)


@router.get("/appointments")
def appointments_legacy(request: Request):
    tenant_slug = _session_tenant_slug(request)
    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/t/{tenant_slug}/suite", status_code=303)


@router.get("/calendar")
def calendar_legacy(request: Request):
    tenant_slug = _session_tenant_slug(request)
    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/t/{tenant_slug}/suite", status_code=303)


@router.get("/timeline")
def timeline_legacy(request: Request):
    tenant_slug = _session_tenant_slug(request)
    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/t/{tenant_slug}/suite", status_code=303)


# ----------------------------
# SMS Outbox
# ----------------------------
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
        .limit(200)
        .all()
    )
    return _render(
        request,
        "pages/sms_outbox.html",
        {"outbox": items},
        db,
        tenant_slug=tctx.tenant_slug,
    )


# ----------------------------
# Children list
# ----------------------------
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

    # "at a glance" meta
    now = datetime.utcnow()
    next_appt: dict[int, datetime] = {}
    last_att: dict[int, str] = {}
    unpaid_cnt: dict[int, int] = {}

    try:
        rows = db.execute(
            sa.text(
                """
                SELECT child_id, MIN(starts_at) AS next_start
                FROM appointments
                WHERE tenant_id = :tid AND starts_at >= :now
                GROUP BY child_id
                """
            ),
            {"tid": tctx.tenant_id, "now": now},
        ).fetchall()
        next_appt = {int(r[0]): r[1] for r in rows if r and r[0] is not None and r[1] is not None}
    except Exception:
        next_appt = {}

    try:
        rows = db.execute(
            sa.text(
                """
                SELECT a.child_id, a.attendance_status
                FROM appointments a
                JOIN (
                  SELECT child_id, MAX(starts_at) AS mx
                  FROM appointments
                  WHERE tenant_id = :tid
                  GROUP BY child_id
                ) x
                ON a.child_id = x.child_id AND a.starts_at = x.mx
                WHERE a.tenant_id = :tid
                """
            ),
            {"tid": tctx.tenant_id},
        ).fetchall()
        last_att = {int(r[0]): (r[1] or "") for r in rows if r and r[0] is not None}
    except Exception:
        last_att = {}

    try:
        rows = db.execute(
            sa.text(
                """
                SELECT child_id, COUNT(*) AS cnt
                FROM billing_items
                WHERE tenant_id = :tid AND paid = 'NO'
                GROUP BY child_id
                """
            ),
            {"tid": tctx.tenant_id},
        ).fetchall()
        unpaid_cnt = {int(r[0]): int(r[1]) for r in rows if r and r[0] is not None}
    except Exception:
        unpaid_cnt = {}

    meta: dict[int, dict] = {}
    for c in children:
        meta[int(c.id)] = {
            "next_appt": next_appt.get(int(c.id)),
            "last_attendance": last_att.get(int(c.id), ""),
            "unpaid_count": unpaid_cnt.get(int(c.id), 0),
            "p1_phone": getattr(c, "parent1_phone", None),
            "p2_phone": getattr(c, "parent2_phone", None),
        }

    return _render(
        request,
        "pages/children_list.html",
        {"children": children, "q": q, "meta": meta},
        db,
        tenant_slug=tctx.tenant_slug,
    )


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
    redirect = _require_login_for_tenant(request, tenant)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant)

    c = Child(
        tenant_id=tctx.tenant_id,
        full_name=(full_name or "").strip(),
        notes=(notes or "").strip() or None,
    )

    dob = _parse_date(date_of_birth)
    if dob is not None and hasattr(c, "date_of_birth"):
        c.date_of_birth = dob  # type: ignore[attr-defined]

    if hasattr(c, "parent1_name"):
        c.parent1_name = (parent1_name or "").strip() or None  # type: ignore[attr-defined]
    if hasattr(c, "parent1_phone"):
        c.parent1_phone = (parent1_phone or "").strip() or None  # type: ignore[attr-defined]
    if hasattr(c, "parent2_name"):
        c.parent2_name = (parent2_name or "").strip() or None  # type: ignore[attr-defined]
    if hasattr(c, "parent2_phone"):
        c.parent2_phone = (parent2_phone or "").strip() or None  # type: ignore[attr-defined]

    db.add(c)
    db.commit()

    _toast_set(request, "success", "Child created")
    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/children?tenant={tctx.tenant_slug}", status_code=303)


# ----------------------------
# POST: Update parents (tenant-safe)
# ----------------------------
@router.post("/children/{child_id}/parents/update")
def child_update_parents(
    request: Request,
    child_id: int,
    parent1_name: str = Form(""),
    parent1_phone: str = Form(""),
    parent2_name: str = Form(""),
    parent2_phone: str = Form(""),
    db: Session = Depends(get_db),
):
    tenant_slug = _session_tenant_slug(request)
    redirect = _require_login_for_tenant(request, tenant_slug)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant_slug)
    child = _child_or_404(db, tctx.tenant_id, child_id)

    # Only set if fields exist
    if hasattr(child, "parent1_name"):
        child.parent1_name = (parent1_name or "").strip() or None
    if hasattr(child, "parent1_phone"):
        child.parent1_phone = (parent1_phone or "").strip() or None
    if hasattr(child, "parent2_name"):
        child.parent2_name = (parent2_name or "").strip() or None
    if hasattr(child, "parent2_phone"):
        child.parent2_phone = (parent2_phone or "").strip() or None

    db.add(child)
    db.commit()

    _toast_set(request, "success", "Parents updated")
    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/children/{child_id}?tab=parents", status_code=303)


# ----------------------------
# Child detail (tenant-safe)
# ----------------------------
@router.get("/children/{child_id}", response_class=HTMLResponse)
def child_detail(request: Request, child_id: int, tab: str = "overview", db: Session = Depends(get_db)):
    tenant_slug = _session_tenant_slug(request)
    redirect = _require_login_for_tenant(request, tenant_slug)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant_slug)
    child = _child_or_404(db, tctx.tenant_id, child_id)

    appts = (
        db.query(Appointment)
        .filter(Appointment.tenant_id == tctx.tenant_id, Appointment.child_id == child_id)
        .order_by(Appointment.starts_at.desc())
        .limit(200)
        .all()
    )

    bills = (
        db.query(BillingItem)
        .filter(BillingItem.tenant_id == tctx.tenant_id, BillingItem.child_id == child_id)
        .order_by(BillingItem.billing_due.desc())
        .limit(200)
        .all()
    )

    appt_ids = [a.id for a in appts]
    notes_by_appt: dict[int, SessionNote] = {}
    if appt_ids:
        notes = (
            db.query(SessionNote)
            .filter(SessionNote.tenant_id == tctx.tenant_id, SessionNote.appointment_id.in_(appt_ids))
            .all()
        )
        notes_by_appt = {n.appointment_id: n for n in notes}

    toast = _toast_pop(request)
    rp = _rp(request)

    return templates.TemplateResponse(
        "pages/child_detail.html",
        {
            "request": request,
            "rp": rp,
            "tenant_slug": tctx.tenant_slug,
            "child": child,
            "appts": appts,
            "bills": bills,
            "notes_by_appt": notes_by_appt,
            "tab": (tab or "overview"),
            "toast": toast,
            "now": datetime.utcnow(),
            "sms_app_url": _sms_link_for(request, tctx.tenant_slug),
        },
    )


# ----------------------------
# POST: Create appointment
# ----------------------------
@router.post("/children/{child_id}/appointments/create")
def child_create_appointment(
    request: Request,
    child_id: int,
    starts_at: str = Form(...),
    ends_at: str = Form(...),
    therapist_name: str = Form(""),
    procedure: str = Form("Session"),
    attendance_status: str = Form("UNCONFIRMED"),
    db: Session = Depends(get_db),
):
    tenant_slug = _session_tenant_slug(request)
    redirect = _require_login_for_tenant(request, tenant_slug)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant_slug)
    _child_or_404(db, tctx.tenant_id, child_id)

    sdt = _parse_dt(starts_at)
    edt = _parse_dt(ends_at)
    if not sdt or not edt or edt <= sdt:
        _toast_set(request, "danger", "Invalid appointment times")
        rp = _rp(request)
        return RedirectResponse(url=f"{rp}/children/{child_id}?tab=appointments", status_code=303)

    a = Appointment(
        tenant_id=tctx.tenant_id,
        child_id=child_id,
        starts_at=sdt,
        ends_at=edt,
        therapist_name=(therapist_name or "").strip(),
        procedure=(procedure or "Session").strip() or "Session",
        attendance_status=(attendance_status or "UNCONFIRMED").strip().upper() or "UNCONFIRMED",
    )
    db.add(a)
    db.commit()

    _toast_set(request, "success", "Appointment created")
    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/children/{child_id}?tab=appointments", status_code=303)


# ----------------------------
# POST: Update appointment attendance
# ----------------------------
@router.post("/children/{child_id}/appointments/{appointment_id}/attendance")
def appointment_set_attendance(
    request: Request,
    child_id: int,
    appointment_id: int,
    attendance_status: str = Form(...),
    db: Session = Depends(get_db),
):
    tenant_slug = _session_tenant_slug(request)
    redirect = _require_login_for_tenant(request, tenant_slug)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant_slug)
    _child_or_404(db, tctx.tenant_id, child_id)

    appt = _appointment_or_404(db, tctx.tenant_id, child_id, appointment_id)

    status = (attendance_status or "").strip().upper()
    allowed = {"UNCONFIRMED", "ATTENDED", "MISSED", "CANCELLED"}
    if status not in allowed:
        raise HTTPException(status_code=400, detail="Invalid attendance status")

    appt.attendance_status = status
    db.add(appt)
    db.commit()

    _toast_set(request, "success", f"Attendance updated to {status}")
    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/children/{child_id}?tab=appointments", status_code=303)


# ----------------------------
# POST: Create billing item
# ----------------------------
@router.post("/children/{child_id}/billing/create")
def child_create_billing(
    request: Request,
    child_id: int,
    billing_due: str = Form(...),
    paid: str = Form("NO"),
    invoice_created: str = Form("NO"),
    parent_signed_off: str = Form("NO"),
    db: Session = Depends(get_db),
):
    tenant_slug = _session_tenant_slug(request)
    redirect = _require_login_for_tenant(request, tenant_slug)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant_slug)
    _child_or_404(db, tctx.tenant_id, child_id)

    due = _parse_date(billing_due)
    if not due:
        _toast_set(request, "danger", "Invalid billing due date")
        rp = _rp(request)
        return RedirectResponse(url=f"{rp}/children/{child_id}?tab=billing", status_code=303)

    b = BillingItem(
        tenant_id=tctx.tenant_id,
        child_id=child_id,
        billing_due=due,
        paid=_yn(paid),
        invoice_created=_yn(invoice_created),
        parent_signed_off=_yn(parent_signed_off),
    )
    db.add(b)
    db.commit()

    _toast_set(request, "success", "Billing item created")
    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/children/{child_id}?tab=billing", status_code=303)


# ----------------------------
# POST: Set billing flags
# ----------------------------
@router.post("/children/{child_id}/billing/{billing_id}/set_flag")
def billing_set_flag(
    request: Request,
    child_id: int,
    billing_id: int,
    flag: str = Form(...),
    value: str = Form("NO"),
    db: Session = Depends(get_db),
):
    tenant_slug = _session_tenant_slug(request)
    redirect = _require_login_for_tenant(request, tenant_slug)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant_slug)
    _child_or_404(db, tctx.tenant_id, child_id)

    row = (
        db.query(BillingItem)
        .filter(
            BillingItem.tenant_id == tctx.tenant_id,
            BillingItem.child_id == child_id,
            BillingItem.id == billing_id,
        )
        .first()
    )
    if not row:
        raise HTTPException(status_code=404, detail="Not Found")

    f = (flag or "").strip()
    if f not in ("paid", "invoice_created", "parent_signed_off"):
        raise HTTPException(status_code=400, detail="Invalid flag")

    setattr(row, f, _yn(value))
    db.add(row)
    db.commit()

    _toast_set(request, "success", "Billing updated")
    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/children/{child_id}?tab=billing", status_code=303)


# ----------------------------
# POST: Upsert session note for appointment
# ----------------------------
@router.post("/children/{child_id}/appointments/{appointment_id}/note")
def appointment_upsert_note(
    request: Request,
    child_id: int,
    appointment_id: int,
    summary: str = Form(""),
    what_went_wrong: str = Form(""),
    improvements: str = Form(""),
    next_steps: str = Form(""),
    db: Session = Depends(get_db),
):
    tenant_slug = _session_tenant_slug(request)
    redirect = _require_login_for_tenant(request, tenant_slug)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant_slug)
    _child_or_404(db, tctx.tenant_id, child_id)
    appt = _appointment_or_404(db, tctx.tenant_id, child_id, appointment_id)

    note = (
        db.query(SessionNote)
        .filter(SessionNote.tenant_id == tctx.tenant_id, SessionNote.appointment_id == appt.id)
        .first()
    )
    if not note:
        note = SessionNote(tenant_id=tctx.tenant_id, appointment_id=appt.id)

    note.summary = (summary or "").strip() or None
    note.what_went_wrong = (what_went_wrong or "").strip() or None
    note.improvements = (improvements or "").strip() or None
    note.next_steps = (next_steps or "").strip() or None

    db.add(note)
    db.commit()

    _toast_set(request, "success", "Session note saved")
    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/children/{child_id}?tab=notes", status_code=303)


# ----------------------------
# Therapists
# ----------------------------
@router.get("/therapists", response_class=HTMLResponse)
def therapists_list(request: Request, tenant: str = "default", db: Session = Depends(get_db)):
    redirect = _require_login_for_tenant(request, tenant)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant)
    therapists = (
        db.query(Therapist)
        .filter(Therapist.tenant_id == tctx.tenant_id)
        .order_by(Therapist.name.asc())
        .all()
    )
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


# ----------------------------
# Settings
# ----------------------------
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

    # Tenant subscription status (new multi-tenant licensing source of truth)
    sub_active = False
    sub_until = None
    sub_plan_code = ""
    try:
        from app.models.licensing import Subscription, Plan
        sub = (
            db.query(Subscription)
            .filter(Subscription.tenant_id == tctx.tenant_id)
            .order_by(Subscription.ends_at.desc())
            .first()
        )
        if sub and getattr(sub, "ends_at", None):
            sub_until = sub.ends_at
            sub_active = bool(sub.ends_at > datetime.utcnow() and str(getattr(sub, "status", "active")).lower() == "active")
            try:
                p = db.query(Plan).filter(Plan.id == sub.plan_id).first()
                if p:
                    sub_plan_code = getattr(p, "code", "") or ""
            except Exception:
                pass
    except Exception:
        pass

    # Client .env export preview
    env_preview = "".join(
        [
            f"TENANT={tctx.tenant_slug}\n",
            f"PORTAL_BASE_URL={settings.SMS_APP_URL or ''}\n",
            f"INFOBIP_BASE_URL={cs.infobip_base_url or ''}\n",
            f"INFOBIP_API_KEY={(cs.infobip_api_key or '')}\n",
            f"INFOBIP_SENDER={(cs.infobip_sender or '')}\n",
        ]
    )

    return _render(
        request,
        "pages/settings.html",
        {
            "clinic": cs,
            "license": lic,
            "google_maps_link": link,
            "env_preview": env_preview,
            "sub_active": sub_active,
            "sub_until": sub_until,
            "sub_plan_code": sub_plan_code,
        },
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
    _toast_set(request, "success", "Clinic settings updated")
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
    _toast_set(request, "success", "SMS provider settings updated")
    return RedirectResponse(url=f"{rp}/settings?tenant={tctx.tenant_slug}", status_code=303)


@router.get("/settings/env")
def settings_env_download(request: Request, db: Session = Depends(get_db)):
    """Download a simple client.env file for the current tenant."""
    tenant_slug = _session_tenant_slug(request)
    redirect = _require_login_for_tenant(request, tenant_slug)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant_slug)
    cs = _get_settings(db, tctx.tenant_id)

    content = "".join(
        [
            f"TENANT={tctx.tenant_slug}\n",
            f"PORTAL_BASE_URL={str(request.base_url).rstrip('/')}\n",
            f"SMS_APP_URL={settings.SMS_APP_URL or ''}\n",
            f"INFOBIP_BASE_URL={cs.infobip_base_url or ''}\n",
            f"INFOBIP_API_KEY={(cs.infobip_api_key or '')}\n",
            f"INFOBIP_SENDER={(cs.infobip_sender or '')}\n",
        ]
    )

    from fastapi.responses import Response

    return Response(
        content,
        media_type="text/plain",
        headers={"Content-Disposition": f"attachment; filename={tctx.tenant_slug}.client.env"},
    )


@router.post("/settings/activate")
def settings_activate_code(
    request: Request,
    activation_code: str = Form(""),
    db: Session = Depends(get_db),
):
    """Redeem an admin-generated activation code for the current tenant.

    Admin-generated codes are bound to tenant_id (ActivationCode.tenant_id). We only accept codes
    that match this tenant, are not revoked, and have remaining redemptions.

    Renewal rule:
      - if active -> extend from expiry
      - if expired -> extend from now
    """
    tenant_slug = _session_tenant_slug(request)
    redirect = _require_login_for_tenant(request, tenant_slug)
    if redirect:
        return redirect

    code = (activation_code or "").strip()
    rp = _rp(request)
    if not code:
        return RedirectResponse(url=f"{rp}/settings?tenant={tenant_slug}#tab-license&err=missing_code", status_code=303)

    import hashlib
    from app.models.licensing import ActivationCode, Plan, Subscription, LicenseAuditLog

    tctx = resolve_tenant(db, request, tenant_slug=tenant_slug)
    code_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()

    ac = db.query(ActivationCode).filter(ActivationCode.code_hash == code_hash).first()
    if not ac:
        return RedirectResponse(url=f"{rp}/settings?tenant={tenant_slug}#tab-license&err=invalid_code", status_code=303)

    # must match tenant
    if ac.tenant_id != tctx.tenant_id:
        return RedirectResponse(url=f"{rp}/settings?tenant={tenant_slug}#tab-license&err=invalid_code", status_code=303)

    now = datetime.utcnow()
    if ac.revoked_at is not None:
        return RedirectResponse(url=f"{rp}/settings?tenant={tenant_slug}#tab-license&err=invalid_code", status_code=303)
    if ac.redeem_by is not None and ac.redeem_by < now:
        return RedirectResponse(url=f"{rp}/settings?tenant={tenant_slug}#tab-license&err=invalid_code", status_code=303)
    if int(ac.redeemed_count or 0) >= int(ac.max_redemptions or 1):
        return RedirectResponse(url=f"{rp}/settings?tenant={tenant_slug}#tab-license&err=invalid_code", status_code=303)

    plan = db.query(Plan).filter(Plan.id == ac.plan_id).first()
    if not plan:
        return RedirectResponse(url=f"{rp}/settings?tenant={tenant_slug}#tab-license&err=invalid_code", status_code=303)

    sub = (
        db.query(Subscription)
        .filter(Subscription.tenant_id == tctx.tenant_id)
        .order_by(Subscription.ends_at.desc())
        .first()
    )

    base = now
    if sub and sub.ends_at and sub.ends_at > now and str(getattr(sub, "status", "active")).lower() == "active":
        base = sub.ends_at

    new_end = base + timedelta(days=int(plan.duration_days or 0))

    if sub:
        sub.ends_at = new_end
        sub.status = "active"
        sub.plan_id = plan.id
        sub.source = "activation_code"
        db.add(sub)
    else:
        sub = Subscription(
            id=str(uuid.uuid4()),
            tenant_id=tctx.tenant_id,
            plan_id=plan.id,
            status="active",
            starts_at=now,
            ends_at=new_end,
            source="activation_code",
        )
        db.add(sub)

    ac.redeemed_count = int(ac.redeemed_count or 0) + 1
    db.add(ac)

    try:
        db.add(
            LicenseAuditLog(
                id=str(uuid.uuid4()),
                tenant_id=tctx.tenant_id,
                event_type="activation_redeemed",
                details_json=f'{{"plan":"{plan.code}","ends_at":"{new_end.isoformat()}"}}',
                created_at=now,
            )
        )
    except Exception:
        pass

    db.commit()

    return RedirectResponse(url=f"{rp}/settings?tenant={tenant_slug}#tab-license&ok=activated", status_code=303)


@router.post("/settings/license")
def settings_manual_license(
    request: Request,
    product_mode: str = Form("BOTH"),
    action: str = Form("TRIAL"),
    weeks: int = Form(4),
    db: Session = Depends(get_db),
):
    """Legacy manual license controls (kept for demos).

    This updates the single-row AppLicense record.
    Production licensing should use subscriptions + activation codes.
    """
    tenant_slug = _session_tenant_slug(request)
    redirect = _require_login_for_tenant(request, tenant_slug)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant_slug)
    lic = _get_license(db)
    lic.product_mode = (product_mode or "BOTH").upper()
    now = datetime.utcnow()

    act = (action or "").upper()
    w = max(1, int(weeks or 4))

    if act in ("TRIAL", "RENEW_WEEKS"):
        end = (lic.license_end or now) if (lic.license_end and lic.license_end > now) else now
        lic.license_end = end + timedelta(weeks=w)
    elif act == "RENEW_YEAR":
        end = (lic.license_end or now) if (lic.license_end and lic.license_end > now) else now
        lic.license_end = end + timedelta(days=365)
    else:
        # default: extend trial
        end = (lic.trial_end or now) if (lic.trial_end and lic.trial_end > now) else now
        lic.trial_end = end + timedelta(weeks=w)

    lic.updated_at = now
    db.add(lic)
    db.commit()

    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/settings?tenant={tctx.tenant_slug}#tab-license", status_code=303)


# ----------------------------
# Internal endpoints for SMS service
# ----------------------------
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

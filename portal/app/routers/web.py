from __future__ import annotations

from datetime import datetime, timedelta, date
import json
from fastapi import APIRouter, Depends, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.child import Child
from app.models.appointment import Appointment
from app.models.session_note import SessionNote
from app.models.attachment import Attachment
from app.models.billing import BillingItem
from app.models.billing_plan import BillingPlan
from app.models.timeline import TimelineEvent
from app.models.clinic_settings import ClinicSettings, AppLicense
from app.models.therapist import Therapist
from urllib.parse import quote_plus
from app.services.storage import save_upload, delete_file
from app.config import settings
from app.services.license_tokens import verify_activation_code

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _get_singletons(db: Session):
    clinic = db.get(ClinicSettings, 1)
    if not clinic:
        clinic = ClinicSettings(id=1)
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


def _render(request: Request, template_name: str, ctx: dict, db: Session):
    clinic, lic = _get_singletons(db)
    base = {
        "request": request,
        "user_name": "PAUL PORTAL TEST",
        "clinic": clinic,
        "license": lic,
        "sms_app_url": (settings.SMS_APP_URL.strip() or "/sms/"),
    }
    base.update(ctx)
    return templates.TemplateResponse(template_name, base)


def _rp(request: Request) -> str:
    """Return the URL prefix when served behind the nginx gateway.

    With the Docker gateway we serve this app under /therapy and start uvicorn with
    --root-path /therapy. In direct mode (http://localhost:8010) root_path is empty.
    """
    return request.scope.get("root_path", "") or ""


def _prefix_with_rp(url: str, rp: str) -> str:
    """Prefix absolute paths with root_path when needed."""
    if not url:
        return url
    if not rp:
        return url
    if not url.startswith("/"):
        return url
    if url.startswith(rp + "/") or url == rp:
        return url
    return rp + url

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

@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    past = db.query(Appointment).order_by(Appointment.starts_at.desc()).limit(25).all()
    return _render(request, "pages/dashboard.html", {
        "past": past,
        "children_count": db.query(Child).count(),
        "appt_count": db.query(Appointment).count(),
        "uploads_count": db.query(Attachment).count(),
        "billing_count": db.query(BillingItem).count(),
    }, db)


@router.get("/suite", response_class=HTMLResponse)
def clinic_suite(request: Request, db: Session = Depends(get_db)):
    return _render(request, "pages/suite.html", {}, db)

@router.get("/calendar", response_class=HTMLResponse)
def calendar_view(request: Request, child_id: int | None = None, db: Session = Depends(get_db)):
    children = db.query(Child).order_by(Child.full_name.asc()).all()
    therapists = db.query(Therapist).order_by(Therapist.name.asc()).all()
    return _render(request, "pages/calendar.html", {
        "children": children,
        "selected_child_id": child_id,
        "therapists": therapists,
    }, db)

@router.get("/api/calendar_events")
def calendar_events(request: Request, start: str | None = None, end: str | None = None, child_id: int | None = None, db: Session = Depends(get_db)):
    start_dt = datetime.fromisoformat(start) if start else datetime.now() - timedelta(days=30)
    end_dt = datetime.fromisoformat(end) if end else datetime.now() + timedelta(days=90)
    events = []

    rp = _rp(request)

    aq = db.query(Appointment).filter(Appointment.starts_at >= start_dt, Appointment.starts_at < end_dt)
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
        events.append({
            "id": f"appt_{a.id}",
            "title": f"Appt: {a.child.full_name} · {a.procedure}",
            "start": a.starts_at.isoformat(),
            "url": _prefix_with_rp(f"/appointments/{a.id}", rp),
            "backgroundColor": color,
            "borderColor": color,
        })

    bq = db.query(BillingItem).filter(BillingItem.billing_due >= start_dt.date(), BillingItem.billing_due < end_dt.date())
    if child_id is not None:
        bq = bq.filter(BillingItem.child_id == child_id)
    for b in bq.all():
        inv = (b.invoice_created or "NO").upper()
        paid = (b.paid or "NO").upper()
        if paid == "YES":
            color = "#22c55e"
            tag = "Paid"
        elif inv == "YES":
            color = "#eab308"
            tag = "Invoice Created"
        else:
            color = "#a855f7"
            tag = "No Invoice"
        events.append({
            "id": f"bill_{b.id}",
            "title": f"Billing: {b.child.full_name} · {tag}",
            "start": datetime.combine(b.billing_due, datetime.min.time()).isoformat(),
            "allDay": True,
            "url": _prefix_with_rp(f"/billing?child_id={b.child_id}", rp),
            "backgroundColor": color,
            "borderColor": color,
        })

    # Timeline journey events (payments, invoice issued, communications, exercises etc.)
    tq = db.query(TimelineEvent).filter(TimelineEvent.occurred_at >= start_dt, TimelineEvent.occurred_at < end_dt)
    if child_id is not None:
        tq = tq.filter(TimelineEvent.child_id == child_id)

    type_color = {
        "VISIT": "#2563eb",
        "PAYMENT": "#22c55e",
        "INVOICE_ISSUED": "#eab308",
        "EXERCISE": "#a855f7",
        "PARENT_FEEDBACK": "#06b6d4",
        "COMMUNICATION": "#64748b",
        "APPT_CANCELLED": "#f97316",
        "NOTE": "#475569",
        "OTHER": "#94a3b8",
    }

    for t in tq.order_by(TimelineEvent.occurred_at.asc()).all():
        et = (t.event_type or "OTHER").upper()
        color = type_color.get(et, "#94a3b8")
        events.append({
            "id": f"tl_{t.id}",
            "title": f"Journey: {t.child.full_name} · {t.title}",
            "start": t.occurred_at.isoformat(),
            "allDay": False,
            "url": _prefix_with_rp(f"/timeline?child_id={t.child_id}&event_type={et}", rp),
            "backgroundColor": color,
            "borderColor": color,
        })

    return JSONResponse(events)


# -----------------
# Calendar Quick Add
# -----------------

@router.post("/calendar/add_appointment")
def calendar_add_appointment(
    request: Request,
    child_id: int = Form(...),
    starts_at: str = Form(...),
    therapist_name: str = Form(...),
    procedure: str = Form("Office Visit"),
    also_add_timeline: str = Form("YES"),
    db: Session = Depends(get_db),
):
    if not db.get(Child, child_id):
        raise HTTPException(404, "Child not found")
    dt = datetime.strptime(starts_at, "%Y-%m-%dT%H:%M")
    appt = Appointment(
        child_id=child_id,
        starts_at=dt,
        therapist_name=therapist_name.strip(),
        procedure=procedure.strip() or "Office Visit",
        attendance_status="UNCONFIRMED",
    )
    db.add(appt)
    db.flush()

    if (also_add_timeline or "YES").strip().upper() == "YES":
        db.add(TimelineEvent(
            child_id=child_id,
            event_type="VISIT",
            title=f"Visit scheduled: {appt.procedure}",
            details=f"Therapist: {appt.therapist_name}",
            occurred_at=dt,
        ))

    db.commit()
    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/calendar?child_id={child_id}", status_code=303)


@router.post("/calendar/add_billing")
def calendar_add_billing(
    request: Request,
    child_id: int = Form(...),
    billing_due: str = Form(...),
    amount_eur: str = Form(""),
    description: str = Form(""),
    invoice_created: str = Form("NO"),
    paid: str = Form("NO"),
    parent_signed_off: str = Form("NO"),
    db: Session = Depends(get_db),
):
    if not db.get(Child, child_id):
        raise HTTPException(404, "Child not found")

    due = datetime.strptime(billing_due, "%Y-%m-%d").date()

    def _yn(v: str) -> str:
        return "YES" if (v or "").strip().upper() == "YES" else "NO"

    amt = None
    try:
        amt = float(amount_eur) if str(amount_eur).strip() else None
    except Exception:
        amt = None

    b = BillingItem(
        child_id=child_id,
        billing_due=due,
        amount_eur=amt,
        description=description.strip() or None,
        invoice_created=_yn(invoice_created),
        paid=_yn(paid),
        parent_signed_off=_yn(parent_signed_off),
    )
    if b.paid == "YES":
        b.invoice_created = "YES"

    db.add(b)

    # Add a journey event so billing shows in the timeline too.
    event_dt = datetime.combine(due, datetime.min.time()).replace(hour=9)
    if b.paid == "YES":
        et = "PAYMENT"
        title = "Payment received"
    elif b.invoice_created == "YES":
        et = "INVOICE_ISSUED"
        title = "Invoice issued"
    else:
        et = "OTHER"
        title = "Billing due"

    details = []
    if b.amount_eur is not None:
        details.append(f"Amount: €{b.amount_eur:.2f}")
    if b.description:
        details.append(b.description)
    db.add(TimelineEvent(
        child_id=child_id,
        event_type=et,
        title=title,
        details=" · ".join(details) or None,
        occurred_at=event_dt,
    ))

    db.commit()
    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/calendar?child_id={child_id}", status_code=303)


@router.post("/calendar/add_journey")
def calendar_add_journey(
    request: Request,
    child_id: int = Form(...),
    event_type: str = Form("OTHER"),
    occurred_at: str = Form(...),
    title: str = Form(...),
    details: str = Form(""),
    db: Session = Depends(get_db),
):
    # Reuse the same validation as timeline_create, but redirect back to calendar.
    if not db.get(Child, child_id):
        raise HTTPException(404, "Child not found")

    et = (event_type or "OTHER").strip().upper()
    if et not in set(TIMELINE_TYPES):
        et = "OTHER"

    try:
        dt = datetime.strptime(occurred_at, "%Y-%m-%dT%H:%M")
    except Exception:
        dt = datetime.fromisoformat(occurred_at)

    db.add(TimelineEvent(
        child_id=child_id,
        event_type=et,
        title=title.strip() or "(Untitled)",
        details=details.strip() or None,
        occurred_at=dt,
    ))
    db.commit()
    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/calendar?child_id={child_id}", status_code=303)

@router.get("/appointments", response_class=HTMLResponse)
def past_appointments(request: Request, child_id: int | None = None, db: Session = Depends(get_db)):
    q = db.query(Appointment)
    if child_id is not None:
        q = q.filter(Appointment.child_id == child_id)
    rows = q.order_by(Appointment.starts_at.desc()).limit(200).all()
    children = db.query(Child).order_by(Child.full_name.asc()).all()
    return templates.TemplateResponse("pages/past_appointments.html", {
        "request": request,
        "rows": rows,
        "children": children,
        "selected_child_id": child_id,
        "user_name": "PAUL PORTAL TEST",
    })

@router.post("/appointments/{appt_id}/attendance")
def mark_attendance(request: Request, appt_id: int, attendance_status: str = Form(...), attendance_note: str = Form(""), db: Session = Depends(get_db)):
    appt = db.get(Appointment, appt_id)
    if not appt:
        raise HTTPException(404, "Appointment not found")

    allowed = {"CONFIRMED","UNCONFIRMED","CANCELLED_PROVIDER","CANCELLED_ME","MISSED","ATTENDED"}
    status = attendance_status.strip().upper()
    if status not in allowed:
        raise HTTPException(400, f"Invalid status: {status}")

    prev = (appt.attendance_status or "").upper()
    appt.attendance_status = status
    appt.attendance_note = attendance_note.strip() or None
    appt.attendance_marked_at = datetime.now()
    # Also create a Journey item so attendance changes are visible on Timeline + Calendar.
    # (This does not change appointment logic, only adds audit events.)
    if status != prev:
        if status in {"MISSED", "ATTENDED"}:
            et = "VISIT"
            title = f"Visit marked: {status}"
        elif status in {"CANCELLED_PROVIDER", "CANCELLED_ME"}:
            et = "APPT_CANCELLED"
            title = f"Appointment cancelled: {status}"
        else:
            et = "COMMUNICATION"
            title = f"Appointment status: {status}"
        db.add(TimelineEvent(
            child_id=appt.child_id,
            event_type=et,
            title=title,
            details=(appt.attendance_note or None),
            occurred_at=datetime.now(),
        ))

    db.commit()
    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/appointments/{appt_id}", status_code=303)

@router.get("/billing", response_class=HTMLResponse)
def billing_view(request: Request, child_id: int | None = None, mode: str = "display", db: Session = Depends(get_db)):
    children = db.query(Child).order_by(Child.full_name.asc()).all()
    q = db.query(BillingItem).order_by(BillingItem.billing_due.asc())
    if child_id is not None:
        q = q.filter(BillingItem.child_id == child_id)
    items = q.limit(500).all()
    return templates.TemplateResponse("pages/billing.html", {
        "request": request,
        "children": children,
        "selected_child_id": child_id,
        "items": items,
        "mode": (mode or "display").lower(),
        "user_name": "PAUL PORTAL TEST",
    })

@router.post("/billing/{bill_id}/update")
def billing_update(
    request: Request,
    bill_id: int,
    paid: str = Form("NO"),
    invoice_created: str = Form("NO"),
    parent_signed_off: str = Form("NO"),
    child_id: str = Form(""),
    redirect: str = Form(""),
    db: Session = Depends(get_db),
):
    b = db.get(BillingItem, bill_id)
    if not b:
        raise HTTPException(404, "Billing row not found")

    def _yn(v: str) -> str:
        return "YES" if (v or "").strip().upper() == "YES" else "NO"

    prev_paid = (b.paid or "NO").upper()
    prev_inv = (b.invoice_created or "NO").upper()
    prev_sign = (b.parent_signed_off or "NO").upper()

    b.paid = _yn(paid)
    b.invoice_created = _yn(invoice_created)
    b.parent_signed_off = _yn(parent_signed_off)

    # Small convenience rule: if paid YES, invoice_created should be YES too (can still be edited later)
    if b.paid == "YES":
        b.invoice_created = "YES"

    # Add Journey audit events when billing status changes (for calendar visibility)
    changed = (b.paid != prev_paid) or (b.invoice_created != prev_inv) or (b.parent_signed_off != prev_sign)
    if changed:
        parts = []
        if b.invoice_created != prev_inv:
            parts.append(f"Invoice created: {prev_inv} → {b.invoice_created}")
        if b.paid != prev_paid:
            parts.append(f"Paid: {prev_paid} → {b.paid}")
        if b.parent_signed_off != prev_sign:
            parts.append(f"Parent sign-off: {prev_sign} → {b.parent_signed_off}")

        if b.paid == "YES":
            et = "PAYMENT"
            title = "Payment received"
        elif b.invoice_created == "YES":
            et = "INVOICE_ISSUED"
            title = "Invoice issued"
        else:
            et = "OTHER"
            title = "Billing updated"

        db.add(TimelineEvent(
            child_id=b.child_id,
            event_type=et,
            title=title,
            details=" · ".join(parts) if parts else None,
            occurred_at=datetime.now(),
        ))

    db.commit()

    # Redirect back to whichever page triggered the update
    rp = _rp(request)

    if redirect.strip():
        return RedirectResponse(url=_prefix_with_rp(redirect.strip(), rp), status_code=303)

    if child_id.strip():
        return RedirectResponse(url=f"{rp}/billing?child_id={child_id}", status_code=303)

    return RedirectResponse(url=f"{rp}/billing", status_code=303)



@router.get("/children", response_class=HTMLResponse)
def children_list(request: Request, q: str | None = None, db: Session = Depends(get_db)):
    query = db.query(Child)
    if q:
        query = query.filter(Child.full_name.ilike(f"%{q}%"))
    children = query.order_by(Child.full_name.asc()).limit(300).all()
    return _render(request, "pages/children_list.html", {
        "children": children,
        "q": q or "",
    }, db)

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
    db: Session = Depends(get_db),
):
    dob = None
    if date_of_birth.strip():
        dob = datetime.strptime(date_of_birth, "%Y-%m-%d").date()
    c = Child(
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
    return RedirectResponse(url=f"{rp}/children", status_code=303)

@router.get("/children/{child_id}", response_class=HTMLResponse)
def child_detail(request: Request, child_id: int, db: Session = Depends(get_db)):
    child = db.get(Child, child_id)
    if not child:
        raise HTTPException(404, "Child not found")

    appts = db.query(Appointment).filter(Appointment.child_id == child_id).order_by(Appointment.starts_at.desc()).limit(200).all()
    uploads = db.query(Attachment).filter(Attachment.child_id == child_id).order_by(Attachment.created_at.desc()).limit(200).all()
    bills = db.query(BillingItem).filter(BillingItem.child_id == child_id).order_by(BillingItem.billing_due.asc()).limit(200).all()
    timeline = db.query(TimelineEvent).filter(TimelineEvent.child_id == child_id).order_by(TimelineEvent.occurred_at.desc()).limit(20).all()

    therapists = db.query(Therapist).order_by(Therapist.name.asc()).all()
    return _render(request, "pages/child_detail.html", {
        "child": child,
        "appts": appts,
        "uploads": uploads,
        "bills": bills,
        "timeline": timeline,
        "therapists": therapists,
    }, db)


# -----------------
# Therapists
# -----------------

WEEKDAYS = [
    ("mon", "Monday"),
    ("tue", "Tuesday"),
    ("wed", "Wednesday"),
    ("thu", "Thursday"),
    ("fri", "Friday"),
    ("sat", "Saturday"),
    ("sun", "Sunday"),
]


def _parse_availability(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _parse_leaves(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _time_to_hours(t: str) -> float:
    # expects HH:MM
    try:
        hh, mm = t.split(":")
        return int(hh) + int(mm) / 60.0
    except Exception:
        return 0.0


def _weekly_hours(avail: dict) -> float:
    total = 0.0
    for k, _label in WEEKDAYS:
        blocks = avail.get(k, []) if isinstance(avail.get(k, []), list) else []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            total += max(0.0, _time_to_hours(str(b.get("end", "0:0"))) - _time_to_hours(str(b.get("start", "0:0"))))
    return round(total, 2)


def _month_hours(avail: dict, leaves: list[dict], year: int, month: int) -> float:
    # Sum availability for each day in the month, excluding leave days.
    # This gives a realistic monthly total compared to weekly*4.
    from calendar import monthrange

    leave_days: set[date] = set()
    for l in leaves:
        try:
            s = datetime.strptime(l.get("start", ""), "%Y-%m-%d").date()
            e = datetime.strptime(l.get("end", ""), "%Y-%m-%d").date()
            cur = s
            while cur <= e:
                leave_days.add(cur)
                cur = cur + timedelta(days=1)
        except Exception:
            continue

    days_in_month = monthrange(year, month)[1]
    total = 0.0
    for d in range(1, days_in_month + 1):
        dt = date(year, month, d)
        if dt in leave_days:
            continue
        weekday = dt.weekday()  # 0=Mon
        key = ["mon","tue","wed","thu","fri","sat","sun"][weekday]
        blocks = avail.get(key, []) if isinstance(avail.get(key, []), list) else []
        for b in blocks:
            if not isinstance(b, dict):
                continue
            total += max(0.0, _time_to_hours(str(b.get("end", "0:0"))) - _time_to_hours(str(b.get("start", "0:0"))))
    return round(total, 2)


@router.get("/therapists", response_class=HTMLResponse)
def therapists_list(request: Request, db: Session = Depends(get_db)):
    therapists = db.query(Therapist).order_by(Therapist.name.asc()).all()
    return _render(request, "pages/therapists.html", {
        "therapists": therapists,
        "weekdays": WEEKDAYS,
    }, db)


@router.post("/therapists/create")
async def therapist_create(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    name = (form.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Name is required")
    t = Therapist(
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
    return RedirectResponse(url=f"{rp}/therapists", status_code=303)


@router.get("/therapists/{therapist_id}", response_class=HTMLResponse)
def therapist_detail(request: Request, therapist_id: int, db: Session = Depends(get_db)):
    t = db.get(Therapist, therapist_id)
    if not t:
        raise HTTPException(404, "Therapist not found")
    avail = _parse_availability(t.availability_json)
    leaves = _parse_leaves(t.annual_leave_json)
    now = datetime.utcnow()
    weekly = _weekly_hours(avail)
    monthly = _month_hours(avail, leaves, now.year, now.month)
    return _render(request, "pages/therapist_detail.html", {
        "t": t,
        "avail": avail,
        "leaves": leaves,
        "weekdays": WEEKDAYS,
        "weekly_hours": weekly,
        "month_hours": monthly,
        "month_label": now.strftime("%B %Y"),
    }, db)


@router.post("/therapists/{therapist_id}/update")
async def therapist_update(request: Request, therapist_id: int, db: Session = Depends(get_db)):
    t = db.get(Therapist, therapist_id)
    if not t:
        raise HTTPException(404, "Therapist not found")
    form = await request.form()
    t.name = (form.get("name") or t.name).strip()
    t.phone = (form.get("phone") or "").strip() or None
    t.email = (form.get("email") or "").strip() or None
    t.role = (form.get("role") or "").strip() or None

    # Availability blocks: fields like mon_start1, mon_end1, mon_start2, mon_end2 ...
    avail: dict = {}
    for key, _label in WEEKDAYS:
        blocks: list[dict] = []
        for idx in (1, 2):
            s = (form.get(f"{key}_start{idx}") or "").strip()
            e = (form.get(f"{key}_end{idx}") or "").strip()
            if s and e:
                blocks.append({"start": s, "end": e})
        if blocks:
            avail[key] = blocks
    t.availability_json = json.dumps(avail)

    db.add(t)
    db.commit()
    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/therapists/{therapist_id}", status_code=303)


@router.post("/therapists/{therapist_id}/leave/add")
async def therapist_leave_add(request: Request, therapist_id: int, db: Session = Depends(get_db)):
    t = db.get(Therapist, therapist_id)
    if not t:
        raise HTTPException(404, "Therapist not found")
    form = await request.form()
    start = (form.get("start") or "").strip()
    end = (form.get("end") or "").strip()
    reason = (form.get("reason") or "").strip()
    leaves = _parse_leaves(t.annual_leave_json)
    if start and end:
        leaves.append({"start": start, "end": end, "reason": reason})
        t.annual_leave_json = json.dumps(leaves)
        db.add(t)
        db.commit()
    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/therapists/{therapist_id}", status_code=303)


@router.post("/therapists/{therapist_id}/leave/remove")
async def therapist_leave_remove(request: Request, therapist_id: int, idx: int = Form(...), db: Session = Depends(get_db)):
    t = db.get(Therapist, therapist_id)
    if not t:
        raise HTTPException(404, "Therapist not found")
    leaves = _parse_leaves(t.annual_leave_json)
    if 0 <= idx < len(leaves):
        leaves.pop(idx)
        t.annual_leave_json = json.dumps(leaves)
        db.add(t)
        db.commit()
    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/therapists/{therapist_id}", status_code=303)


# -----------------
# Timeline Journey
# -----------------

TIMELINE_TYPES = [
    "VISIT",
    "PAYMENT",
    "INVOICE_ISSUED",
    "EXERCISE",
    "PARENT_FEEDBACK",
    "COMMUNICATION",
    "APPT_CANCELLED",
    "NOTE",
    "OTHER",
]


@router.get("/timeline", response_class=HTMLResponse)
def timeline_view(request: Request, child_id: int | None = None, event_type: str | None = None, db: Session = Depends(get_db)):
    children = db.query(Child).order_by(Child.full_name.asc()).all()
    # Important: apply filters BEFORE limit/offset.
    # SQLAlchemy raises InvalidRequestError if filter() is called after limit().
    q = db.query(TimelineEvent)
    if child_id is not None:
        q = q.filter(TimelineEvent.child_id == child_id)
    if event_type and event_type.strip() and event_type.strip().upper() != "ALL":
        q = q.filter(TimelineEvent.event_type == event_type.strip().upper())

    events = q.order_by(TimelineEvent.occurred_at.desc()).limit(800).all()
    return templates.TemplateResponse("pages/timeline.html", {
        "request": request,
        "children": children,
        "selected_child_id": child_id,
        "selected_event_type": (event_type or "ALL").upper(),
        "types": TIMELINE_TYPES,
        "events": events,
        "user_name": "PAUL PORTAL TEST",
    })


@router.post("/timeline/create")
def timeline_create(
    request: Request,
    child_id: int = Form(...),
    event_type: str = Form(...),
    occurred_at: str = Form(...),
    title: str = Form(...),
    details: str = Form(""),
    db: Session = Depends(get_db),
):
    if not db.get(Child, child_id):
        raise HTTPException(404, "Child not found")

    et = (event_type or "OTHER").strip().upper()
    if et not in set(TIMELINE_TYPES):
        et = "OTHER"

    # datetime-local -> "YYYY-MM-DDTHH:MM"
    try:
        dt = datetime.strptime(occurred_at, "%Y-%m-%dT%H:%M")
    except Exception:
        # Allow ISO format fallback
        dt = datetime.fromisoformat(occurred_at)

    ev = TimelineEvent(
        child_id=child_id,
        event_type=et,
        title=title.strip() or "(Untitled)",
        details=details.strip() or None,
        occurred_at=dt,
    )
    db.add(ev)
    db.commit()

    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/timeline?child_id={child_id}", status_code=303)

@router.post("/children/{child_id}/appointments/create")
def appointment_create(request: Request, child_id: int, starts_at: str = Form(...), therapist_name: str = Form(...), procedure: str = Form("Office Visit"), db: Session = Depends(get_db)):
    if not db.get(Child, child_id):
        raise HTTPException(404, "Child not found")
    dt = datetime.strptime(starts_at, "%Y-%m-%dT%H:%M")
    appt = Appointment(child_id=child_id, starts_at=dt, therapist_name=therapist_name.strip(), procedure=procedure.strip() or "Office Visit", attendance_status="UNCONFIRMED")
    db.add(appt)
    db.commit()
    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/children/{child_id}", status_code=303)

@router.get("/appointments/{appt_id}", response_class=HTMLResponse)
def session_detail(request: Request, appt_id: int, db: Session = Depends(get_db)):
    appt = db.get(Appointment, appt_id)
    if not appt:
        raise HTTPException(404, "Appointment not found")
    note = appt.session_note or SessionNote(appointment_id=appt_id)
    uploads = db.query(Attachment).filter(Attachment.child_id == appt.child_id).order_by(Attachment.created_at.desc()).limit(200).all()
    return templates.TemplateResponse("pages/session_detail.html", {"request": request, "appt": appt, "note": note, "uploads": uploads, "user_name": "PAUL PORTAL TEST"})

@router.post("/appointments/{appt_id}/note")
def save_note(request: Request, appt_id: int, summary: str = Form(""), what_went_wrong: str = Form(""), improvements: str = Form(""), next_steps: str = Form(""), db: Session = Depends(get_db)):
    appt = db.get(Appointment, appt_id)
    if not appt:
        raise HTTPException(404, "Appointment not found")
    note = appt.session_note
    if not note:
        note = SessionNote(appointment_id=appt_id)
        db.add(note)
    note.summary = summary.strip() or None
    note.what_went_wrong = what_went_wrong.strip() or None
    note.improvements = improvements.strip() or None
    note.next_steps = next_steps.strip() or None
    db.commit()
    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/appointments/{appt_id}", status_code=303)

@router.post("/children/{child_id}/upload")
def upload_file(request: Request, child_id: int, file: UploadFile = File(...), db: Session = Depends(get_db)):
    if not db.get(Child, child_id):
        raise HTTPException(404, "Child not found")
    saved = save_upload(child_id=child_id, upload=file)
    db.add(saved)
    db.commit()
    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/children/{child_id}", status_code=303)

@router.get("/files/{attachment_id}")
def open_file(attachment_id: int, db: Session = Depends(get_db)):
    a = db.get(Attachment, attachment_id)
    if not a:
        raise HTTPException(404, "File not found")
    return FileResponse(a.storage_path, media_type=a.mime_type, filename=a.original_name)

@router.post("/attachments/{attachment_id}/delete")
def delete_attachment(request: Request, attachment_id: int, db: Session = Depends(get_db)):
    a = db.get(Attachment, attachment_id)
    if not a:
        raise HTTPException(404, "File not found")
    child_id = a.child_id
    delete_file(a.storage_path)
    db.delete(a)
    db.commit()
    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/children/{child_id}", status_code=303)

@router.get("/messages", response_class=HTMLResponse)
def messages(request: Request, db: Session = Depends(get_db)):
    mock_messages = [
        {"direction": "OUT", "to": "Parent of Maria K.", "channel": "SMS", "subject": "Reminder", "status": "SENT", "when": "2026-01-19 18:05"},
        {"direction": "IN", "to": "Clinic", "channel": "SMS", "subject": "Will arrive 10min late", "status": "RECEIVED", "when": "2026-01-18 16:44"},
        {"direction": "OUT", "to": "Parent of James S.", "channel": "Email", "subject": "Invoice issued", "status": "DELIVERED", "when": "2026-01-17 09:12"},
    ]
    return _render(request, "pages/messages.html", {"header": "Messages", "rows": mock_messages}, db)

@router.get("/profile", response_class=HTMLResponse)
def profile(request: Request, db: Session = Depends(get_db)):
    profile_data = {
        "display_name": "PAUL PORTAL TEST",
        "clinic": "1st Providers Choice",
        "role": "Admin",
        "timezone": "Europe/Athens",
        "default_channel": "SMS",
    }
    return _render(request, "pages/profile.html", {"header": "Profile", "p": profile_data}, db)

@router.get("/questionnaires", response_class=HTMLResponse)
def questionnaires(request: Request, db: Session = Depends(get_db)):
    mock_q = [
        {"child": "Maria K.", "name": "Initial Intake", "status": "COMPLETED", "when": "2026-01-10"},
        {"child": "James S.", "name": "Progress Check", "status": "PENDING", "when": "2026-01-20"},
        {"child": "Theo P.", "name": "Parent Feedback", "status": "IN_REVIEW", "when": "2026-01-15"},
    ]
    return _render(request, "pages/questionnaires.html", {"header": "Questionnaires", "rows": mock_q}, db)


# -----------------
# Clinic Setup (per client)
# -----------------

@router.get("/settings", response_class=HTMLResponse)
def settings_view(request: Request, db: Session = Depends(get_db)):
    children = db.query(Child).order_by(Child.full_name.asc()).all()
    clinic, lic = _get_singletons(db)

    def maps_link() -> str:
        # Use address if available, otherwise fall back to coordinates.
        q = (clinic.address or "").strip()
        if not q and clinic.lat is not None and clinic.lng is not None:
            q = f"{clinic.lat},{clinic.lng}"
        if not q:
            return ""
        return f"https://www.google.com/maps/search/?api=1&query={quote_plus(q)}"

    def env_preview() -> str:
        # NOTE: This is a convenience export for client installs.
        # API key is shown here because the user is in the local admin UI.
        lines = [
            "SMS_PROVIDER=infobip",
            f"INFOBIP_BASE_URL=\"{(clinic.infobip_base_url or '').strip()}\"",
            f"INFOBIP_API_KEY=\"{(clinic.infobip_api_key or '').strip()}\"",
            f"INFOBIP_FROM=\"{(clinic.infobip_sender or '').strip()}\"",
            f"INFOBIP_USERNAME=\"{(getattr(clinic,'infobip_username','') or '').strip()}\"",
            f"INFOBIP_USERKEY=\"{(getattr(clinic,'infobip_userkey','') or '').strip()}\"",
        ]
        return "\n".join(lines) + "\n"
    return _render(
        request,
        "pages/settings.html",
        {
            "header": "Clinic Setup",
            "children": children,
            "clinic": clinic,
            "license": lic,
            "sms_app_url": (settings.SMS_APP_URL.strip() or "/sms/"),
            "google_maps_link": maps_link(),
            "env_preview": env_preview(),
        },
        db,
    )


@router.post("/settings/clinic")
def settings_update_clinic(
    request: Request,
    clinic_name: str = Form(""),
    address: str = Form(""),
    lat: str = Form(""),
    lng: str = Form(""),
    db: Session = Depends(get_db),
):
    clinic, _lic = _get_singletons(db)
    clinic.clinic_name = (clinic_name or "").strip() or clinic.clinic_name
    clinic.address = (address or "").strip()
    try:
        clinic.lat = float(lat) if str(lat).strip() else None
    except Exception:
        clinic.lat = None
    try:
        clinic.lng = float(lng) if str(lng).strip() else None
    except Exception:
        clinic.lng = None
    clinic.updated_at = datetime.utcnow()
    db.add(clinic)
    db.commit()
    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/settings", status_code=303)


@router.post("/settings/infobip")
def settings_update_infobip(
    request: Request,
    infobip_base_url: str = Form(""),
    infobip_api_key: str = Form(""),
    infobip_sender: str = Form(""),
    infobip_username: str = Form(""),
    infobip_userkey: str = Form(""),
    db: Session = Depends(get_db),
):
    clinic, _lic = _get_singletons(db)
    clinic.infobip_base_url = (infobip_base_url or "").strip() or clinic.infobip_base_url
    clinic.infobip_api_key = (infobip_api_key or "").strip()
    clinic.infobip_sender = (infobip_sender or "").strip()
    # Optional extra fields (some clients use these)
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
    return RedirectResponse(url=f"{rp}/settings", status_code=303)


@router.post("/settings/license")
def settings_update_license(
    request: Request,
    product_mode: str = Form("BOTH"),
    action: str = Form("TRIAL"),
    weeks: int = Form(4),
    db: Session = Depends(get_db),
):
    _clinic, lic = _get_singletons(db)

    pm = (product_mode or "BOTH").upper().strip()
    if pm not in {"PORTAL", "SMS", "BOTH"}:
        pm = "BOTH"
    lic.product_mode = pm

    now = datetime.utcnow()
    act = (action or "TRIAL").upper().strip()

    if act == "TRIAL":
        lic.trial_end = now + timedelta(weeks=max(1, int(weeks)))
        # keep license_end untouched
    elif act == "RENEW_WEEKS":
        lic.license_end = now + timedelta(weeks=max(1, int(weeks)))
    elif act == "RENEW_YEAR":
        lic.license_end = now + timedelta(days=365)

    lic.updated_at = now
    db.add(lic)
    db.commit()
    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/settings", status_code=303)


@router.post("/settings/activate")
def settings_activate_license(
    request: Request,
    activation_code: str = Form(""),
    db: Session = Depends(get_db),
):
    """Activate using an offline signed code (Option A).

    Code is verified with Ed25519 public key (LICENSE_PUBLIC_KEY).
    """
    _clinic, lic = _get_singletons(db)
    code = (activation_code or "").strip()
    rp = _rp(request)
    if not code:
        return RedirectResponse(url=f"{rp}/settings?err=missing_code", status_code=303)

    try:
        payload = verify_activation_code(code, settings.LICENSE_PUBLIC_KEY)
    except Exception as e:
        return RedirectResponse(url=f"{rp}/settings?err=invalid_code", status_code=303)

    now = datetime.utcnow()
    # Apply
    lic.client_id = payload.client_id
    lic.activation_token = code
    lic.plan = int(payload.plan)
    lic.product_mode = payload.mode
    lic.activated_at = now
    lic.updated_at = now

    # Reset previous ends and apply new
    lic.trial_end = None
    lic.license_end = None
    # plan mapping: 1=1w trial, 2=1m trial, 3=1y license
    if payload.plan in (1, 2):
        lic.trial_end = payload.expires_at.replace(tzinfo=None)
    else:
        lic.license_end = payload.expires_at.replace(tzinfo=None)

    db.add(lic)
    db.commit()
    return RedirectResponse(url=f"{rp}/settings?ok=activated", status_code=303)


@router.get("/settings/env")
def download_env_for_client(db: Session = Depends(get_db)):
    """Download a .env file for the local installation.

    This is meant to help you configure the SMS provider credentials per client
    (so you can paste/save it as a .env in your packaged folder).
    """
    clinic, _lic = _get_singletons(db)
    lines = [
        "SMS_PROVIDER=infobip",
        f"INFOBIP_BASE_URL=\"{(clinic.infobip_base_url or '').strip()}\"",
        f"INFOBIP_API_KEY=\"{(clinic.infobip_api_key or '').strip()}\"",
        f"INFOBIP_FROM=\"{(clinic.infobip_sender or '').strip()}\"",
        f"INFOBIP_USERNAME=\"{(getattr(clinic,'infobip_username','') or '').strip()}\"",
        f"INFOBIP_USERKEY=\"{(getattr(clinic,'infobip_userkey','') or '').strip()}\"",
    ]
    content = "\n".join(lines) + "\n"
    return Response(
        content=content,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": "attachment; filename=client.env"},
    )


@router.get("/api/clinic_settings")
def api_clinic_settings(db: Session = Depends(get_db)):
    clinic, _lic = _get_singletons(db)
    return {
        "clinic_name": clinic.clinic_name,
        "address": clinic.address,
        "lat": clinic.lat,
        "lng": clinic.lng,
        "sms_provider": getattr(clinic, "sms_provider", "infobip"),
        "infobip_base_url": clinic.infobip_base_url,
        "infobip_sender": clinic.infobip_sender,
        # Never expose API key in the API response.
    }


@router.post("/api/clinic_settings")
async def api_update_clinic_settings(request: Request, db: Session = Depends(get_db)):
    """Update clinic settings via JSON (used by the landing page tabs).

    Body fields (optional): clinic_name, address, lat, lng
    """
    clinic, _lic = _get_singletons(db)
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    clinic.clinic_name = (payload.get("clinic_name") or clinic.clinic_name or "").strip()
    clinic.address = (payload.get("address") or clinic.address or "").strip()

    def _to_float(v):
        if v is None or v == "":
            return None
        try:
            return float(v)
        except Exception:
            return None

    lat = _to_float(payload.get("lat"))
    lng = _to_float(payload.get("lng"))
    if lat is not None:
        clinic.lat = lat
    if lng is not None:
        clinic.lng = lng

    clinic.updated_at = datetime.utcnow()
    db.add(clinic)
    db.commit()
    return {"ok": True}

@router.get("/api/infobip_settings")
def api_infobip_settings(db: Session = Depends(get_db)):
    clinic, _lic = _get_singletons(db)
    return {
        "sms_provider": getattr(clinic, "sms_provider", "infobip"),
        "infobip_base_url": clinic.infobip_base_url,
        "infobip_sender": clinic.infobip_sender,
        "infobip_username": getattr(clinic, "infobip_username", ""),
        "infobip_userkey": getattr(clinic, "infobip_userkey", ""),
        # Never expose API key in the API response.
    }


@router.post("/api/infobip_settings")
async def api_update_infobip_settings(request: Request, db: Session = Depends(get_db)):
    """Update Infobip settings via JSON (used by the landing page tabs).

    Body fields (optional): infobip_base_url, infobip_api_key, infobip_sender,
    infobip_username, infobip_userkey
    """
    clinic, _lic = _get_singletons(db)
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    if hasattr(clinic, "sms_provider"):
        clinic.sms_provider = "infobip"

    base_url = (payload.get("infobip_base_url") or "").strip()
    sender = (payload.get("infobip_sender") or "").strip()
    api_key = (payload.get("infobip_api_key") or "").strip()
    username = (payload.get("infobip_username") or "").strip()
    userkey = (payload.get("infobip_userkey") or "").strip()

    if base_url:
        clinic.infobip_base_url = base_url
    if sender:
        clinic.infobip_sender = sender
    # allow blank key to intentionally clear
    if api_key is not None:
        clinic.infobip_api_key = api_key

    if hasattr(clinic, "infobip_username") and username is not None:
        clinic.infobip_username = username
    if hasattr(clinic, "infobip_userkey") and userkey is not None:
        clinic.infobip_userkey = userkey

    clinic.updated_at = datetime.utcnow()
    db.add(clinic)
    db.commit()
    return {"ok": True}


@router.post("/api/license/manual")
async def api_update_license_manual(request: Request, db: Session = Depends(get_db)):
    """Update license settings manually via JSON (used by landing page tabs).

    Body: {product_mode, action, weeks}
    action in: TRIAL, RENEW_WEEKS, RENEW_YEAR
    """
    _clinic, lic = _get_singletons(db)
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    pm = (payload.get("product_mode") or lic.product_mode or "BOTH").upper().strip()
    if pm not in {"PORTAL", "SMS", "BOTH"}:
        pm = "BOTH"
    lic.product_mode = pm

    now = datetime.utcnow()
    act = (payload.get("action") or "TRIAL").upper().strip()
    weeks = payload.get("weeks", 4)
    try:
        weeks_i = max(1, int(weeks))
    except Exception:
        weeks_i = 4

    if act == "TRIAL":
        lic.trial_end = now + timedelta(weeks=weeks_i)
    elif act == "RENEW_WEEKS":
        lic.license_end = now + timedelta(weeks=weeks_i)
    elif act == "RENEW_YEAR":
        lic.license_end = now + timedelta(days=365)

    lic.updated_at = now
    db.add(lic)
    db.commit()
    return api_license(db)



@router.get("/api/license")
def api_license(db: Session = Depends(get_db)):
    _clinic, lic = _get_singletons(db)
    now = datetime.utcnow()
    trial_active = bool(lic.trial_end and lic.trial_end > now)
    license_active = bool(lic.license_end and lic.license_end > now)
    active = trial_active or license_active or (lic.trial_end is None and lic.license_end is None)
    end = lic.license_end or lic.trial_end
    days_left = None
    if end:
        try:
            days_left = max(0, int((end - now).total_seconds() // 86400))
        except Exception:
            days_left = None

    source = "activation" if (getattr(lic, "activation_token", "") or "").strip() else "manual"

    return {
        "product_mode": lic.product_mode,
        "client_id": getattr(lic, "client_id", "") or "",
        "source": source,
        "trial_end": lic.trial_end.isoformat() if lic.trial_end else None,
        "license_end": lic.license_end.isoformat() if lic.license_end else None,
        "days_left": days_left,
        "active": active,
    }


@router.post("/api/license/manual")
async def api_update_license_manual(request: Request, db: Session = Depends(get_db)):
    """Manual license/trial update via JSON (landing page tabs).

    Body:
      - product_mode: PORTAL|SMS|BOTH
      - action: start_trial | extend_trial | set_license_year
      - weeks: int (for trial actions)
    """
    _clinic, lic = _get_singletons(db)
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    mode = (payload.get('product_mode') or lic.product_mode or 'BOTH').strip().upper()
    if mode not in ('PORTAL','SMS','BOTH'):
        raise HTTPException(status_code=400, detail='invalid_mode')

    action = (payload.get('action') or '').strip().lower()
    weeks = payload.get('weeks')
    try:
        weeks_int = int(weeks) if weeks is not None and str(weeks) != '' else None
    except Exception:
        weeks_int = None

    now = datetime.utcnow()
    lic.product_mode = mode
    lic.updated_at = now

    if action in ('start_trial','extend_trial'):
        if not weeks_int or weeks_int <= 0 or weeks_int > 260:
            raise HTTPException(status_code=400, detail='invalid_weeks')
        base = lic.trial_end if (lic.trial_end and lic.trial_end > now) else now
        lic.trial_end = (base + timedelta(weeks=weeks_int)).replace(tzinfo=None)
        lic.license_end = None
        lic.activation_token = ''
        lic.client_id = lic.client_id or ''
    elif action in ('set_license_year','license_year','set_year'):
        lic.license_end = (now + timedelta(days=365)).replace(tzinfo=None)
        lic.trial_end = None
        lic.activation_token = ''
        lic.client_id = lic.client_id or ''
    elif action == '' or action == 'none':
        pass
    else:
        raise HTTPException(status_code=400, detail='invalid_action')

    db.add(lic)
    db.commit()
    return {"ok": True}


@router.post("/api/license/activate")
async def api_activate_license(request: Request, db: Session = Depends(get_db)):
    """Activate license via JSON (used by the landing page).

    Body: {"activation_code": "..."}
    """
    _clinic, lic = _get_singletons(db)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    code = (payload.get("activation_code") or payload.get("code") or "").strip()
    if not code:
        raise HTTPException(status_code=400, detail="missing_code")

    try:
        act = verify_activation_code(code, settings.LICENSE_PUBLIC_KEY)
    except Exception:
        raise HTTPException(status_code=400, detail="invalid_code")

    now = datetime.utcnow()
    # Apply
    lic.client_id = act.client_id
    lic.activation_token = code
    lic.plan = int(act.plan)
    lic.product_mode = act.mode
    lic.activated_at = now
    lic.updated_at = now

    # Reset previous ends and apply new
    lic.trial_end = None
    lic.license_end = None
    if act.plan in (1, 2):
        lic.trial_end = act.expires_at.replace(tzinfo=None)
    else:
        lic.license_end = act.expires_at.replace(tzinfo=None)

    db.add(lic)
    db.commit()

    # Return current license state
    return api_license(db)


@router.get("/api/internal/infobip")
def api_internal_infobip(request: Request, db: Session = Depends(get_db)):
    """Internal endpoint used by the SMS app to read Infobip creds.

    Protected by a shared token (SECRET_KEY) set in docker-compose env. This is
    not meant as strong security, only to avoid exposing the API key publicly.
    """
    token = request.headers.get("x-internal-token", "")
    if token != (settings.SECRET_KEY or ""):
        raise HTTPException(403, "Forbidden")
    clinic, _lic = _get_singletons(db)
    return {
        "sms_provider": getattr(clinic, "sms_provider", "infobip"),
        "infobip_base_url": clinic.infobip_base_url,
        "infobip_sender": clinic.infobip_sender,
        "infobip_api_key": clinic.infobip_api_key,
        "infobip_username": getattr(clinic, "infobip_username", ""),
        "infobip_userkey": getattr(clinic, "infobip_userkey", ""),
    }


# -----------------
# Billing inputs (recurring plans)
# -----------------
@router.get("/billing/inputs", response_class=HTMLResponse)
def billing_inputs(request: Request, db: Session = Depends(get_db)):
    children = db.query(Child).order_by(Child.full_name.asc()).all()
    plans = db.query(BillingPlan).order_by(BillingPlan.id.desc()).limit(200).all()
    return templates.TemplateResponse("pages/billing_inputs.html", {
        "request": request,
        "children": children,
        "plans": plans,
        "user_name": "PAUL PORTAL TEST",
    })

def _generate_billing_rows_for_plan(db: Session, plan: BillingPlan):
    """Generate BillingItem rows from a plan.
    For indefinitely plans, generate a rolling 12-month horizon for operational planning.
    """
    from datetime import date as _date
    from calendar import monthrange

    start = plan.start_date
    until = plan.until_date
    if plan.indefinitely and until is None:
        # 12 months horizon from start
        until = _date(start.year + (start.month + 11)//12, ((start.month + 11) % 12) + 1, start.day)

    # Safety limit
    max_rows = 200

    created = 0
    if plan.frequency == "weekly":
        step_weeks = int(plan.every_n_weeks or 1)
        d = start
        while until is None or d <= until:
            exists = db.query(BillingItem).filter(BillingItem.child_id == plan.child_id, BillingItem.billing_due == d).first()
            if not exists:
                db.add(BillingItem(child_id=plan.child_id, billing_due=d, paid="NO", invoice_created="NO", parent_signed_off="NO"))
                created += 1
                if created >= max_rows:
                    break
            d = d + timedelta(weeks=step_weeks)
            if until is not None and d > until:
                break

    else:  # monthly
        day = int(plan.day_of_month or start.day)
        y, m = start.year, start.month
        # iterate months
        while True:
            last_day = monthrange(y, m)[1]
            dday = min(day, last_day)
            d = _date(y, m, dday)

            # only create for months >= start
            if d >= start:
                if until is not None and d > until:
                    break
                exists = db.query(BillingItem).filter(BillingItem.child_id == plan.child_id, BillingItem.billing_due == d).first()
                if not exists:
                    db.add(BillingItem(child_id=plan.child_id, billing_due=d, paid="NO", invoice_created="NO", parent_signed_off="NO"))
                    created += 1
                    if created >= max_rows:
                        break

            # next month
            if m == 12:
                y += 1; m = 1
            else:
                m += 1

            if until is not None:
                # quick stop if next month start already beyond until by a lot
                if _date(y, m, 1) > until:
                    break
            if plan.indefinitely and created >= max_rows:
                break

@router.post("/api/infobip")
async def api_update_infobip(request: Request, db: Session = Depends(get_db)):
    """Update Infobip credentials via JSON (landing page tabs).

    Body fields: base_url, api_key, sender, username, userkey
    """
    clinic, _lic = _get_singletons(db)
    try:
        payload = await request.json()
    except Exception:
        payload = {}

    clinic.infobip_base_url = (payload.get("base_url") or payload.get("infobip_base_url") or clinic.infobip_base_url or "").strip()
    clinic.infobip_sender = (payload.get("sender") or payload.get("infobip_sender") or clinic.infobip_sender or "").strip()

    api_key = (payload.get("api_key") or payload.get("infobip_api_key") or "").strip()
    if api_key:
        clinic.infobip_api_key = api_key

    username = (payload.get("username") or payload.get("infobip_username") or "").strip()
    if hasattr(clinic, 'infobip_username') and username != "":
        clinic.infobip_username = username

    userkey = (payload.get("userkey") or payload.get("infobip_userkey") or "").strip()
    if hasattr(clinic, 'infobip_userkey') and userkey != "":
        clinic.infobip_userkey = userkey

    clinic.updated_at = datetime.utcnow()
    db.add(clinic)
    db.commit()
    return {"ok": True}


@router.post("/billing/inputs/create")
def billing_inputs_create(
    request: Request,
    child_id: int = Form(...),
    frequency: str = Form(...),
    start_date: str = Form(...),
    every_n_weeks: str = Form(""),
    day_of_month: str = Form(""),
    until_date: str = Form(""),
    indefinitely: str = Form("NO"),
    description: str = Form(""),
    db: Session = Depends(get_db),
):
    if not db.get(Child, child_id):
        raise HTTPException(404, "Child not found")

    freq = frequency.strip().lower()
    if freq not in {"weekly","monthly"}:
        raise HTTPException(400, "frequency must be weekly or monthly")

    sd = datetime.strptime(start_date, "%Y-%m-%d").date()
    ud = None
    if until_date.strip():
        ud = datetime.strptime(until_date, "%Y-%m-%d").date()

    indef = (indefinitely or "NO").strip().upper() == "YES"

    plan = BillingPlan(
        child_id=child_id,
        frequency=freq,
        every_n_weeks=int(every_n_weeks) if every_n_weeks.strip() else None,
        day_of_month=int(day_of_month) if day_of_month.strip() else None,
        start_date=sd,
        until_date=ud,
        indefinitely=indef,
        description=description.strip() or None,
    )
    db.add(plan)
    db.commit()

    # Generate billing rows
    _generate_billing_rows_for_plan(db, plan)
    db.commit()

    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/billing/inputs", status_code=303)



@router.get("/api/internal/clinic_settings")
def api_internal_clinic_settings(request: Request, db: Session = Depends(get_db)):
    """Internal endpoint used by the SMS service to fetch clinic settings.

    Security:
      - Requires header: X-Internal-Key matching settings.INTERNAL_API_KEY
      - Do NOT expose this key to end users.
    """
    key = request.headers.get("X-Internal-Key", "").strip()
    expected = (settings.INTERNAL_API_KEY or "").strip()
    if not expected or key != expected:
        raise HTTPException(status_code=403, detail="Forbidden")

    clinic, lic = _get_singletons(db)
    return {
        "clinic": {
            "name": clinic.name or "",
            "address": clinic.address or "",
            "map_url": getattr(clinic, "map_url", "") or "",
            "sms_provider": clinic.sms_provider or "infobip",
            "infobip_base_url": clinic.infobip_base_url or "",
            "infobip_api_key": clinic.infobip_api_key or "",
            "infobip_sender": clinic.infobip_sender or "",
            "infobip_username": getattr(clinic, "infobip_username", "") or "",
            "infobip_userkey": getattr(clinic, "infobip_userkey", "") or "",
        },
        "license": {
            "product_mode": getattr(lic, "product_mode", "") or "",
            "trial_end": (lic.trial_end.isoformat() if getattr(lic, "trial_end", None) else ""),
        },
    }

from __future__ import annotations

from datetime import datetime, date
import uuid
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


def _session_tenant_slug(request: Request) -> str:
    s = _session(request)
    return (s.get("tenant_slug") or "default").strip().lower()


def _sso_serializer() -> URLSafeTimedSerializer:
    secret = (settings.SSO_SHARED_SECRET or "").strip()
    if not secret:
        # This prevents insecure fallback behavior.
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

    sms_url = (settings.SMS_APP_URL or "").strip() or "/sms"
    if sms_url.endswith("/"):
        sms_url = sms_url[:-1]

    sso = _make_sms_sso_token(request, tctx.tenant_slug)

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
# Legacy compatibility routes (avoid 404 from old UI links)
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

    return _render(
        request,
        "pages/children_list.html",
        {"children": children, "q": q},
        db,
        tenant_slug=tctx.tenant_slug,
    )


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


# ----------------------------
# ✅ Tenant-safe Child Detail Page
# ----------------------------
@router.get("/children/{child_id}", response_class=HTMLResponse)
def child_detail(request: Request, child_id: int, db: Session = Depends(get_db)):
    tenant_slug = _session_tenant_slug(request)
    redirect = _require_login_for_tenant(request, tenant_slug)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant_slug)
    child = _child_or_404(db, tctx.tenant_id, child_id)

    # appointments
    appts = (
        db.query(Appointment)
        .filter(Appointment.tenant_id == tctx.tenant_id, Appointment.child_id == child_id)
        .order_by(Appointment.starts_at.desc())
        .limit(200)
        .all()
    )

    # billing
    bills = (
        db.query(BillingItem)
        .filter(BillingItem.tenant_id == tctx.tenant_id, BillingItem.child_id == child_id)
        .order_by(BillingItem.billing_due.desc())
        .limit(200)
        .all()
    )

    # notes are per appointment (1:1). Collect them keyed by appointment_id
    appt_ids = [a.id for a in appts]
    notes_by_appt: dict[int, SessionNote] = {}
    if appt_ids:
        notes = (
            db.query(SessionNote)
            .filter(SessionNote.tenant_id == tctx.tenant_id, SessionNote.appointment_id.in_(appt_ids))
            .all()
        )
        notes_by_appt = {n.appointment_id: n for n in notes}

    # parents best-effort (won't crash if fields not present)
    parents = []
    for label, name_attr, phone_attr in [
        ("Parent 1", "parent_name", "parent_phone"),
        ("Parent 2", "parent2_name", "parent2_phone"),
        ("Mother", "mother_name", "mother_phone"),
        ("Father", "father_name", "father_phone"),
    ]:
        n = (getattr(child, name_attr, None) or "").strip() if hasattr(child, name_attr) else ""
        p = (getattr(child, phone_attr, None) or "").strip() if hasattr(child, phone_attr) else ""
        if n or p:
            parents.append((label, n, p))

    rp = _rp(request)
    child_name = getattr(child, "full_name", getattr(child, "name", f"Child #{child_id}"))
    child_notes = getattr(child, "notes", "") or ""

    # build appointment rows + note preview
    appt_rows = ""
    appt_options = ""
    for a in appts:
        note = notes_by_appt.get(a.id)
        note_badge = "No note"
        if note and (note.summary or note.next_steps or note.improvements or note.what_went_wrong):
            note_badge = "Has note"
        appt_rows += (
            "<tr>"
            f"<td style='padding:8px'>{_fmt_dt(a.starts_at)}</td>"
            f"<td style='padding:8px'>{_fmt_dt(a.ends_at)}</td>"
            f"<td style='padding:8px'>{a.therapist_name}</td>"
            f"<td style='padding:8px'>{a.procedure}</td>"
            f"<td style='padding:8px'>{a.attendance_status}</td>"
            f"<td style='padding:8px;opacity:.85'>{note_badge}</td>"
            "</tr>"
        )
        appt_options += f"<option value='{a.id}'>#{a.id} — {_fmt_dt(a.starts_at)} — {a.procedure}</option>"

    bill_rows = ""
    for b in bills:
        bill_rows += (
            "<tr>"
            f"<td style='padding:8px'>{b.billing_due.isoformat()}</td>"
            f"<td style='padding:8px'>{b.paid}</td>"
            f"<td style='padding:8px'>{b.invoice_created}</td>"
            f"<td style='padding:8px'>{b.parent_signed_off}</td>"
            "<td style='padding:8px;white-space:nowrap'>"
            f"<form method='post' action='{rp}/children/{child_id}/billing/{b.id}/set_flag' style='display:inline;'>"
            "<input type='hidden' name='flag' value='paid'/>"
            "<input type='hidden' name='value' value='YES'/>"
            "<button type='submit'>Paid</button>"
            "</form> "
            f"<form method='post' action='{rp}/children/{child_id}/billing/{b.id}/set_flag' style='display:inline;'>"
            "<input type='hidden' name='flag' value='paid'/>"
            "<input type='hidden' name='value' value='NO'/>"
            "<button type='submit'>Unpaid</button>"
            "</form> "
            f"<form method='post' action='{rp}/children/{child_id}/billing/{b.id}/set_flag' style='display:inline;'>"
            "<input type='hidden' name='flag' value='invoice_created'/>"
            "<input type='hidden' name='value' value='YES'/>"
            "<button type='submit'>Invoice ✔</button>"
            "</form> "
            f"<form method='post' action='{rp}/children/{child_id}/billing/{b.id}/set_flag' style='display:inline;'>"
            "<input type='hidden' name='flag' value='parent_signed_off'/>"
            "<input type='hidden' name='value' value='YES'/>"
            "<button type='submit'>Signed ✔</button>"
            "</form>"
            "</td>"
            "</tr>"
        )

    parents_html = ""
    if parents:
        rows = "".join(
            f"<tr><td style='padding:8px;opacity:.85'>{label}</td><td style='padding:8px'>{n}</td><td style='padding:8px'>{p}</td></tr>"
            for (label, n, p) in parents
        )
        parents_html = f"""
          <div class="card">
            <h3>Parents</h3>
            <table class="tbl">
              <thead><tr><th>Type</th><th>Name</th><th>Phone</th></tr></thead>
              <tbody>{rows}</tbody>
            </table>
          </div>
        """
    else:
        parents_html = """
          <div class="card">
            <h3>Parents</h3>
            <div class="muted">No parent details found on this child record.</div>
          </div>
        """

    html = f"""
    <!doctype html>
    <html>
      <head>
        <meta charset="utf-8"/>
        <meta name="viewport" content="width=device-width, initial-scale=1"/>
        <title>{child_name}</title>
        <style>
          body {{
            font-family: system-ui, -apple-system, Segoe UI, Roboto, Arial;
            margin: 0; padding: 18px;
            background: #0b1220; color: #e5e7eb;
          }}
          a {{ color: #60a5fa; text-decoration: none; }}
          .top {{
            display:flex; gap:10px; align-items:center; justify-content:space-between;
            max-width: 1100px; margin: 0 auto 12px auto;
          }}
          .wrap {{ max-width: 1100px; margin: 0 auto; display:grid; gap: 12px; }}
          .card {{
            background: rgba(255,255,255,.06);
            border: 1px solid rgba(255,255,255,.10);
            border-radius: 14px;
            padding: 14px;
          }}
          .muted {{ color: rgba(229,231,235,.75); }}
          .btn {{
            display:inline-block; padding: 9px 12px;
            border-radius: 10px;
            background: rgba(96,165,250,.18);
            border: 1px solid rgba(96,165,250,.35);
          }}
          .grid2 {{ display:grid; grid-template-columns: 1fr 1fr; gap: 12px; }}
          @media (max-width: 900px) {{
            .grid2 {{ grid-template-columns: 1fr; }}
            .top {{ flex-direction:column; align-items:flex-start; }}
          }}
          .tbl {{ width:100%; border-collapse: collapse; margin-top: 8px; }}
          .tbl th {{
            text-align:left; padding:8px;
            border-bottom: 1px solid rgba(255,255,255,.14);
            color: rgba(229,231,235,.85);
            font-weight: 700;
          }}
          .tbl td {{
            border-bottom: 1px solid rgba(255,255,255,.08);
          }}
          input, select, textarea {{
            width:100%;
            background:#0b1220; color:#e5e7eb;
            border: 1px solid rgba(255,255,255,.18);
            border-radius: 10px;
            padding: 10px;
            margin-top: 8px;
          }}
          textarea {{ min-height: 90px; }}
          button {{
            margin-top: 10px;
            padding: 10px 12px;
            border-radius: 10px;
            border: 1px solid rgba(96,165,250,.35);
            background: rgba(96,165,250,.18);
            color: #e5e7eb;
            cursor: pointer;
          }}
        </style>
      </head>
      <body>
        <div class="top">
          <div>
            <div class="muted">Tenant: <b>{tctx.tenant_slug}</b></div>
            <h2 style="margin:6px 0 0 0;">{child_name}</h2>
          </div>
          <div style="display:flex; gap:10px; flex-wrap:wrap;">
            <a class="btn" href="{rp}/t/{tctx.tenant_slug}/suite">Back to Suite</a>
            <a class="btn" href="{rp}/children?tenant={tctx.tenant_slug}">Back to Children</a>
          </div>
        </div>

        <div class="wrap">
          <div class="card">
            <h3>Child Details</h3>
            <div class="muted">ID: {child_id}</div>
            <div style="margin-top:10px;">
              <div class="muted" style="margin-bottom:6px;">Child notes</div>
              <div style="white-space:pre-wrap;">{child_notes if child_notes else "<span class='muted'>No notes.</span>"}</div>
            </div>
          </div>

          <div class="grid2">
            {parents_html}

            <div class="card">
              <h3>Add Appointment</h3>
              <form method="post" action="{rp}/children/{child_id}/appointments/create">
                <input name="starts_at" type="datetime-local" required />
                <input name="ends_at" type="datetime-local" required />
                <input name="therapist_name" placeholder="Therapist name" />
                <input name="procedure" placeholder="Procedure (e.g. Speech Therapy)" />
                <select name="attendance_status">
                  <option value="UNCONFIRMED">UNCONFIRMED</option>
                  <option value="ATTENDED">ATTENDED</option>
                  <option value="MISSED">MISSED</option>
                  <option value="CANCELLED">CANCELLED</option>
                </select>
                <button type="submit">Create Appointment</button>
              </form>
            </div>
          </div>

          <div class="grid2">
            <div class="card">
              <h3>Appointments</h3>
              {"<div class='muted'>No appointments found.</div>" if not appt_rows else ""}
              {"<table class='tbl'><thead><tr><th>Start</th><th>End</th><th>Therapist</th><th>Procedure</th><th>Status</th><th>Note</th></tr></thead><tbody>"+appt_rows+"</tbody></table>" if appt_rows else ""}
            </div>

            <div class="card">
              <h3>Session Note</h3>
              <div class="muted">Notes are stored per appointment (1 note per appointment).</div>
              <form method="post" action="{rp}/children/{child_id}/appointments/0/note" onsubmit="this.action=this.action.replace('/appointments/0/note','/appointments/'+document.getElementById('appt_sel').value+'/note');">
                <select id="appt_sel" name="appointment_id" required>
                  <option value="">Select appointment…</option>
                  {appt_options}
                </select>
                <textarea name="summary" placeholder="Summary"></textarea>
                <textarea name="what_went_wrong" placeholder="What went wrong"></textarea>
                <textarea name="improvements" placeholder="Improvements"></textarea>
                <textarea name="next_steps" placeholder="Next steps"></textarea>
                <button type="submit">Save Note</button>
              </form>
            </div>
          </div>

          <div class="grid2">
            <div class="card">
              <h3>Billing</h3>
              {"<div class='muted'>No billing entries found.</div>" if not bill_rows else ""}
              {"<table class='tbl'><thead><tr><th>Due</th><th>Paid</th><th>Invoice</th><th>Signed</th><th>Actions</th></tr></thead><tbody>"+bill_rows+"</tbody></table>" if bill_rows else ""}
            </div>

            <div class="card">
              <h3>Create Invoice</h3>
              <form method="post" action="{rp}/children/{child_id}/billing/create">
                <input name="billing_due" type="date" required />
                <select name="invoice_created">
                  <option value="NO">Invoice created? NO</option>
                  <option value="YES">Invoice created? YES</option>
                </select>
                <select name="paid">
                  <option value="NO">Paid? NO</option>
                  <option value="YES">Paid? YES</option>
                </select>
                <select name="parent_signed_off">
                  <option value="NO">Parent signed? NO</option>
                  <option value="YES">Parent signed? YES</option>
                </select>
                <button type="submit">Create Billing Item</button>
              </form>
            </div>
          </div>
        </div>
      </body>
    </html>
    """
    return HTMLResponse(html)


# ----------------------------
# POST: Create appointment (tenant-safe)
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
        rp = _rp(request)
        return RedirectResponse(url=f"{rp}/children/{child_id}", status_code=303)

    a = Appointment(
        tenant_id=tctx.tenant_id,
        child_id=child_id,
        starts_at=sdt,
        ends_at=edt,
        therapist_name=(therapist_name or "").strip(),
        procedure=(procedure or "Session").strip() or "Session",
        attendance_status=(attendance_status or "UNCONFIRMED").strip() or "UNCONFIRMED",
    )
    db.add(a)
    db.commit()

    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/children/{child_id}", status_code=303)


# ----------------------------
# POST: Create billing item (tenant-safe)
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
        rp = _rp(request)
        return RedirectResponse(url=f"{rp}/children/{child_id}", status_code=303)

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

    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/children/{child_id}", status_code=303)


# ----------------------------
# POST: Set billing flags (paid / invoice_created / parent_signed_off)
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

    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/children/{child_id}", status_code=303)


# ----------------------------
# POST: Upsert session note for appointment (tenant-safe)
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

    rp = _rp(request)
    return RedirectResponse(url=f"{rp}/children/{child_id}", status_code=303)


# ----------------------------
# Therapists
# ----------------------------
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


# ----------------------------
# Internal endpoints for SMS service (tenant-aware via ?tenant=slug)
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

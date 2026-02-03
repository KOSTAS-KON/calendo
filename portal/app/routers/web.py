from __future__ import annotations

from datetime import datetime
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
from app.models.clinic_settings import ClinicSettings, AppLicense
from app.models.sms_outbox import SmsOutbox

# Optional models (exist in many versions of your repo)
try:
    # common naming
    from app.models.billing_item import BillingItem  # type: ignore
except Exception:
    BillingItem = None  # type: ignore

try:
    from app.models.session_note import SessionNote  # type: ignore
except Exception:
    SessionNote = None  # type: ignore


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
    Returns RedirectResponse if denied, else None.
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
    """Internal calls from SMS app."""
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

    sso = _make_sms_sso_token(request, tctx.tenant_slug)

    # Render deployments often mount Streamlit at /sms/ with a route under /sms
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
# Legacy / compatibility routes (avoid 404 from old UI links)
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

    # If your template already links to /children/{id}, it will now work (real page exists below).
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
    """
    Tenant-safe detail page:
      - Uses the logged-in tenant from session
      - Loads child only if Child.tenant_id matches tenant_id
      - Prevents cross-tenant data leakage
    """
    tenant_slug = _session_tenant_slug(request)
    redirect = _require_login_for_tenant(request, tenant_slug)
    if redirect:
        return redirect

    tctx = resolve_tenant(db, request, tenant_slug=tenant_slug)

    child = (
        db.query(Child)
        .filter(Child.tenant_id == tctx.tenant_id, Child.id == child_id)
        .first()
    )
    if not child:
        # Tenant-safe: do NOT reveal whether the ID exists in other tenants
        raise HTTPException(status_code=404, detail="Not Found")

    # Parents (best-effort based on your actual Child model fields)
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

    # Appointments (best-effort fields)
    appts = []
    try:
        aq = db.query(Appointment).filter(
            Appointment.tenant_id == tctx.tenant_id,
            getattr(Appointment, "child_id") == child_id,  # type: ignore[attr-defined]
        )
        # order by a reasonable datetime field if present
        if hasattr(Appointment, "starts_at"):
            aq = aq.order_by(getattr(Appointment, "starts_at").desc())
        elif hasattr(Appointment, "start_at"):
            aq = aq.order_by(getattr(Appointment, "start_at").desc())
        appts = aq.limit(200).all()
    except Exception:
        appts = []

    # Billing items (optional model)
    bills = []
    if BillingItem is not None:
        try:
            bq = db.query(BillingItem).filter(
                getattr(BillingItem, "tenant_id") == tctx.tenant_id,  # type: ignore[attr-defined]
                getattr(BillingItem, "child_id") == child_id,  # type: ignore[attr-defined]
            )
            # order by a reasonable date field if present
            if hasattr(BillingItem, "due_date"):
                bq = bq.order_by(getattr(BillingItem, "due_date").desc())
            elif hasattr(BillingItem, "created_at"):
                bq = bq.order_by(getattr(BillingItem, "created_at").desc())
            bills = bq.limit(200).all()
        except Exception:
            bills = []

    # Notes (optional model)
    notes = []
    if SessionNote is not None:
        try:
            nq = db.query(SessionNote).filter(
                getattr(SessionNote, "tenant_id") == tctx.tenant_id,  # type: ignore[attr-defined]
                getattr(SessionNote, "child_id") == child_id,  # type: ignore[attr-defined]
            )
            if hasattr(SessionNote, "created_at"):
                nq = nq.order_by(getattr(SessionNote, "created_at").desc())
            notes = nq.limit(200).all()
        except Exception:
            notes = []

    rp = _rp(request)

    # Render an inline page (no new template files needed)
    def _safe(v) -> str:
        return "" if v is None else str(v)

    # Child fields (best-effort)
    child_name = _safe(getattr(child, "full_name", getattr(child, "name", f"Child #{child_id}")))
    child_notes = _safe(getattr(child, "notes", ""))

    # Build HTML cards
    parents_html = ""
    if parents:
        rows = "".join(
            f"<tr><td style='padding:8px;opacity:.85'>{label}</td><td style='padding:8px'>{_safe(n)}</td><td style='padding:8px'>{_safe(p)}</td></tr>"
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

    appts_rows = ""
    for a in appts:
        status = _safe(getattr(a, "status", getattr(a, "state", "")))
        starts = _fmt_dt(getattr(a, "starts_at", getattr(a, "start_at", getattr(a, "start_time", None))))
        ends = _fmt_dt(getattr(a, "ends_at", getattr(a, "end_at", getattr(a, "end_time", None))))
        title = _safe(getattr(a, "title", getattr(a, "service", "")))
        appts_rows += f"<tr><td style='padding:8px'>{starts}</td><td style='padding:8px'>{ends}</td><td style='padding:8px'>{title}</td><td style='padding:8px'>{status}</td></tr>"

    appts_html = f"""
      <div class="card">
        <h3>Appointments</h3>
        {"<div class='muted'>No appointments found.</div>" if not appts_rows else ""}
        {"<table class='tbl'><thead><tr><th>Start</th><th>End</th><th>Title</th><th>Status</th></tr></thead><tbody>"+appts_rows+"</tbody></table>" if appts_rows else ""}
      </div>
    """

    bills_rows = ""
    for b in bills:
        due = _fmt_dt(getattr(b, "due_date", getattr(b, "date", getattr(b, "created_at", None))))
        amount = _safe(getattr(b, "amount", getattr(b, "total", "")))
        paid = _safe(getattr(b, "paid", getattr(b, "is_paid", "")))
        desc = _safe(getattr(b, "description", getattr(b, "title", getattr(b, "label", ""))))
        bills_rows += f"<tr><td style='padding:8px'>{due}</td><td style='padding:8px'>{desc}</td><td style='padding:8px'>{amount}</td><td style='padding:8px'>{paid}</td></tr>"

    bills_html = f"""
      <div class="card">
        <h3>Billing</h3>
        {"<div class='muted'>Billing model not enabled in this build.</div>" if BillingItem is None else ""}
        {"" if BillingItem is None else ("<div class='muted'>No billing entries found.</div>" if not bills_rows else "")}
        {"" if (BillingItem is None or not bills_rows) else "<table class='tbl'><thead><tr><th>Date/Due</th><th>Description</th><th>Amount</th><th>Paid</th></tr></thead><tbody>"+bills_rows+"</tbody></table>"}
      </div>
    """

    notes_rows = ""
    for n in notes:
        created = _fmt_dt(getattr(n, "created_at", getattr(n, "date", None)))
        body = _safe(getattr(n, "note", getattr(n, "text", getattr(n, "content", ""))))
        # keep short preview
        preview = (body[:180] + "…") if len(body) > 180 else body
        notes_rows += f"<tr><td style='padding:8px;white-space:nowrap'>{created}</td><td style='padding:8px'>{preview}</td></tr>"

    notes_html = f"""
      <div class="card">
        <h3>Notes</h3>
        {"<div class='muted'>Notes model not enabled in this build.</div>" if SessionNote is None else ""}
        {"" if SessionNote is None else ("<div class='muted'>No notes found.</div>" if not notes_rows else "")}
        {"" if (SessionNote is None or not notes_rows) else "<table class='tbl'><thead><tr><th>Created</th><th>Note</th></tr></thead><tbody>"+notes_rows+"</tbody></table>"}
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
          textarea {{
            width:100%; min-height: 90px;
            background:#0b1220; color:#e5e7eb;
            border: 1px solid rgba(255,255,255,.18);
            border-radius: 10px;
            padding: 10px;
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
              <div class="muted" style="margin-bottom:6px;">Notes</div>
              <div style="white-space:pre-wrap;">{child_notes if child_notes else "<span class='muted'>No notes.</span>"}</div>
            </div>
          </div>

          <div class="grid2">
            {parents_html}
            {notes_html}
          </div>

          <div class="grid2">
            {appts_html}
            {bills_html}
          </div>
        </div>
      </body>
    </html>
    """
    return HTMLResponse(html)


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

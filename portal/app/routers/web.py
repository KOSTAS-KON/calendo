from __future__ import annotations

"""Main UI + internal JSON endpoints.

This file intentionally keeps the "Clinic Suite" web UX routes together
and also exposes a small internal API used by the SMS Calendar service.

The upstream repo had merge-conflict markers and missing Calendar endpoints.
This rewrite restores a clean, working router and adds:
  - /calendar/add_appointment
  - /calendar/add_billing
  - /calendar/add_journey
  - /api/calendar_events
and patches BillingItem storage to persist amount + description.
"""

import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote

import sqlalchemy as sa
import bcrypt
from fastapi import APIRouter, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.db import SessionLocal
from app.models import (
    Appointment,
    Attachment,
    BillingItem,
    BillingPlan,
    Child,
    ClinicSettings,
    SessionNote,
    SmsOutbox,
    Therapist,
    TimelineEvent,
    User,
    ChildTherapistAssignment,
)
from app.models.clinic_settings import AppLicense
from app.models.licensing import Subscription
from app.models.tenant import Tenant
from app.services.license_tokens import verify_activation_code
from app.services.storage import delete_file, save_upload
from app.utils.paths import TEMPLATES_DIR
from app.utils.security import generate_temp_password


router = APIRouter()
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# -----------------------------
# Template helpers / globals
# -----------------------------


def _pill(label: str, color: str) -> str:
    return (
        f"<span class='pill' style='background:{color};color:white;"
        f"padding:6px 10px;border-radius:999px;font-weight:900;font-size:12px;'>{label}</span>"
    )


def status_badge(status: str | None) -> str:
    s = (status or "UNCONFIRMED").upper()
    if s == "ATTENDED":
        return _pill("✅", "#16a34a")
    if s == "MISSED":
        return _pill("⚠", "#dc2626")
    if s == "CONFIRMED":
        return _pill("✓", "#2563eb")
    if s == "CANCELLED_PROVIDER":
        return _pill("✕", "#f59e0b")
    if s == "CANCELLED_ME":
        return _pill("✕", "#f97316")
    return _pill("…", "#6b7280")


def status_chip(status: str | None) -> str:
    s = (status or "UNCONFIRMED").upper()
    if s == "ATTENDED":
        return _pill("ATTENDED", "#16a34a")
    if s == "MISSED":
        return _pill("MISSED", "#dc2626")
    if s == "CONFIRMED":
        return _pill("CONFIRMED", "#2563eb")
    if s == "CANCELLED_PROVIDER":
        return _pill("CANCELLED (PROVIDER)", "#f59e0b")
    if s == "CANCELLED_ME":
        return _pill("CANCELLED (ME)", "#f97316")
    return _pill("UNCONFIRMED", "#6b7280")


templates.env.globals["status_badge"] = status_badge
templates.env.globals["status_chip"] = status_chip


# -----------------------------
# DB dependency
# -----------------------------


def _db() -> Session:
    return SessionLocal()


# -----------------------------
# Common helpers
# -----------------------------


def _rp(request: Request) -> str:
    return str(request.scope.get("root_path") or "")


def _full_path(request: Request) -> str:
    # Include query string for next=... redirects
    path = request.url.path
    if request.url.query:
        path += "?" + request.url.query
    return path


def _toast(request: Request, text: str, kind: str = "success") -> None:
    request.session["toast"] = {"text": text, "kind": kind}


def _require_login(request: Request) -> Optional[RedirectResponse]:
    s = request.session or {}
    if not s.get("user_id"):
        next_path = _full_path(request)
        return RedirectResponse(url=f"{_rp(request)}/login?next={quote(next_path)}", status_code=303)
    return None


def _session_tenant_slug(request: Request) -> str:
    s = request.session or {}
    return str(s.get("tenant_slug") or "default").strip().lower() or "default"


def _session_tenant_id(request: Request) -> str | None:
    s = request.session or {}
    v = s.get("tenant_id")
    return str(v) if v else None


def _resolve_tenant_or_404(db: Session, request: Request, requested_slug: str | None = None) -> tuple[str, str]:
    """Return (tenant_slug, tenant_id) for UI routes.

    For logged-in UI pages we do not allow switching tenant via query params.
    The session tenant wins.
    """
    tenant_slug = _session_tenant_slug(request)
    if requested_slug and requested_slug.strip().lower() != tenant_slug:
        # Normalize navigation: keep session tenant.
        tenant_slug = tenant_slug

    t = db.query(Tenant).filter(Tenant.slug == tenant_slug).first()
    if not t:
        raise HTTPException(status_code=404, detail="Tenant not found")
    if (t.status or "active") != "active":
        raise HTTPException(status_code=403, detail="Tenant suspended")

    # Keep session consistent.
    request.session["tenant_slug"] = tenant_slug
    request.session["tenant_id"] = t.id
    return tenant_slug, t.id



ROLE_CLINIC_SUPERUSER = {"clinic_superuser", "owner", "admin", "superuser"}
ROLE_CALENDAR_STAFF = {"calendar_staff", "receptionist", "secretary", "staff"}
ROLE_THERAPIST = {"therapist"}

def _session_role(request: Request) -> str:
    s = request.session or {}
    return str(s.get("role") or "").strip().lower()

def _role_flags(request: Request) -> dict:
    role = _session_role(request)
    is_superuser = role in ROLE_CLINIC_SUPERUSER
    is_calendar_staff = (role in ROLE_CALENDAR_STAFF) and not is_superuser
    is_therapist = role in ROLE_THERAPIST
    return {
        "current_role": role or "calendar_staff",
        "is_clinic_superuser": is_superuser,
        "is_calendar_staff": is_calendar_staff,
        "is_therapist": is_therapist,
        "can_manage_team": is_superuser,
        "can_access_children": is_superuser or is_therapist,
        "can_access_therapists": is_superuser,
        "can_access_billing": is_superuser,
        "can_access_settings": is_superuser,
        "can_access_sms_outbox": is_superuser or is_calendar_staff,
        "can_access_calendar": is_superuser or is_calendar_staff or is_therapist,
    }

def _redirect_suite(request: Request, message: str | None = None) -> RedirectResponse:
    if message:
        _toast(request, message, "danger")
    slug = _session_tenant_slug(request)
    return RedirectResponse(url=f"{_rp(request)}/t/{slug}/suite", status_code=303)

def _require_superuser_role(request: Request) -> Optional[RedirectResponse]:
    if not _role_flags(request)["is_clinic_superuser"]:
        return _redirect_suite(request, "Access restricted to clinic superusers.")
    return None

def _require_calendar_role(request: Request) -> Optional[RedirectResponse]:
    if not _role_flags(request)["can_access_calendar"]:
        return _redirect_suite(request, "Access restricted to calendar roles.")
    return None

def _current_user_row(db: Session, request: Request, tid: str) -> Optional[User]:
    uid = request.session.get("user_id")
    if uid:
        u = db.query(User).filter(User.tenant_id == tid, User.id == str(uid)).first()
        if u:
            return u
    email = str(request.session.get("email") or "").strip().lower()
    if email:
        return db.query(User).filter(User.tenant_id == tid, sa.func.lower(User.email) == email).first()
    return None

def _ensure_assignment_table(db: Session) -> None:
    bind = db.get_bind()
    try:
        ChildTherapistAssignment.__table__.create(bind, checkfirst=True)
    except Exception:
        pass

def _therapist_for_current_user(db: Session, request: Request, tid: str) -> Optional[Therapist]:
    _ensure_assignment_table(db)
    u = _current_user_row(db, request, tid)
    if not u:
        return None
    # first try explicit link if column exists in db/model
    q = db.query(Therapist).filter(Therapist.tenant_id == tid)
    if hasattr(Therapist, "user_id"):
        t = q.filter(Therapist.user_id == u.id).first()
        if t:
            return t
    email = (u.email or "").strip().lower()
    if email:
        return db.query(Therapist).filter(Therapist.tenant_id == tid, sa.func.lower(Therapist.email) == email).first()
    return None

def _assigned_child_ids_for_request(db: Session, request: Request, tid: str) -> list[int]:
    rolef = _role_flags(request)
    if rolef["is_clinic_superuser"] or rolef["is_calendar_staff"]:
        return []
    if not rolef["is_therapist"]:
        return []
    t = _therapist_for_current_user(db, request, tid)
    if not t:
        return []
    rows = (
        db.query(ChildTherapistAssignment.child_id)
        .filter(
            ChildTherapistAssignment.tenant_id == tid,
            ChildTherapistAssignment.therapist_id == t.id,
            ChildTherapistAssignment.is_active.is_(True),
        )
        .all()
    )
    return [int(r[0]) for r in rows]

def _assert_child_access(db: Session, request: Request, tid: str, child_id: int) -> Optional[RedirectResponse]:
    rolef = _role_flags(request)
    if rolef["is_clinic_superuser"]:
        return None
    if rolef["is_calendar_staff"]:
        return _redirect_suite(request, "Calendar staff do not have access to child records.")
    if rolef["is_therapist"]:
        allowed = set(_assigned_child_ids_for_request(db, request, tid))
        if child_id not in allowed:
            return _redirect_suite(request, "You only have access to children assigned to you.")
        return None
    return _redirect_suite(request, "Access denied.")

def _ensure_billing_item_columns(db: Session) -> None:
    """Best-effort schema drift fixer for billing tables.

    A few deployments may have been created before the billing feature (or before
    the latest migrations landed). In that situation, the `/billing` pages can
    crash due to missing columns (or older boolean flag types).

    This helper keeps the app resilient by:
      • creating the billing tables if missing (checkfirst)
      • adding missing columns (billing_due, amount/currency/description, flags)
      • attempting to normalize old boolean flags to YES/NO strings

    Notes:
      • This is a *safety net*. The preferred path is still running Alembic migrations.
      • All operations are best-effort; failures are swallowed to avoid taking the
        whole portal down.
    """
    bind = db.get_bind()

    # If the tables do not exist (fresh DB / older install), create them from ORM metadata.
    try:
        BillingPlan.__table__.create(bind, checkfirst=True)
        BillingItem.__table__.create(bind, checkfirst=True)
    except Exception:
        pass

    try:
        insp = sa.inspect(bind)
        col_info = insp.get_columns("billing_items")
    except Exception:
        return

    cols = {c.get("name"): c for c in col_info if c.get("name")}

    stmts: list[str] = []

    # --- billing_due (older schema used "month")
    if "billing_due" not in cols:
        stmts.append("ALTER TABLE billing_items ADD COLUMN billing_due DATE")

        # Backfill if possible
        if "month" in cols:
            if bind.dialect.name == "postgresql":
                stmts.append(
                    "UPDATE billing_items "
                    "SET billing_due = (to_date(month || '-01','YYYY-MM-DD') + INTERVAL '1 month - 1 day')::date "
                    "WHERE billing_due IS NULL"
                )
            else:
                # SQLite (typeless) - best effort
                stmts.append(
                    "UPDATE billing_items "
                    "SET billing_due = date(month || '-01','start of month','+1 month','-1 day') "
                    "WHERE billing_due IS NULL"
                )
        else:
            if bind.dialect.name == "postgresql":
                stmts.append("UPDATE billing_items SET billing_due = CURRENT_DATE WHERE billing_due IS NULL")
            else:
                stmts.append("UPDATE billing_items SET billing_due = date('now') WHERE billing_due IS NULL")

    # --- tenant_id (older schema may not have it)
    if "tenant_id" not in cols:
        stmts.append("ALTER TABLE billing_items ADD COLUMN tenant_id VARCHAR(36) NULL")
        if bind.dialect.name == "postgresql":
            stmts.append("UPDATE billing_items bi SET tenant_id = c.tenant_id FROM children c WHERE bi.child_id = c.id AND (bi.tenant_id IS NULL OR bi.tenant_id = '')")
            stmts.append("CREATE INDEX IF NOT EXISTS ix_billing_items_tenant_id ON billing_items (tenant_id)")
        else:
            stmts.append("UPDATE billing_items SET tenant_id = (SELECT tenant_id FROM children WHERE children.id = billing_items.child_id) WHERE tenant_id IS NULL OR tenant_id = ''")

    # --- amount/currency/description
    if "amount_cents" not in cols:
        stmts.append("ALTER TABLE billing_items ADD COLUMN amount_cents INTEGER NULL")
    if "currency" not in cols:
        stmts.append("ALTER TABLE billing_items ADD COLUMN currency VARCHAR(8) NULL")
    if "description" not in cols:
        stmts.append("ALTER TABLE billing_items ADD COLUMN description TEXT NULL")

    # Default currency backfill
    if "currency" in cols or "currency" not in cols:
        stmts.append("UPDATE billing_items SET currency = 'EUR' WHERE currency IS NULL OR currency = ''")

    # --- flags: paid / invoice_created / parent_signed_off
    for flag in ("paid", "invoice_created", "parent_signed_off"):
        if flag not in cols:
            stmts.append(f"ALTER TABLE billing_items ADD COLUMN {flag} VARCHAR(3) NULL")
            stmts.append(f"UPDATE billing_items SET {flag} = 'NO' WHERE {flag} IS NULL OR {flag} = ''")
            continue

        ctype = str(cols[flag].get("type", "")).lower()
        # Older schema may have BOOLEAN or INTEGER 0/1. Normalize to YES/NO.
        if ("bool" in ctype) or ("boolean" in ctype):
            if bind.dialect.name == "postgresql":
                stmts.append(
                    f"ALTER TABLE billing_items ALTER COLUMN {flag} TYPE VARCHAR(3) "
                    f"USING CASE WHEN {flag} THEN 'YES' ELSE 'NO' END"
                )
            else:
                # SQLite: can't ALTER TYPE; but it's typeless and will happily store text.
                stmts.append(
                    f"UPDATE billing_items SET {flag} = CASE "
                    f"WHEN {flag} IN (1,'1','t','true','TRUE') THEN 'YES' ELSE 'NO' END"
                )
        else:
            # Ensure canonical YES/NO values
            stmts.append(
                f"UPDATE billing_items SET {flag} = "
                f"CASE WHEN upper(CAST({flag} AS TEXT)) = 'YES' THEN 'YES' ELSE 'NO' END "
                f"WHERE {flag} IS NOT NULL"
            )

    # Execute statements one-by-one so a single failure doesn't break everything.
    if stmts:
        for sql in stmts:
            try:
                db.execute(sa.text(sql))
            except Exception:
                continue
        try:
            db.commit()
        except Exception:
            pass


def _parse_yes_no(v: str | None, default: str = "NO") -> str:
    s = (v or "").strip().upper()
    return "YES" if s == "YES" else ("NO" if default.upper() != "YES" else "YES")


def _parse_date(v: str) -> date:
    try:
        return date.fromisoformat((v or "").strip())
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid date")


def _parse_dt_local(v: str) -> datetime:
    try:
        return datetime.fromisoformat((v or "").strip())
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid datetime")


def _parse_money_eur_to_cents(v: str | None) -> int | None:
    s = (v or "").strip()
    if not s:
        return None
    try:
        amt = Decimal(s)
    except InvalidOperation:
        raise HTTPException(status_code=400, detail="Invalid amount")
    return int((amt * 100).quantize(Decimal("1")))


def _fmt_money(cents: int | None, currency: str | None = "EUR") -> str:
    if cents is None:
        return ""
    cur = (currency or "EUR").upper()
    value = Decimal(cents) / Decimal(100)
    if cur == "EUR":
        return f"€{value:.2f}"
    return f"{cur} {value:.2f}"


def _sms_sso_url(tenant_slug: str) -> str:
    """Return SMS app URL with tenant + SSO token."""
    base = (settings.SMS_APP_URL or "").strip().rstrip("/")
    if not base:
        base = "/sms"  # docker gateway (nginx maps /sms/)

    secret = (settings.SSO_SHARED_SECRET or "").strip() or settings.SECRET_KEY
    ser = URLSafeTimedSerializer(secret_key=secret, salt="calendo-sms-sso-v1")
    token = ser.dumps({"tenant": tenant_slug})

    join = "&" if "?" in base else "?"
    return f"{base}{join}tenant={tenant_slug}&sso={quote(token)}"


def _get_or_create_clinic_settings(db: Session, tenant_id: str) -> ClinicSettings:
    cs = db.query(ClinicSettings).filter(ClinicSettings.tenant_id == tenant_id).first()
    if cs:
        return cs
    cs = ClinicSettings(tenant_id=tenant_id)
    db.add(cs)
    db.commit()
    db.refresh(cs)
    return cs


def _get_or_create_app_license(db: Session) -> AppLicense:
    lic = db.query(AppLicense).order_by(AppLicense.id.asc()).first()
    if lic:
        return lic
    lic = AppLicense(id=1)
    db.add(lic)
    db.commit()
    db.refresh(lic)
    return lic


def _subscription_until(db: Session, tenant_id: str) -> datetime | None:
    sub = (
        db.query(Subscription)
        .filter(Subscription.tenant_id == tenant_id)
        .order_by(Subscription.ends_at.desc())
        .first()
    )
    if not sub:
        return None
    return getattr(sub, "ends_at", None)


def _base_context(db: Session, request: Request, tenant_slug: str, tenant_id: str) -> dict:
    clinic = _get_or_create_clinic_settings(db, tenant_id)
    license_obj = _get_or_create_app_license(db)
    until = _subscription_until(db, tenant_id)
    if until:
        request.session["subscription_until"] = until.replace(microsecond=0).isoformat()
    rolef = _role_flags(request)
    therapist = _therapist_for_current_user(db, request, tenant_id) if rolef["is_therapist"] else None

    return {
        "tenant_slug": tenant_slug,
        "clinic": clinic,
        "license": license_obj,
        "sms_app_url": _sms_sso_url(tenant_slug),
        "active": request.scope.get("route").name if request.scope.get("route") else "",
        "therapist_self": therapist,
        **rolef,
    }




def _ensure_people_archive_columns(db: Session) -> None:
    """Best-effort schema safety net for child/therapist archiving.

    Adds:
      - children.is_archived, children.archived_at
      - therapists.is_archived, therapists.archived_at
    Older deployments may not have these yet.
    """
    bind = db.get_bind()
    try:
        insp = sa.inspect(bind)
    except Exception:
        return

    plans = {
        "children": [
            ("is_archived", "BOOLEAN"),
            ("archived_at", "TIMESTAMP"),
        ],
        "therapists": [
            ("is_archived", "BOOLEAN"),
            ("archived_at", "TIMESTAMP"),
        ],
    }
    stmts: list[str] = []
    for table, cols_needed in plans.items():
        try:
            existing = {c.get("name") for c in insp.get_columns(table) if c.get("name")}
        except Exception:
            continue
        for col, typ in cols_needed:
            if col not in existing:
                stmts.append(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
        if "is_archived" not in existing:
            if bind.dialect.name == "postgresql":
                stmts.append(f"UPDATE {table} SET is_archived = FALSE WHERE is_archived IS NULL")
            else:
                stmts.append(f"UPDATE {table} SET is_archived = 0 WHERE is_archived IS NULL")
    for stmt in stmts:
        try:
            with bind.begin() as conn:
                conn.exec_driver_sql(stmt)
        except Exception:
            pass


def _is_active_filter(model):
    return sa.or_(getattr(model, "is_archived").is_(False), getattr(model, "is_archived").is_(None))

# -----------------------------
# UI: Suite & Dashboard
# -----------------------------


@router.get("/t/{tenant_slug}", include_in_schema=False)
def t_root(request: Request, tenant_slug: str):
    # Keep legacy /t/<slug> paths working.
    return RedirectResponse(url=f"{_rp(request)}/t/{tenant_slug}/suite", status_code=303)


@router.get("/t/{tenant_slug}/suite", response_class=HTMLResponse)
def suite(request: Request, tenant_slug: str):
    if (resp := _require_login(request)):
        return resp
    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request, requested_slug=tenant_slug)
        ctx = _base_context(db, request, ts, tid)
        return templates.TemplateResponse("pages/suite.html", {"request": request, **ctx})
    finally:
        db.close()


@router.get("/t/{tenant_slug}/dashboard", response_class=HTMLResponse)
@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, tenant_slug: str | None = None):
    if (resp := _require_login(request)):
        return resp
    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request, requested_slug=tenant_slug)

        # preflight billing schema (needed for count in older DBs)
        _ensure_billing_item_columns(db)

        children_count = db.query(Child).filter(Child.tenant_id == tid).count()
        appt_count = db.query(Appointment).filter(Appointment.tenant_id == tid).count()
        uploads_count = (
            db.query(Attachment)
            .join(Child, Child.id == Attachment.child_id)
            .filter(Child.tenant_id == tid)
            .count()
        )
        billing_count = db.query(BillingItem).filter(BillingItem.tenant_id == tid).count()

        rows = (
            db.query(Appointment)
            .options(joinedload(Appointment.child))
            .filter(Appointment.tenant_id == tid)
            .order_by(Appointment.starts_at.desc())
            .limit(12)
            .all()
        )
        ctx = _base_context(db, request, ts, tid)
        return templates.TemplateResponse(
            "pages/dashboard.html",
            {
                "request": request,
                **ctx,
                "children_count": children_count,
                "appt_count": appt_count,
                "uploads_count": uploads_count,
                "billing_count": billing_count,
                "rows": rows,
            },
        )
    finally:
        db.close()


# -----------------------------
# UI: Children
# -----------------------------


@router.get("/children", response_class=HTMLResponse)

def children_list(request: Request):
    if (resp := _require_login(request)):
        return resp
    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        _ensure_billing_item_columns(db)
        _ensure_people_archive_columns(db)
        guard = _assert_child_access(db, request, tid, -1) if _role_flags(request)["is_calendar_staff"] else None
        if guard:
            return guard

        show = (request.query_params.get("show") or "active").strip().lower()
        _ensure_people_archive_columns(db)
        q_children = db.query(Child).filter(Child.tenant_id == tid)
        if show == "archived":
            q_children = q_children.filter(Child.is_archived.is_(True))
        elif show != "all":
            q_children = q_children.filter(_is_active_filter(Child))

        if _role_flags(request)["is_therapist"]:
            assigned_ids = _assigned_child_ids_for_request(db, request, tid)
            if not assigned_ids:
                children = []
            else:
                children = q_children.filter(Child.id.in_(assigned_ids)).order_by(Child.full_name.asc()).all()
        else:
            children = q_children.order_by(Child.full_name.asc()).all()

        now = datetime.utcnow()
        meta: dict[int, dict[str, Any]] = {}
        for c in children:
            next_appt = (
                db.query(Appointment)
                .filter(Appointment.tenant_id == tid, Appointment.child_id == c.id, Appointment.starts_at >= now)
                .order_by(Appointment.starts_at.asc())
                .first()
            )
            last_appt = (
                db.query(Appointment)
                .filter(Appointment.tenant_id == tid, Appointment.child_id == c.id)
                .order_by(Appointment.starts_at.desc())
                .first()
            )
            unpaid_count = (
                db.query(BillingItem)
                .filter(BillingItem.tenant_id == tid, BillingItem.child_id == c.id)
                .filter(sa.func.upper(BillingItem.paid) != "YES")
                .count()
            )
            meta[c.id] = {
                "next_appt": getattr(next_appt, "starts_at", None) if next_appt else None,
                "last_attendance": getattr(last_appt, "attendance_status", None) if last_appt else None,
                "unpaid_count": unpaid_count,
                "p1_phone": getattr(c, "parent1_phone", None),
            }

        ctx = _base_context(db, request, ts, tid)
        return templates.TemplateResponse(
            "pages/children_list.html",
            {
                "request": request,
                **ctx,
                "children": children,
                "meta": meta,
                "show": show,
                "can_edit_child": _role_flags(request)["is_clinic_superuser"],
            },
        )
    finally:
        db.close()



@router.post("/children/create")
async def children_create(request: Request):
    if (resp := _require_login(request)):
        return resp
    if (guard := _require_superuser_role(request)):
        return guard

    form = await request.form()
    full_name = str(form.get("full_name") or "").strip()
    if not full_name:
        _toast(request, "Child name is required", "danger")
        return RedirectResponse(url=f"{_rp(request)}/children", status_code=303)

    dob_raw = str(form.get("date_of_birth") or "").strip()
    dob: date | None = None
    if dob_raw:
        try:
            dob = date.fromisoformat(dob_raw)
        except Exception:
            _toast(request, "Invalid date of birth", "danger")
            return RedirectResponse(url=f"{_rp(request)}/children", status_code=303)

    notes = str(form.get("notes") or "").strip() or None

    parent1_name = str(form.get("parent1_name") or "").strip() or None
    parent1_phone = str(form.get("parent1_phone") or "").strip() or None
    parent2_name = str(form.get("parent2_name") or "").strip() or None
    parent2_phone = str(form.get("parent2_phone") or "").strip() or None

    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        c = Child(
            tenant_id=tid,
            full_name=full_name,
            date_of_birth=dob,
            notes=notes,
            parent1_name=parent1_name,
            parent1_phone=parent1_phone,
            parent2_name=parent2_name,
            parent2_phone=parent2_phone,
        )
        db.add(c)
        db.commit()
        db.refresh(c)
        _toast(request, "Child created")
        return RedirectResponse(url=f"{_rp(request)}/children/{c.id}", status_code=303)
    finally:
        db.close()




@router.post("/children/{child_id}/edit")
async def child_update(request: Request, child_id: int):
    if (resp := _require_login(request)):
        return resp
    if (guard := _require_superuser_role(request)):
        return guard

    form = await request.form()
    full_name = str(form.get("full_name") or "").strip()
    if not full_name:
        _toast(request, "Child name is required", "danger")
        return RedirectResponse(url=f"{_rp(request)}/children/{child_id}?tab=overview&edit=1", status_code=303)

    dob_raw = str(form.get("date_of_birth") or "").strip()
    dob: date | None = None
    if dob_raw:
        try:
            dob = date.fromisoformat(dob_raw)
        except Exception:
            _toast(request, "Invalid date of birth", "danger")
            return RedirectResponse(url=f"{_rp(request)}/children/{child_id}?tab=overview&edit=1", status_code=303)

    notes = str(form.get("notes") or "").strip() or None
    parent1_name = str(form.get("parent1_name") or "").strip() or None
    parent1_phone = str(form.get("parent1_phone") or form.get("primary_sms_phone") or "").strip() or None
    parent2_name = str(form.get("parent2_name") or "").strip() or None
    parent2_phone = str(form.get("parent2_phone") or "").strip() or None

    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        if (guard := _assert_child_access(db, request, tid, child_id)):
            return guard
        child = db.query(Child).filter(Child.tenant_id == tid, Child.id == child_id).first()
        if not child:
            raise HTTPException(status_code=404, detail="Child not found")

        child.full_name = full_name
        child.date_of_birth = dob
        child.notes = notes
        child.parent1_name = parent1_name
        child.parent1_phone = parent1_phone
        child.parent2_name = parent2_name
        child.parent2_phone = parent2_phone
        db.add(child)
        db.commit()
        _toast(request, "Child details updated")
        return RedirectResponse(url=f"{_rp(request)}/children/{child_id}?tab=overview", status_code=303)
    finally:
        db.close()

@router.get("/children/{child_id}", response_class=HTMLResponse)
def child_detail(request: Request, child_id: int):
    if (resp := _require_login(request)):
        return resp
    tab = (request.query_params.get("tab") or "overview").strip().lower()

    # Convenience: links in UI may use ?tab=billing
    if tab == "billing":
        return RedirectResponse(url=f"{_rp(request)}/billing?child_id={child_id}", status_code=303)

    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        if (guard := _assert_child_access(db, request, tid, child_id)):
            return guard
        child = db.query(Child).filter(Child.tenant_id == tid, Child.id == child_id).first()
        if not child:
            raise HTTPException(status_code=404, detail="Child not found")

        appointments = (
            db.query(Appointment)
            .filter(Appointment.tenant_id == tid, Appointment.child_id == child_id)
            .order_by(Appointment.starts_at.desc())
            .limit(50)
            .all()
        )
        timeline = (
            db.query(TimelineEvent)
            .join(Child, Child.id == TimelineEvent.child_id)
            .filter(Child.tenant_id == tid, TimelineEvent.child_id == child_id)
            .order_by(TimelineEvent.occurred_at.desc())
            .limit(80)
            .all()
        )
        attachments = (
            db.query(Attachment)
            .filter(Attachment.child_id == child_id)
            .order_by(Attachment.created_at.desc())
            .all()
        )
        ctx = _base_context(db, request, ts, tid)
        return templates.TemplateResponse(
            "pages/child_detail.html",
            {
                "request": request,
                **ctx,
                "child": child,
                "appointments": appointments,
                "timeline": timeline,
                "attachments": attachments,
                "tab": tab,
                "edit_mode": (request.query_params.get("edit") or "").strip() in ("1", "true", "yes"),
                "can_edit_child": _role_flags(request)["is_clinic_superuser"],
            },
        )
    finally:
        db.close()



@router.post("/children/{child_id}/archive")
def child_archive(request: Request, child_id: int):
    if (resp := _require_login(request)):
        return resp
    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        _ensure_people_archive_columns(db)
        if (guard := _require_superuser_role(request)):
            return guard
        child = db.query(Child).filter(Child.tenant_id == tid, Child.id == child_id).first()
        if not child:
            raise HTTPException(status_code=404, detail="Child not found")
        child.is_archived = True
        child.archived_at = datetime.utcnow()
        db.add(child)
        db.commit()
        _toast(request, "Child archived")
        return RedirectResponse(url=f"{_rp(request)}/children?tenant={ts}", status_code=303)
    finally:
        db.close()


@router.post("/children/{child_id}/restore")
def child_restore(request: Request, child_id: int):
    if (resp := _require_login(request)):
        return resp
    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        _ensure_people_archive_columns(db)
        if (guard := _require_superuser_role(request)):
            return guard
        child = db.query(Child).filter(Child.tenant_id == tid, Child.id == child_id).first()
        if not child:
            raise HTTPException(status_code=404, detail="Child not found")
        child.is_archived = False
        child.archived_at = None
        db.add(child)
        db.commit()
        _toast(request, "Child restored")
        return RedirectResponse(url=f"{_rp(request)}/children?tenant={ts}&show=archived", status_code=303)
    finally:
        db.close()

# -----------------------------
# UI: Therapists
# -----------------------------


WEEKDAYS: list[tuple[str, str]] = [
    ("mon", "Monday"),
    ("tue", "Tuesday"),
    ("wed", "Wednesday"),
    ("thu", "Thursday"),
    ("fri", "Friday"),
    ("sat", "Saturday"),
    ("sun", "Sunday"),
]


def _time_to_minutes(s: str) -> int | None:
    s = (s or "").strip()
    if not s:
        return None
    try:
        hh, mm = s.split(":")
        return int(hh) * 60 + int(mm)
    except Exception:
        return None


def _blocks_hours(blocks: list[dict[str, str]]) -> float:
    total = 0
    for b in blocks:
        a = _time_to_minutes(b.get("start", ""))
        z = _time_to_minutes(b.get("end", ""))
        if a is None or z is None or z <= a:
            continue
        total += z - a
    return round(total / 60.0, 2)


@router.get("/therapists", response_class=HTMLResponse)

def therapists_list(request: Request):
    if (resp := _require_login(request)):
        return resp
    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        _ensure_people_archive_columns(db)
        if (guard := _require_superuser_role(request)):
            return guard
        show = (request.query_params.get("show") or "active").strip().lower()
        q = db.query(Therapist).filter(Therapist.tenant_id == tid)
        if show == "archived":
            q = q.filter(Therapist.is_archived.is_(True))
        elif show != "all":
            q = q.filter(_is_active_filter(Therapist))
        therapists = q.order_by(Therapist.name.asc()).all()
        ctx = _base_context(db, request, ts, tid)
        return templates.TemplateResponse(
            "pages/therapists.html",
            {"request": request, **ctx, "therapists": therapists, "show": show},
        )
    finally:
        db.close()



@router.post("/therapists/create")
async def therapist_create(request: Request):
    if (resp := _require_login(request)):
        return resp
    form = await request.form()
    name = str(form.get("name") or "").strip()
    if not name:
        _toast(request, "Therapist name is required", "danger")
        return RedirectResponse(url=f"{_rp(request)}/therapists", status_code=303)
    role = str(form.get("role") or "").strip() or None
    phone = str(form.get("phone") or "").strip() or None
    email = str(form.get("email") or "").strip() or None
    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        if (guard := _require_superuser_role(request)):
            return guard
        now = datetime.utcnow()
        t = Therapist(
            tenant_id=tid,
            name=name,
            role=role,
            phone=phone,
            email=email,
            created_at=now,
            updated_at=now,
        )
        db.add(t)
        db.commit()
        db.refresh(t)
        _toast(request, "Therapist created")
        return RedirectResponse(url=f"{_rp(request)}/therapists/{t.id}", status_code=303)
    finally:
        db.close()


@router.get("/therapists/{therapist_id}", response_class=HTMLResponse)
def therapist_detail(request: Request, therapist_id: int):
    if (resp := _require_login(request)):
        return resp
    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        if (guard := _require_superuser_role(request)):
            return guard
        t = db.query(Therapist).filter(Therapist.tenant_id == tid, Therapist.id == therapist_id).first()
        if not t:
            raise HTTPException(status_code=404, detail="Therapist not found")

        # availability
        avail: dict[str, list[dict[str, str]]] = {}
        try:
            avail = json.loads(getattr(t, "availability_json", "") or "{}") or {}
        except Exception:
            avail = {}
        # annual leave
        leaves: list[dict[str, str]] = []
        try:
            leaves = json.loads(getattr(t, "annual_leave_json", "") or "[]") or []
        except Exception:
            leaves = []

        weekly_hours = 0.0
        for day, blocks in avail.items():
            if isinstance(blocks, list):
                weekly_hours += _blocks_hours([b for b in blocks if isinstance(b, dict)])
        weekly_hours = round(weekly_hours, 2)

        # Month hours: simple estimate (weekly_hours * 4.33)
        month_hours = round(weekly_hours * 4.33, 2)
        month_label = datetime.utcnow().strftime("%b %Y")

        ctx = _base_context(db, request, ts, tid)
        return templates.TemplateResponse(
            "pages/therapist_detail.html",
            {
                "request": request,
                **ctx,
                "t": t,
                "avail": avail,
                "leaves": leaves,
                "weekdays": WEEKDAYS,
                "weekly_hours": weekly_hours,
                "month_hours": month_hours,
                "month_label": month_label,
            },
        )
    finally:
        db.close()


@router.post("/therapists/{therapist_id}/update")
async def therapist_update(request: Request, therapist_id: int):
    if (resp := _require_login(request)):
        return resp
    form = await request.form()
    name = str(form.get("name") or "").strip()
    if not name:
        _toast(request, "Therapist name is required", "danger")
        return RedirectResponse(url=f"{_rp(request)}/therapists/{therapist_id}", status_code=303)

    role = str(form.get("role") or "").strip() or None
    phone = str(form.get("phone") or "").strip() or None
    email = str(form.get("email") or "").strip() or None

    avail: dict[str, list[dict[str, str]]] = {}
    for key, _label in WEEKDAYS:
        blocks: list[dict[str, str]] = []
        s1, e1 = str(form.get(f"{key}_start1") or "").strip(), str(form.get(f"{key}_end1") or "").strip()
        s2, e2 = str(form.get(f"{key}_start2") or "").strip(), str(form.get(f"{key}_end2") or "").strip()
        if s1 and e1:
            blocks.append({"start": s1, "end": e1})
        if s2 and e2:
            blocks.append({"start": s2, "end": e2})
        if blocks:
            avail[key] = blocks

    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        t = db.query(Therapist).filter(Therapist.tenant_id == tid, Therapist.id == therapist_id).first()
        if not t:
            raise HTTPException(status_code=404, detail="Therapist not found")
        t.name = name
        t.role = role
        t.phone = phone
        t.email = email
        t.availability_json = json.dumps(avail, ensure_ascii=False)
        t.updated_at = datetime.utcnow()
        db.add(t)
        db.commit()
        _toast(request, "Therapist saved")
        return RedirectResponse(url=f"{_rp(request)}/therapists/{therapist_id}", status_code=303)
    finally:
        db.close()



@router.post("/therapists/{therapist_id}/archive")
async def therapist_archive(request: Request, therapist_id: int):
    if (resp := _require_login(request)):
        return resp
    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        _ensure_people_archive_columns(db)
        if (guard := _require_superuser_role(request)):
            return guard
        t = db.query(Therapist).filter(Therapist.tenant_id == tid, Therapist.id == therapist_id).first()
        if not t:
            raise HTTPException(status_code=404, detail="Therapist not found")
        t.is_archived = True
        t.archived_at = datetime.utcnow()
        db.add(t)
        db.commit()
        _toast(request, "Therapist archived")
        return RedirectResponse(url=f"{_rp(request)}/therapists?show=active", status_code=303)
    finally:
        db.close()


@router.post("/therapists/{therapist_id}/restore")
async def therapist_restore(request: Request, therapist_id: int):
    if (resp := _require_login(request)):
        return resp
    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        _ensure_people_archive_columns(db)
        if (guard := _require_superuser_role(request)):
            return guard
        t = db.query(Therapist).filter(Therapist.tenant_id == tid, Therapist.id == therapist_id).first()
        if not t:
            raise HTTPException(status_code=404, detail="Therapist not found")
        t.is_archived = False
        t.archived_at = None
        db.add(t)
        db.commit()
        _toast(request, "Therapist restored")
        return RedirectResponse(url=f"{_rp(request)}/therapists?show=archived", status_code=303)
    finally:
        db.close()

@router.post("/therapists/{therapist_id}/leave/add")
async def therapist_leave_add(request: Request, therapist_id: int):
    if (resp := _require_login(request)):
        return resp
    form = await request.form()
    start = str(form.get("start") or "").strip()
    end = str(form.get("end") or "").strip()
    reason = str(form.get("reason") or "").strip() or None
    if not start or not end:
        _toast(request, "Start/end required", "danger")
        return RedirectResponse(url=f"{_rp(request)}/therapists/{therapist_id}", status_code=303)

    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        t = db.query(Therapist).filter(Therapist.tenant_id == tid, Therapist.id == therapist_id).first()
        if not t:
            raise HTTPException(status_code=404, detail="Therapist not found")
        leaves: list[dict[str, str]] = []
        try:
            leaves = json.loads(getattr(t, "annual_leave_json", "") or "[]") or []
        except Exception:
            leaves = []
        leaves.append({"start": start, "end": end, "reason": reason or ""})
        t.annual_leave_json = json.dumps(leaves, ensure_ascii=False)
        db.add(t)
        db.commit()
        _toast(request, "Leave added")
        return RedirectResponse(url=f"{_rp(request)}/therapists/{therapist_id}", status_code=303)
    finally:
        db.close()


@router.post("/therapists/{therapist_id}/leave/remove")
async def therapist_leave_remove(request: Request, therapist_id: int):
    if (resp := _require_login(request)):
        return resp
    form = await request.form()
    idx_raw = str(form.get("idx") or "").strip()
    try:
        idx = int(idx_raw)
    except Exception:
        idx = -1

    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        t = db.query(Therapist).filter(Therapist.tenant_id == tid, Therapist.id == therapist_id).first()
        if not t:
            raise HTTPException(status_code=404, detail="Therapist not found")
        leaves: list[dict[str, str]] = []
        try:
            leaves = json.loads(getattr(t, "annual_leave_json", "") or "[]") or []
        except Exception:
            leaves = []
        if 0 <= idx < len(leaves):
            leaves.pop(idx)
            t.annual_leave_json = json.dumps(leaves, ensure_ascii=False)
            db.add(t)
            db.commit()
            _toast(request, "Leave removed")
        return RedirectResponse(url=f"{_rp(request)}/therapists/{therapist_id}", status_code=303)
    finally:
        db.close()


# -----------------------------
# UI: Calendar
# -----------------------------


@router.get("/calendar", response_class=HTMLResponse)
def calendar_page(request: Request):
    if (resp := _require_login(request)):
        return resp
    if (guard := _require_calendar_role(request)):
        return guard
    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        rolef = _role_flags(request)
        q_children = db.query(Child).filter(Child.tenant_id == tid)
        if rolef["is_therapist"]:
            assigned_ids = _assigned_child_ids_for_request(db, request, tid)
            children = q_children.filter(Child.id.in_(assigned_ids)).order_by(Child.full_name.asc()).all() if assigned_ids else []
        elif rolef["is_calendar_staff"] or rolef["is_clinic_superuser"]:
            children = q_children.order_by(Child.full_name.asc()).all()
        else:
            children = []
        therapists = db.query(Therapist).filter(Therapist.tenant_id == tid).filter(_is_active_filter(Therapist)).order_by(Therapist.name.asc()).all()
        therapists_payload: list[dict[str, Any]] = []
        for t in therapists:
            try:
                availability = json.loads(getattr(t, "availability_json", "") or "{}") or {}
            except Exception:
                availability = {}
            if not isinstance(availability, dict):
                availability = {}
            try:
                leaves = json.loads(getattr(t, "annual_leave_json", "") or "[]") or []
            except Exception:
                leaves = []
            if not isinstance(leaves, list):
                leaves = []
            therapists_payload.append(
                {
                    "id": t.id,
                    "name": t.name,
                    "availability": availability,
                    "leaves": leaves,
                }
            )
        selected_child_id: int | None = None
        raw = (request.query_params.get("child_id") or "").strip()
        if raw:
            try:
                selected_child_id = int(raw)
            except Exception:
                selected_child_id = None
        selected_therapist_ids: list[int] = []
        raw_multi = request.query_params.getlist("therapist_ids") if hasattr(request.query_params, "getlist") else []
        if not raw_multi:
            one = (request.query_params.get("therapist_ids") or "").strip()
            if one:
                raw_multi = [x for x in one.split(",") if x.strip()]
        for val in raw_multi:
            try:
                selected_therapist_ids.append(int(val))
            except Exception:
                continue
        if rolef["is_therapist"]:
            therapist_self = _therapist_for_current_user(db, request, tid)
            selected_therapist_ids = [therapist_self.id] if therapist_self else []
        ctx = _base_context(db, request, ts, tid)
        return templates.TemplateResponse(
            "pages/calendar.html",
            {
                "request": request,
                **ctx,
                "children": children,
                "therapists": therapists,
                "therapists_payload": therapists_payload,
                "selected_child_id": selected_child_id,
                "selected_therapist_ids": selected_therapist_ids,
            },
        )
    finally:
        db.close()


def _parse_calendar_range_dt(v: str | None) -> datetime | None:
    if not v:
        return None
    s = v.strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        # Try date-only
        try:
            d = date.fromisoformat(s[:10])
            dt = datetime.combine(d, time.min)
        except Exception:
            return None
    # Appointments are stored as naive datetimes (from <input type="datetime-local">).
    # Treat calendar ranges as local-naive by dropping tzinfo (do *not* convert),
    # otherwise we can clip events near the end boundary for positive offsets.
    if dt.tzinfo:
        dt = dt.replace(tzinfo=None)
    return dt


@router.get("/api/calendar_events")
def api_calendar_events(request: Request):
    if (resp := _require_login(request)):
        return resp
    if (guard := _require_calendar_role(request)):
        return guard

    start_dt = _parse_calendar_range_dt(request.query_params.get("start"))
    end_dt = _parse_calendar_range_dt(request.query_params.get("end"))
    if not start_dt or not end_dt:
        raise HTTPException(status_code=400, detail="start/end required")

    child_id: int | None = None
    raw = (request.query_params.get("child_id") or "").strip()
    if raw:
        try:
            child_id = int(raw)
        except Exception:
            child_id = None

    therapist_ids: list[int] = []
    raw_multi = request.query_params.getlist("therapist_ids") if hasattr(request.query_params, "getlist") else []
    if not raw_multi:
        one = (request.query_params.get("therapist_ids") or "").strip()
        if one:
            raw_multi = [x for x in one.split(",") if x.strip()]
    for val in raw_multi:
        try:
            therapist_ids.append(int(val))
        except Exception:
            continue

    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        _ensure_billing_item_columns(db)
        rolef = _role_flags(request)
        _ensure_people_archive_columns(db)
        therapist_by_id = {t.id: t for t in db.query(Therapist).filter(Therapist.tenant_id == tid).filter(_is_active_filter(Therapist)).all()}
        if rolef["is_therapist"]:
            therapist_self = _therapist_for_current_user(db, request, tid)
            therapist_ids = [therapist_self.id] if therapist_self else []
        therapist_names = [therapist_by_id[i].name for i in therapist_ids if i in therapist_by_id]

        allowed_child_ids = _assigned_child_ids_for_request(db, request, tid) if rolef["is_therapist"] else []

        # Appointments
        appt_q = (
            db.query(Appointment)
            .options(joinedload(Appointment.child))
            .join(Child, Child.id == Appointment.child_id)
            .filter(Appointment.tenant_id == tid, Child.tenant_id == tid)
            .filter(_is_active_filter(Child))
            .filter(Appointment.starts_at >= start_dt, Appointment.starts_at < end_dt)
        )
        if child_id:
            appt_q = appt_q.filter(Appointment.child_id == child_id)
        if allowed_child_ids:
            appt_q = appt_q.filter(Appointment.child_id.in_(allowed_child_ids))
        elif rolef["is_therapist"]:
            appt_q = appt_q.filter(sa.sql.false())
        if therapist_names:
            appt_q = appt_q.filter(Appointment.therapist_name.in_(therapist_names))
        appts = appt_q.all()

        # Billing (by date) - therapists do not see billing events
        start_d = start_dt.date()
        end_d = end_dt.date()
        bills = []
        if rolef["is_clinic_superuser"]:
            bill_q = (
                db.query(BillingItem)
                .options(joinedload(BillingItem.child))
                .join(Child, Child.id == BillingItem.child_id)
                .filter(Child.tenant_id == tid)
                .filter(BillingItem.billing_due >= start_d, BillingItem.billing_due < end_d)
            )
            if child_id:
                bill_q = bill_q.filter(BillingItem.child_id == child_id)
            bills = bill_q.all()

        # Journey / Timeline
        journey_types = ["PARENT_FEEDBACK", "COMMUNICATION", "EXERCISE", "NOTE", "APPT_CANCELLED"]
        tl_q = (
            db.query(TimelineEvent)
            .options(joinedload(TimelineEvent.child))
            .join(Child, Child.id == TimelineEvent.child_id)
            .filter(Child.tenant_id == tid)
            .filter(TimelineEvent.occurred_at >= start_dt, TimelineEvent.occurred_at < end_dt)
            .filter(TimelineEvent.event_type.in_(journey_types))
        )
        if child_id:
            tl_q = tl_q.filter(TimelineEvent.child_id == child_id)
        if allowed_child_ids:
            tl_q = tl_q.filter(TimelineEvent.child_id.in_(allowed_child_ids))
        elif rolef["is_therapist"]:
            tl_q = tl_q.filter(sa.sql.false())
        journey = tl_q.all()

        events: list[dict[str, Any]] = []
        rp = _rp(request)

        def appt_color(a: Appointment) -> str:
            s = (getattr(a, "attendance_status", None) or "UNCONFIRMED").upper()
            return {
                "ATTENDED": "#16a34a",
                "MISSED": "#dc2626",
                "CONFIRMED": "#2563eb",
                "UNCONFIRMED": "#6b7280",
                "CANCELLED_PROVIDER": "#f59e0b",
                "CANCELLED_ME": "#f97316",
            }.get(s, "#6b7280")

        for a in appts:
            start = a.starts_at
            end = a.ends_at or (a.starts_at + timedelta(minutes=60))
            child_name = getattr(getattr(a, "child", None), "full_name", "") or ""
            tname = (getattr(a, "therapist_name", "") or "").strip()
            title = f"{child_name} — {a.procedure}".strip(" —")
            if tname:
                title = f"{title} · {tname}"
            events.append(
                {
                    "id": f"appt-{a.id}",
                    "title": title,
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "color": appt_color(a),
                    "url": f"{rp}/appointments/{a.id}",
                }
            )

        for b in bills:
            paid = (getattr(b, "paid", "NO") or "NO").upper() == "YES"
            inv = (getattr(b, "invoice_created", "NO") or "NO").upper() == "YES"
            color = "#a855f7"
            state = "No invoice"
            if paid:
                color = "#22c55e"
                state = "Paid"
            elif inv:
                color = "#eab308"
                state = "Invoice created"

            amt = _fmt_money(getattr(b, "amount_cents", None), getattr(b, "currency", "EUR"))
            desc = (getattr(b, "description", "") or "").strip()
            child_name = getattr(getattr(b, "child", None), "full_name", "") or ""
            pieces = ["💳", child_name]
            if amt:
                pieces.append(amt)
            if desc:
                pieces.append(desc)
            pieces.append(f"({state})")
            title = " ".join([p for p in pieces if p])

            events.append(
                {
                    "id": f"bill-{b.id}",
                    "title": title,
                    "start": b.billing_due.isoformat(),
                    "allDay": True,
                    "color": color,
                    "url": f"{rp}/billing?child_id={b.child_id}",
                }
            )

        def journey_color(tl: TimelineEvent) -> str:
            et = (tl.event_type or "OTHER").upper()
            return {
                "PARENT_FEEDBACK": "#06b6d4",
                "COMMUNICATION": "#64748b",
                "EXERCISE": "#a855f7",
                "NOTE": "#475569",
                "APPT_CANCELLED": "#f97316",
            }.get(et, "#475569")

        def journey_icon(et: str) -> str:
            etu = (et or "OTHER").upper()
            return {
                "PARENT_FEEDBACK": "💬",
                "COMMUNICATION": "📞",
                "EXERCISE": "🏋️",
                "NOTE": "📝",
                "APPT_CANCELLED": "✕",
            }.get(etu, "🧭")

        for tl in journey:
            child_name = getattr(getattr(tl, "child", None), "full_name", "") or ""
            icon = journey_icon(tl.event_type)
            title = f"{icon} {child_name} — {tl.title}".strip()
            events.append(
                {
                    "id": f"journey-{tl.id}",
                    "title": title,
                    "start": tl.occurred_at.isoformat(),
                    "color": journey_color(tl),
                    "url": f"{rp}/timeline?child_id={tl.child_id}",
                }
            )

        return JSONResponse(events)
    finally:
        db.close()


@router.post("/calendar/add_appointment")
async def calendar_add_appointment(request: Request):
    if (resp := _require_login(request)):
        return resp
    if (guard := _require_calendar_role(request)):
        return guard
    form = await request.form()

    child_id_raw = str(form.get("child_id") or "").strip()
    starts_at_raw = str(form.get("starts_at") or "").strip()
    therapist_name = str(form.get("therapist_name") or "").strip()
    procedure = str(form.get("procedure") or "").strip() or "Session"
    also_add_tl = _parse_yes_no(str(form.get("also_add_timeline") or "YES"), default="YES")

    try:
        child_id = int(child_id_raw)
    except Exception:
        raise HTTPException(status_code=400, detail="child_id required")
    starts_at = _parse_dt_local(starts_at_raw)
    ends_at = starts_at + timedelta(minutes=60)

    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        if (guard := _assert_child_access(db, request, tid, child_id)):
            return guard
        child = db.query(Child).filter(Child.tenant_id == tid, Child.id == child_id).first()
        if not child:
            raise HTTPException(status_code=404, detail="Child not found")

        appt = Appointment(
            tenant_id=tid,
            child_id=child_id,
            therapist_name=therapist_name,
            starts_at=starts_at,
            ends_at=ends_at,
            procedure=procedure,
            attendance_status="UNCONFIRMED",
        )
        db.add(appt)
        db.commit()
        db.refresh(appt)

        if also_add_tl == "YES":
            tl = TimelineEvent(
                child_id=child_id,
                event_type="VISIT",
                occurred_at=starts_at,
                title=procedure,
                details=f"Therapist: {therapist_name}" if therapist_name else None,
            )
            db.add(tl)
            db.commit()

        _toast(request, "Appointment added")
        return RedirectResponse(url=f"{_rp(request)}/calendar?child_id={child_id}", status_code=303)
    finally:
        db.close()


@router.post("/calendar/add_billing")
async def calendar_add_billing(request: Request):
    if (resp := _require_login(request)):
        return resp
    if (guard := _require_calendar_role(request)):
        return guard
    form = await request.form()

    child_id_raw = str(form.get("child_id") or "").strip()
    due_raw = str(form.get("billing_due") or "").strip()
    invoice_created = _parse_yes_no(str(form.get("invoice_created") or "NO"))
    paid = _parse_yes_no(str(form.get("paid") or "NO"))
    parent_signed_off = _parse_yes_no(str(form.get("parent_signed_off") or "NO"))
    amount_cents = _parse_money_eur_to_cents(str(form.get("amount_eur") or "").strip() or None)
    description = str(form.get("description") or "").strip() or None

    try:
        child_id = int(child_id_raw)
    except Exception:
        raise HTTPException(status_code=400, detail="child_id required")
    due = _parse_date(due_raw)

    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        _ensure_billing_item_columns(db)

        child = db.query(Child).filter(Child.tenant_id == tid, Child.id == child_id).first()
        if not child:
            raise HTTPException(status_code=404, detail="Child not found")

        b = BillingItem(
            tenant_id=tid,
            child_id=child_id,
            billing_due=due,
            invoice_created=invoice_created,
            paid=paid,
            parent_signed_off=parent_signed_off,
            amount_cents=amount_cents,
            currency="EUR",
            description=description,
        )
        db.add(b)
        db.commit()
        db.refresh(b)

        # Add a journey event reflecting the billing state
        if paid == "YES":
            et = "PAYMENT"
            state = "Payment received"
        elif invoice_created == "YES":
            et = "INVOICE_ISSUED"
            state = "Invoice issued"
        else:
            et = "OTHER"
            state = "Billing due"

        amt = _fmt_money(amount_cents, "EUR")
        title_bits = [state]
        if amt:
            title_bits.append(amt)
        if description:
            title_bits.append(description)
        tl_title = " — ".join(title_bits)

        tl = TimelineEvent(
            child_id=child_id,
            event_type=et,
            occurred_at=datetime.combine(due, time(12, 0)),
            title=tl_title,
            details=None,
        )
        db.add(tl)
        db.commit()

        _toast(request, "Billing item added")
        return RedirectResponse(url=f"{_rp(request)}/calendar?child_id={child_id}", status_code=303)
    finally:
        db.close()


@router.post("/calendar/add_journey")
async def calendar_add_journey(request: Request):
    if (resp := _require_login(request)):
        return resp
    if (guard := _require_calendar_role(request)):
        return guard
    form = await request.form()

    child_id_raw = str(form.get("child_id") or "").strip()
    occurred_at_raw = str(form.get("occurred_at") or "").strip()
    event_type = str(form.get("event_type") or "OTHER").strip().upper() or "OTHER"
    title = str(form.get("title") or "").strip()
    details = str(form.get("details") or "").strip() or None

    if not title:
        raise HTTPException(status_code=400, detail="title required")
    try:
        child_id = int(child_id_raw)
    except Exception:
        raise HTTPException(status_code=400, detail="child_id required")

    occurred_at = _parse_dt_local(occurred_at_raw)

    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        if (guard := _assert_child_access(db, request, tid, child_id)):
            return guard
        child = db.query(Child).filter(Child.tenant_id == tid, Child.id == child_id).first()
        if not child:
            raise HTTPException(status_code=404, detail="Child not found")

        tl = TimelineEvent(
            child_id=child_id,
            event_type=event_type,
            occurred_at=occurred_at,
            title=title,
            details=details,
        )
        db.add(tl)
        db.commit()
        _toast(request, "Journey item added")
        return RedirectResponse(url=f"{_rp(request)}/calendar?child_id={child_id}", status_code=303)
    finally:
        db.close()


# -----------------------------
# UI: Billing
# -----------------------------


@router.get("/billing", response_class=HTMLResponse)
def billing_page(request: Request):
    if (resp := _require_login(request)):
        return resp
    if (guard := _require_superuser_role(request)):
        return guard
    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        _ensure_billing_item_columns(db)

        mode = (request.query_params.get("mode") or "display").strip().lower()
        if mode != "edit":
            mode = "display"

        children = db.query(Child).filter(Child.tenant_id == tid).order_by(Child.full_name.asc()).all()
        selected_child_id: int | None = None
        raw = (request.query_params.get("child_id") or "").strip()
        if raw:
            try:
                selected_child_id = int(raw)
            except Exception:
                selected_child_id = None

        q = (
            db.query(BillingItem)
            .options(joinedload(BillingItem.child))
            .filter(BillingItem.tenant_id == tid)
            .order_by(BillingItem.billing_due.desc())
        )
        if selected_child_id:
            q = q.filter(BillingItem.child_id == selected_child_id)
        items = q.limit(200).all()

        ctx = _base_context(db, request, ts, tid)
        return templates.TemplateResponse(
            "pages/billing.html",
            {
                "request": request,
                **ctx,
                "items": items,
                "children": children,
                "selected_child_id": selected_child_id,
                "mode": mode,
            },
        )
    finally:
        db.close()


@router.post("/billing/{billing_id}/update")
async def billing_update(request: Request, billing_id: int):
    if (resp := _require_login(request)):
        return resp
    if (guard := _require_superuser_role(request)):
        return guard
    form = await request.form()
    redirect_to = str(form.get("redirect") or "").strip()
    paid = _parse_yes_no(str(form.get("paid") or "NO"))
    invoice_created = _parse_yes_no(str(form.get("invoice_created") or "NO"))
    parent_signed_off = _parse_yes_no(str(form.get("parent_signed_off") or "NO"))

    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        _ensure_billing_item_columns(db)
        b = db.query(BillingItem).filter(BillingItem.tenant_id == tid, BillingItem.id == billing_id).first()
        if not b:
            raise HTTPException(status_code=404, detail="Billing item not found")
        b.paid = paid
        b.invoice_created = invoice_created
        b.parent_signed_off = parent_signed_off
        db.add(b)
        db.commit()
        _toast(request, "Billing updated")
        if redirect_to.startswith("/") and "//" not in redirect_to:
            return RedirectResponse(url=f"{_rp(request)}{redirect_to}", status_code=303)
        return RedirectResponse(url=f"{_rp(request)}/billing?child_id={b.child_id}&mode=edit", status_code=303)
    finally:
        db.close()


@router.get("/billing/inputs", response_class=HTMLResponse)
def billing_inputs(request: Request):
    if (resp := _require_login(request)):
        return resp
    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        children = db.query(Child).filter(Child.tenant_id == tid).order_by(Child.full_name.asc()).all()
        plans = (
            db.query(BillingPlan)
            .join(Child, Child.id == BillingPlan.child_id)
            .filter(Child.tenant_id == tid)
            .order_by(BillingPlan.start_date.desc())
            .all()
        )
        ctx = _base_context(db, request, ts, tid)
        return templates.TemplateResponse(
            "pages/billing_inputs.html",
            {"request": request, **ctx, "children": children, "plans": plans},
        )
    finally:
        db.close()


def _generate_due_dates(plan: BillingPlan, horizon_days: int = 365) -> list[date]:
    """Generate due dates for a plan (best-effort)."""
    out: list[date] = []
    start = plan.start_date
    until = plan.until_date
    if plan.indefinitely or not until:
        until = start + timedelta(days=horizon_days)

    if plan.frequency == "weekly":
        step_weeks = int(plan.every_n_weeks or 1)
        d = start
        while d <= until:
            out.append(d)
            d = d + timedelta(days=7 * step_weeks)
        return out

    # monthly
    dom = int(plan.day_of_month or max(1, min(28, start.day)))
    y, m = start.year, start.month

    def add_month(y: int, m: int) -> tuple[int, int]:
        m += 1
        if m > 12:
            return y + 1, 1
        return y, m

    d = date(y, m, min(dom, 28))
    if d < start:
        y, m = add_month(y, m)
        d = date(y, m, min(dom, 28))

    while d <= until:
        out.append(d)
        y, m = add_month(y, m)
        d = date(y, m, min(dom, 28))
    return out


@router.post("/billing/inputs/create")
async def billing_inputs_create(request: Request):
    if (resp := _require_login(request)):
        return resp
    form = await request.form()

    child_id_raw = str(form.get("child_id") or "").strip()
    freq = str(form.get("frequency") or "monthly").strip().lower()
    start_date_raw = str(form.get("start_date") or "").strip()
    until_date_raw = str(form.get("until_date") or "").strip()
    indefinitely = str(form.get("indefinitely") or "0").strip() in ("1", "true", "yes", "on")
    description = str(form.get("description") or "").strip() or None

    try:
        child_id = int(child_id_raw)
    except Exception:
        raise HTTPException(status_code=400, detail="child_id required")

    try:
        start_d = date.fromisoformat(start_date_raw)
    except Exception:
        raise HTTPException(status_code=400, detail="start_date required")

    until_d: date | None = None
    if until_date_raw:
        try:
            until_d = date.fromisoformat(until_date_raw)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid until_date")

    every_n_weeks = None
    day_of_month = None
    if freq == "weekly":
        every_n_weeks = int(str(form.get("every_n_weeks") or "1").strip() or 1)
    else:
        day_of_month = int(str(form.get("day_of_month") or "1").strip() or 1)

    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        _ensure_billing_item_columns(db)
        child = db.query(Child).filter(Child.tenant_id == tid, Child.id == child_id).first()
        if not child:
            raise HTTPException(status_code=404, detail="Child not found")

        plan = BillingPlan(
            child_id=child_id,
            frequency="weekly" if freq == "weekly" else "monthly",
            every_n_weeks=every_n_weeks,
            day_of_month=day_of_month,
            start_date=start_d,
            until_date=until_d,
            indefinitely=bool(indefinitely),
            description=description,
        )
        db.add(plan)
        db.commit()
        db.refresh(plan)

        # Generate billing items (avoid duplicates)
        due_dates = _generate_due_dates(plan)
        created = 0
        for d in due_dates:
            exists = (
                db.query(BillingItem)
                .filter(BillingItem.tenant_id == tid, BillingItem.child_id == child_id, BillingItem.billing_due == d)
                .first()
            )
            if exists:
                continue
            bi = BillingItem(
                tenant_id=tid,
                child_id=child_id,
                billing_due=d,
                paid="NO",
                invoice_created="NO",
                parent_signed_off="NO",
                description=description,
                currency="EUR",
            )
            db.add(bi)
            created += 1
        db.commit()

        _toast(request, f"Billing plan saved · created {created} rows")
        return RedirectResponse(url=f"{_rp(request)}/billing/inputs", status_code=303)
    finally:
        db.close()


# -----------------------------
# UI: Timeline
# -----------------------------


TIMELINE_TYPES: list[str] = [
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
def timeline_page(request: Request):
    if (resp := _require_login(request)):
        return resp
    child_id: int | None = None
    raw_child = (request.query_params.get("child_id") or "").strip()
    if raw_child:
        try:
            child_id = int(raw_child)
        except Exception:
            child_id = None
    event_type = (request.query_params.get("event_type") or "").strip().upper() or None

    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        rolef = _role_flags(request)
        if rolef["is_calendar_staff"]:
            return _redirect_suite(request, "Calendar staff do not have access to the clinical timeline.")
        q_children = db.query(Child).filter(Child.tenant_id == tid)
        if rolef["is_therapist"]:
            allowed_ids = _assigned_child_ids_for_request(db, request, tid)
            children = q_children.filter(Child.id.in_(allowed_ids)).order_by(Child.full_name.asc()).all() if allowed_ids else []
        else:
            children = q_children.order_by(Child.full_name.asc()).all()

        q = (
            db.query(TimelineEvent)
            .options(joinedload(TimelineEvent.child))
            .join(Child, Child.id == TimelineEvent.child_id)
            .filter(Child.tenant_id == tid)
            .order_by(TimelineEvent.occurred_at.desc())
        )
        if child_id:
            q = q.filter(TimelineEvent.child_id == child_id)
        if rolef["is_therapist"]:
            allowed_ids = _assigned_child_ids_for_request(db, request, tid)
            if allowed_ids:
                q = q.filter(TimelineEvent.child_id.in_(allowed_ids))
            else:
                q = q.filter(sa.sql.false())
        if event_type:
            q = q.filter(TimelineEvent.event_type == event_type)
        events = q.limit(200).all()

        ctx = _base_context(db, request, ts, tid)
        return templates.TemplateResponse(
            "pages/timeline.html",
            {
                "request": request,
                **ctx,
                "events": events,
                "children": children,
                "types": TIMELINE_TYPES,
                "selected_child_id": child_id,
                "selected_type": event_type,
            },
        )
    finally:
        db.close()


@router.post("/timeline/create")
async def timeline_create(request: Request):
    if (resp := _require_login(request)):
        return resp
    form = await request.form()
    child_id_raw = str(form.get("child_id") or "").strip()
    event_type = str(form.get("event_type") or "OTHER").strip().upper() or "OTHER"
    occurred_at_raw = str(form.get("occurred_at") or "").strip()
    title = str(form.get("title") or "").strip()
    details = str(form.get("details") or "").strip() or None

    if not title:
        _toast(request, "Title required", "danger")
        return RedirectResponse(url=f"{_rp(request)}/timeline", status_code=303)
    try:
        child_id = int(child_id_raw)
    except Exception:
        _toast(request, "Child required", "danger")
        return RedirectResponse(url=f"{_rp(request)}/timeline", status_code=303)

    occurred_at = _parse_dt_local(occurred_at_raw) if occurred_at_raw else datetime.utcnow()

    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        if (guard := _assert_child_access(db, request, tid, child_id)):
            return guard
        child = db.query(Child).filter(Child.tenant_id == tid, Child.id == child_id).first()
        if not child:
            raise HTTPException(status_code=404, detail="Child not found")
        tl = TimelineEvent(
            child_id=child_id,
            event_type=event_type,
            occurred_at=occurred_at,
            title=title,
            details=details,
        )
        db.add(tl)
        db.commit()
        _toast(request, "Timeline item added")
        return RedirectResponse(url=f"{_rp(request)}/timeline?child_id={child_id}", status_code=303)
    finally:
        db.close()


# -----------------------------
# UI: Appointments + Notes + Files
# -----------------------------


@router.get("/appointments/{appointment_id}", response_class=HTMLResponse)
def appointment_detail(request: Request, appointment_id: int):
    if (resp := _require_login(request)):
        return resp
    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        if _role_flags(request)["is_calendar_staff"]:
            return _redirect_suite(request, "Calendar staff do not have access to clinical appointment notes.")
        appt = (
            db.query(Appointment)
            .options(joinedload(Appointment.child))
            .filter(Appointment.tenant_id == tid, Appointment.id == appointment_id)
            .first()
        )
        if not appt:
            raise HTTPException(status_code=404, detail="Appointment not found")
        if (guard := _assert_child_access(db, request, tid, appt.child_id)):
            return guard

        note = db.query(SessionNote).filter(SessionNote.tenant_id == tid, SessionNote.appointment_id == appt.id).first()
        if not note:
            note = SessionNote(tenant_id=tid, appointment_id=appt.id)
            db.add(note)
            db.commit()
            db.refresh(note)

        previous_appt = (
            db.query(Appointment)
            .filter(
                Appointment.tenant_id == tid,
                Appointment.child_id == appt.child_id,
                Appointment.starts_at < appt.starts_at,
            )
            .order_by(Appointment.starts_at.desc())
            .first()
        )
        previous_note = None
        if previous_appt:
            previous_note = (
                db.query(SessionNote)
                .filter(
                    SessionNote.tenant_id == tid,
                    SessionNote.appointment_id == previous_appt.id,
                )
                .first()
            )

        uploads = db.query(Attachment).filter(Attachment.child_id == appt.child_id).order_by(Attachment.created_at.desc()).all()

        ctx = _base_context(db, request, ts, tid)
        return templates.TemplateResponse(
            "pages/session_detail.html",
            {
                "request": request,
                **ctx,
                "appt": appt,
                "note": note,
                "uploads": uploads,
                "previous_appt": previous_appt,
                "previous_note": previous_note,
            },
        )
    finally:
        db.close()


@router.post("/appointments/{appointment_id}/attendance")
async def appointment_attendance(request: Request, appointment_id: int):
    if (resp := _require_login(request)):
        return resp
    form = await request.form()
    status = str(form.get("attendance_status") or "UNCONFIRMED").strip().upper() or "UNCONFIRMED"
    note = str(form.get("attendance_note") or "").strip() or None
    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        appt = db.query(Appointment).filter(Appointment.tenant_id == tid, Appointment.id == appointment_id).first()
        if not appt:
            raise HTTPException(status_code=404, detail="Appointment not found")
        if (guard := _assert_child_access(db, request, tid, appt.child_id)) and _role_flags(request)["is_therapist"]:
            return guard
        appt.attendance_status = status

        # Backwards-compatible fields (template expects them; not all DBs have them)
        if hasattr(appt, "attendance_note"):
            setattr(appt, "attendance_note", note)
        if hasattr(appt, "attendance_marked_at"):
            setattr(appt, "attendance_marked_at", datetime.utcnow())

        db.add(appt)
        db.commit()
        _toast(request, "Attendance saved")
        return RedirectResponse(url=f"{_rp(request)}/appointments/{appointment_id}", status_code=303)
    finally:
        db.close()


@router.post("/appointments/{appointment_id}/note")
async def appointment_note_save(request: Request, appointment_id: int):
    if (resp := _require_login(request)):
        return resp
    form = await request.form()
    summary = str(form.get("summary") or "").strip() or None
    what_went_wrong = str(form.get("what_went_wrong") or "").strip() or None
    improvements = str(form.get("improvements") or "").strip() or None
    next_steps = str(form.get("next_steps") or "").strip() or None

    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        appt = db.query(Appointment).filter(Appointment.tenant_id == tid, Appointment.id == appointment_id).first()
        if not appt:
            raise HTTPException(status_code=404, detail="Appointment not found")
        if _role_flags(request)["is_calendar_staff"]:
            return _redirect_suite(request, "Calendar staff do not have access to session notes.")
        if (guard := _assert_child_access(db, request, tid, appt.child_id)):
            return guard
        note = db.query(SessionNote).filter(SessionNote.tenant_id == tid, SessionNote.appointment_id == appt.id).first()
        if not note:
            note = SessionNote(tenant_id=tid, appointment_id=appt.id)
        note.summary = summary
        note.what_went_wrong = what_went_wrong
        note.improvements = improvements
        note.next_steps = next_steps
        db.add(note)
        db.commit()
        _toast(request, "Session note saved")
        return RedirectResponse(url=f"{_rp(request)}/appointments/{appointment_id}", status_code=303)
    finally:
        db.close()


@router.post("/children/{child_id}/upload")
async def child_upload(request: Request, child_id: int):
    if (resp := _require_login(request)):
        return resp
    form = await request.form()
    upload: UploadFile | None = form.get("file")  # type: ignore
    if not upload:
        _toast(request, "File required", "danger")
        return RedirectResponse(url=f"{_rp(request)}/children/{child_id}", status_code=303)

    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        if (guard := _assert_child_access(db, request, tid, child_id)):
            return guard
        child = db.query(Child).filter(Child.tenant_id == tid, Child.id == child_id).first()
        if not child:
            raise HTTPException(status_code=404, detail="Child not found")
        att = save_upload(child_id=child_id, upload=upload)
        db.add(att)
        db.commit()
        _toast(request, "Uploaded")
        return RedirectResponse(url=f"{_rp(request)}/children/{child_id}", status_code=303)
    finally:
        db.close()


@router.get("/files/{attachment_id}")
def file_get(request: Request, attachment_id: int):
    if (resp := _require_login(request)):
        return resp
    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        att = (
            db.query(Attachment)
            .join(Child, Child.id == Attachment.child_id)
            .filter(Attachment.id == attachment_id, Child.tenant_id == tid)
            .first()
        )
        if not att:
            raise HTTPException(status_code=404, detail="File not found")
        if (guard := _assert_child_access(db, request, tid, att.child_id)):
            return guard
        return FileResponse(att.storage_path, filename=att.original_name, media_type=att.mime_type)
    finally:
        db.close()


@router.post("/attachments/{attachment_id}/delete")
async def attachment_delete(request: Request, attachment_id: int):
    if (resp := _require_login(request)):
        return resp
    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        att = (
            db.query(Attachment)
            .join(Child, Child.id == Attachment.child_id)
            .filter(Attachment.id == attachment_id, Child.tenant_id == tid)
            .first()
        )
        if not att:
            raise HTTPException(status_code=404, detail="File not found")
        path = att.storage_path
        child_id = att.child_id
        db.delete(att)
        db.commit()
        delete_file(path)
        _toast(request, "Deleted")
        return RedirectResponse(url=f"{_rp(request)}/children/{child_id}", status_code=303)
    finally:
        db.close()


# -----------------------------
# UI: Settings
# -----------------------------


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    if (resp := _require_login(request)):
        return resp
    if (guard := _require_superuser_role(request)):
        return guard
    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        clinic = _get_or_create_clinic_settings(db, tid)
        lic = _get_or_create_app_license(db)

        env_preview = "\n".join(
            [
                f"CLINIC_NAME={clinic.clinic_name}",
                f"INFOBIP_BASE_URL={clinic.infobip_base_url}",
                f"INFOBIP_SENDER={clinic.infobip_sender}",
                f"INFOBIP_API_KEY={clinic.infobip_api_key}",
                f"TENANT_SLUG={ts}",
            ]
        )

        ctx = _base_context(db, request, ts, tid)
        return templates.TemplateResponse(
            "pages/settings.html",
            {
                "request": request,
                **ctx,
                "clinic": clinic,
                "license": lic,
                "google_maps_link": clinic.map_url,
                "env_preview": env_preview,
            },
        )
    finally:
        db.close()


@router.post("/settings/clinic")
async def settings_clinic_save(request: Request):
    if (resp := _require_login(request)):
        return resp
    if (guard := _require_superuser_role(request)):
        return guard
    form = await request.form()
    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        clinic = _get_or_create_clinic_settings(db, tid)
        clinic.clinic_name = str(form.get("clinic_name") or "").strip() or clinic.clinic_name
        clinic.address = str(form.get("address") or "").strip() or ""
        lat_raw = str(form.get("lat") or "").strip()
        lng_raw = str(form.get("lng") or "").strip()
        clinic.lat = float(lat_raw) if lat_raw else None
        clinic.lng = float(lng_raw) if lng_raw else None
        clinic.updated_at = datetime.utcnow()
        db.add(clinic)
        db.commit()
        _toast(request, "Clinic settings saved")
        return RedirectResponse(url=f"{_rp(request)}/settings", status_code=303)
    finally:
        db.close()


@router.post("/settings/infobip")
async def settings_infobip_save(request: Request):
    if (resp := _require_login(request)):
        return resp
    if (guard := _require_superuser_role(request)):
        return guard
    form = await request.form()
    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        clinic = _get_or_create_clinic_settings(db, tid)
        clinic.infobip_base_url = str(form.get("infobip_base_url") or "").strip() or clinic.infobip_base_url
        clinic.infobip_sender = str(form.get("infobip_sender") or "").strip() or ""
        clinic.infobip_api_key = str(form.get("infobip_api_key") or "").strip() or ""
        # optional fields
        if hasattr(clinic, "infobip_username"):
            clinic.infobip_username = str(form.get("infobip_username") or "").strip() or ""
        if hasattr(clinic, "infobip_userkey"):
            clinic.infobip_userkey = str(form.get("infobip_userkey") or "").strip() or ""
        clinic.updated_at = datetime.utcnow()
        db.add(clinic)
        db.commit()
        _toast(request, "Infobip settings saved")
        return RedirectResponse(url=f"{_rp(request)}/settings", status_code=303)
    finally:
        db.close()


@router.get("/settings/env")
def settings_env_download(request: Request):
    if (resp := _require_login(request)):
        return resp
    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        clinic = _get_or_create_clinic_settings(db, tid)
        content = "\n".join(
            [
                f"CLINIC_NAME={clinic.clinic_name}",
                f"INFOBIP_BASE_URL={clinic.infobip_base_url}",
                f"INFOBIP_SENDER={clinic.infobip_sender}",
                f"INFOBIP_API_KEY={clinic.infobip_api_key}",
                f"TENANT_SLUG={ts}",
            ]
        )
        return Response(
            content,
            media_type="text/plain",
            headers={"Content-Disposition": "attachment; filename=client.env"},
        )
    finally:
        db.close()


@router.post("/settings/license")
async def settings_license_manual(request: Request):
    """Manual license controls (legacy)."""
    if (resp := _require_login(request)):
        return resp
    form = await request.form()
    product_mode = str(form.get("product_mode") or "BOTH").strip().upper() or "BOTH"
    action = str(form.get("action") or "TRIAL").strip().upper() or "TRIAL"
    weeks_raw = str(form.get("weeks") or "4").strip()
    try:
        weeks = int(weeks_raw)
    except Exception:
        weeks = 4

    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        lic = _get_or_create_app_license(db)
        lic.product_mode = product_mode
        now = datetime.utcnow()
        delta = timedelta(days=max(1, weeks) * 7)
        if action == "TRIAL":
            lic.trial_end = (lic.trial_end or now) + delta
        elif action == "RENEW_WEEKS":
            lic.license_end = (lic.license_end or now) + delta
        elif action == "RENEW_YEAR":
            lic.license_end = (lic.license_end or now) + timedelta(days=365)
        lic.updated_at = now
        db.add(lic)
        db.commit()
        _toast(request, "License updated")
        return RedirectResponse(url=f"{_rp(request)}/settings", status_code=303)
    finally:
        db.close()


@router.post("/settings/activate")
async def settings_activate(request: Request):
    """Activation code → license/trial renewal."""
    if (resp := _require_login(request)):
        return resp
    form = await request.form()
    code = str(form.get("activation_code") or "").strip()
    if not code:
        return RedirectResponse(url=f"{_rp(request)}/settings?err=missing_code", status_code=303)

    db = _db()
    try:
        # tenant not strictly required for activation; but keep access consistent
        ts, tid = _resolve_tenant_or_404(db, request)
        lic = _get_or_create_app_license(db)

        try:
            payload = verify_activation_code(code, settings.LICENSE_PUBLIC_KEY)
        except Exception:
            return RedirectResponse(url=f"{_rp(request)}/settings?err=invalid_code", status_code=303)

        # plan mapping: 1=1 week trial, 2=1 month trial, 3=1 year license
        now = datetime.utcnow()
        if payload.plan == 1:
            lic.trial_end = now + timedelta(days=7)
        elif payload.plan == 2:
            lic.trial_end = now + timedelta(days=30)
        elif payload.plan == 3:
            lic.license_end = now + timedelta(days=365)

        lic.client_id = payload.client_id
        lic.activation_token = code
        lic.plan = payload.plan
        lic.product_mode = payload.mode
        lic.activated_at = now
        lic.updated_at = now

        db.add(lic)
        db.commit()
        return RedirectResponse(url=f"{_rp(request)}/settings?ok=activated", status_code=303)
    finally:
        db.close()


# -----------------------------
# UI: SMS outbox
# -----------------------------


@router.get("/sms-outbox", response_class=HTMLResponse)
def sms_outbox(request: Request):
    if (resp := _require_login(request)):
        return resp
    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        outbox = (
            db.query(SmsOutbox)
            .filter(SmsOutbox.tenant_id == tid)
            .order_by(SmsOutbox.scheduled_at.desc())
            .limit(50)
            .all()
        )
        ctx = _base_context(db, request, ts, tid)
        return templates.TemplateResponse(
            "pages/sms_outbox.html",
            {"request": request, **ctx, "outbox": outbox},
        )
    finally:
        db.close()


@router.post("/sms-outbox/test")
async def sms_outbox_test(request: Request):
    if (resp := _require_login(request)):
        return resp
    form = await request.form()
    to_phone = str(form.get("to_phone") or "").strip()
    message = str(form.get("message") or "").strip()
    if not to_phone or not message:
        _toast(request, "Phone + message required", "danger")
        return RedirectResponse(url=f"{_rp(request)}/sms-outbox", status_code=303)

    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        item = SmsOutbox(
            id=str(uuid.uuid4()),
            tenant_id=tid,
            to_phone=to_phone,
            message=message,
            scheduled_at=datetime.utcnow(),
            status="queued",
            attempts=0,
        )
        db.add(item)
        db.commit()
        _toast(request, "Queued")
        return RedirectResponse(url=f"{_rp(request)}/sms-outbox", status_code=303)
    finally:
        db.close()


# -----------------------------
# Internal API (Portal <-> SMS)
# -----------------------------


def _require_internal_key(request: Request) -> None:
    expected = (settings.INTERNAL_API_KEY or "").strip()
    got = (request.headers.get("X-Internal-Key") or request.headers.get("x-internal-key") or "").strip()
    if not expected or got != expected:
        raise HTTPException(status_code=403, detail="Forbidden")


def _require_internal_token(request: Request) -> None:
    # legacy: SMS app uses INTERNAL_TOKEN == Portal SECRET_KEY
    expected = (settings.SECRET_KEY or "").strip()
    got = (request.headers.get("x-internal-token") or request.headers.get("X-Internal-Token") or "").strip()
    if not expected or got != expected:
        raise HTTPException(status_code=403, detail="Forbidden")


def _tenant_from_query_or_400(request: Request) -> str:
    slug = (request.query_params.get("tenant") or "").strip().lower()
    if not slug:
        raise HTTPException(status_code=400, detail="tenant required")
    return slug


@router.get("/api/internal/clinic_settings")
def api_internal_clinic_settings(request: Request):
    _require_internal_key(request)
    tenant_slug = _tenant_from_query_or_400(request)
    db = _db()
    try:
        t = db.query(Tenant).filter(Tenant.slug == tenant_slug).first()
        if not t:
            raise HTTPException(status_code=404, detail="Tenant not found")
        cs = _get_or_create_clinic_settings(db, t.id)
        return JSONResponse(
            {
                "tenant": tenant_slug,
                "clinic": {
                    "clinic_name": cs.clinic_name,
                    "address": cs.address,
                    "lat": cs.lat,
                    "lng": cs.lng,
                    "google_maps_link": cs.map_url,
                    "sms_provider": cs.sms_provider,
                    "infobip_base_url": cs.infobip_base_url,
                    "infobip_sender": cs.infobip_sender,
                    "infobip_api_key": cs.infobip_api_key,
                    "infobip_username": getattr(cs, "infobip_username", ""),
                    "infobip_userkey": getattr(cs, "infobip_userkey", ""),
                },
            }
        )
    finally:
        db.close()


@router.get("/api/internal/infobip")
def api_internal_infobip(request: Request):
    _require_internal_token(request)
    tenant_slug = _tenant_from_query_or_400(request)
    db = _db()
    try:
        t = db.query(Tenant).filter(Tenant.slug == tenant_slug).first()
        if not t:
            raise HTTPException(status_code=404, detail="Tenant not found")
        cs = _get_or_create_clinic_settings(db, t.id)
        return JSONResponse(
            {
                "tenant": tenant_slug,
                "infobip_base_url": cs.infobip_base_url,
                "infobip_sender": cs.infobip_sender,
                "infobip_api_key": cs.infobip_api_key,
            }
        )
    finally:
        db.close()


@router.get("/api/internal/children")
def api_internal_children_list(request: Request):
    _require_internal_key(request)
    tenant_slug = _tenant_from_query_or_400(request)
    db = _db()
    try:
        t = db.query(Tenant).filter(Tenant.slug == tenant_slug).first()
        if not t:
            raise HTTPException(status_code=404, detail="Tenant not found")
        _ensure_people_archive_columns(db)
        rows = db.query(Child).filter(Child.tenant_id == t.id).filter(_is_active_filter(Child)).order_by(Child.full_name.asc()).all()
        return JSONResponse(
            {
                "tenant": tenant_slug,
                "children": [
                    {
                        "id": c.id,
                        "full_name": c.full_name,
                        "date_of_birth": getattr(c, "date_of_birth", None).isoformat() if getattr(c, "date_of_birth", None) else None,
                        "notes": getattr(c, "notes", None),
                        "primary_sms_phone": getattr(c, "parent1_phone", None),
                        "parent1_name": getattr(c, "parent1_name", None),
                        "parent1_phone": getattr(c, "parent1_phone", None),
                        "primary_sms_phone": getattr(c, "parent1_phone", None),
                        "archived": bool(getattr(c, "is_archived", False)),
                        "parent2_name": getattr(c, "parent2_name", None),
                        "parent2_phone": getattr(c, "parent2_phone", None),
                    }
                    for c in rows
                ],
            }
        )
    finally:
        db.close()


@router.post("/api/internal/children")
async def api_internal_children_create(request: Request):
    _require_internal_key(request)
    tenant_slug = _tenant_from_query_or_400(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")

    full_name = str(payload.get("full_name") or "").strip()
    if not full_name:
        raise HTTPException(status_code=400, detail="full_name required")

    db = _db()
    try:
        t = db.query(Tenant).filter(Tenant.slug == tenant_slug).first()
        if not t:
            raise HTTPException(status_code=404, detail="Tenant not found")

        primary_sms_phone = str(payload.get("primary_sms_phone") or "").strip() or None
        parent1_phone = str(payload.get("parent1_phone") or "").strip() or primary_sms_phone
        c = Child(
            tenant_id=t.id,
            full_name=full_name,
            notes=str(payload.get("notes") or "").strip() or None,
            parent1_name=str(payload.get("parent1_name") or "").strip() or None,
            parent1_phone=parent1_phone,
            parent2_name=str(payload.get("parent2_name") or "").strip() or None,
            parent2_phone=str(payload.get("parent2_phone") or "").strip() or None,
        )
        db.add(c)
        db.commit()
        db.refresh(c)
        return JSONResponse({"ok": True, "id": c.id})
    finally:
        db.close()


@router.get("/t/{tenant_slug}/children", include_in_schema=False)
def t_children_alias(request: Request, tenant_slug: str):
    return RedirectResponse(url=f"{_rp(request)}/children?tenant={tenant_slug}", status_code=303)

@router.get("/t/{tenant_slug}/children/{child_id}", include_in_schema=False)
def t_child_detail_alias(request: Request, tenant_slug: str, child_id: int):
    return RedirectResponse(url=f"{_rp(request)}/children/{child_id}?tenant={tenant_slug}", status_code=303)

@router.get("/appointments", include_in_schema=False)
def appointments_alias(request: Request):
    return RedirectResponse(url=f"{_rp(request)}/calendar", status_code=303)

@router.get("/t/{tenant_slug}/appointments", include_in_schema=False)
def t_appointments_alias(request: Request, tenant_slug: str):
    return RedirectResponse(url=f"{_rp(request)}/calendar?tenant={tenant_slug}", status_code=303)


@router.get("/team", response_class=HTMLResponse)
def team_page(request: Request):
    if (resp := _require_login(request)):
        return resp
    if (guard := _require_superuser_role(request)):
        return guard
    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        _ensure_assignment_table(db)
        users = db.query(User).filter(User.tenant_id == tid).order_by(User.email.asc()).all()
        therapists = db.query(Therapist).filter(Therapist.tenant_id == tid).order_by(Therapist.name.asc()).all()
        children = db.query(Child).filter(Child.tenant_id == tid).order_by(Child.full_name.asc()).all()
        assignments = (
            db.query(ChildTherapistAssignment)
            .options(joinedload(ChildTherapistAssignment.child), joinedload(ChildTherapistAssignment.therapist))
            .filter(ChildTherapistAssignment.tenant_id == tid)
            .order_by(ChildTherapistAssignment.assigned_at.desc())
            .all()
        )
        ctx = _base_context(db, request, ts, tid)
        return templates.TemplateResponse(
            "pages/team.html",
            {
                "request": request,
                **ctx,
                "users": users,
                "therapists": therapists,
                "children": children,
                "assignments": assignments,
                "temp_password": request.session.pop("team_temp_password", None),
                "temp_email": request.session.pop("team_temp_email", None),
            },
        )
    finally:
        db.close()

@router.post("/team/users/create")
async def team_user_create(request: Request):
    if (resp := _require_login(request)):
        return resp
    if (guard := _require_superuser_role(request)):
        return guard
    form = await request.form()
    email = str(form.get("email") or "").strip().lower()
    role = str(form.get("role") or "calendar_staff").strip().lower()
    job_title = str(form.get("job_title") or "").strip() or None
    therapist_id_raw = str(form.get("therapist_id") or "").strip()
    if not email:
        _toast(request, "Email is required", "danger")
        return RedirectResponse(url=f"{_rp(request)}/team", status_code=303)
    allowed = {"clinic_superuser","calendar_staff","therapist"}
    if role not in allowed:
        role = "calendar_staff"
    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        existing = db.query(User).filter(User.tenant_id == tid, sa.func.lower(User.email) == email).first()
        temp_pw = generate_temp_password()
        pw_hash = bcrypt.hashpw(temp_pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        if existing:
            existing.password_hash = pw_hash
            existing.role = role
            existing.job_title = job_title
            existing.is_active = True
            existing.must_reset_password = True
            u = existing
        else:
            u = User(id=str(uuid.uuid4()), tenant_id=tid, email=email, password_hash=pw_hash, role=role, job_title=job_title, is_active=True, must_reset_password=True)
            db.add(u)
        db.commit()
        db.refresh(u)
        if role == "therapist" and therapist_id_raw:
            try:
                therapist_id = int(therapist_id_raw)
            except Exception:
                therapist_id = None
            if therapist_id:
                t = db.query(Therapist).filter(Therapist.tenant_id == tid, Therapist.id == therapist_id).first()
                if t:
                    t.email = email
                    if hasattr(t, "user_id"):
                        t.user_id = u.id
                    db.add(t)
                    db.commit()
        request.session["team_temp_password"] = temp_pw
        request.session["team_temp_email"] = email
        _toast(request, "User created / password reset", "success")
        return RedirectResponse(url=f"{_rp(request)}/team", status_code=303)
    finally:
        db.close()

@router.post("/team/users/{user_id}/toggle")
async def team_user_toggle(request: Request, user_id: str):
    if (resp := _require_login(request)):
        return resp
    if (guard := _require_superuser_role(request)):
        return guard
    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        u = db.query(User).filter(User.tenant_id == tid, User.id == user_id).first()
        if not u:
            raise HTTPException(status_code=404, detail="User not found")
        u.is_active = not bool(u.is_active)
        db.add(u)
        db.commit()
        _toast(request, "User access updated", "success")
        return RedirectResponse(url=f"{_rp(request)}/team", status_code=303)
    finally:
        db.close()

@router.post("/team/users/{user_id}/reset_password")
async def team_user_reset_password(request: Request, user_id: str):
    if (resp := _require_login(request)):
        return resp
    if (guard := _require_superuser_role(request)):
        return guard
    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        u = db.query(User).filter(User.tenant_id == tid, User.id == user_id).first()
        if not u:
            raise HTTPException(status_code=404, detail="User not found")
        temp_pw = generate_temp_password()
        u.password_hash = bcrypt.hashpw(temp_pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
        u.must_reset_password = True
        u.is_active = True
        db.add(u)
        db.commit()
        request.session["team_temp_password"] = temp_pw
        request.session["team_temp_email"] = u.email
        _toast(request, "Temporary password generated", "success")
        return RedirectResponse(url=f"{_rp(request)}/team", status_code=303)
    finally:
        db.close()

@router.post("/team/assignments/create")
async def team_assignment_create(request: Request):
    if (resp := _require_login(request)):
        return resp
    if (guard := _require_superuser_role(request)):
        return guard
    form = await request.form()
    child_id_raw = str(form.get("child_id") or "").strip()
    therapist_id_raw = str(form.get("therapist_id") or "").strip()
    try:
        child_id = int(child_id_raw)
        therapist_id = int(therapist_id_raw)
    except Exception:
        _toast(request, "Select both child and therapist", "danger")
        return RedirectResponse(url=f"{_rp(request)}/team", status_code=303)
    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        _ensure_assignment_table(db)
        existing = db.query(ChildTherapistAssignment).filter(ChildTherapistAssignment.tenant_id == tid, ChildTherapistAssignment.child_id == child_id, ChildTherapistAssignment.therapist_id == therapist_id).first()
        if existing:
            existing.is_active = True
            db.add(existing)
        else:
            db.add(ChildTherapistAssignment(tenant_id=tid, child_id=child_id, therapist_id=therapist_id, assigned_by_user_id=str(request.session.get("user_id") or "") or None, is_active=True))
        db.commit()
        _toast(request, "Child assigned to therapist", "success")
        return RedirectResponse(url=f"{_rp(request)}/team", status_code=303)
    finally:
        db.close()

@router.post("/team/assignments/{assignment_id}/toggle")
async def team_assignment_toggle(request: Request, assignment_id: int):
    if (resp := _require_login(request)):
        return resp
    if (guard := _require_superuser_role(request)):
        return guard
    db = _db()
    try:
        ts, tid = _resolve_tenant_or_404(db, request)
        a = db.query(ChildTherapistAssignment).filter(ChildTherapistAssignment.tenant_id == tid, ChildTherapistAssignment.id == assignment_id).first()
        if not a:
            raise HTTPException(status_code=404, detail="Assignment not found")
        a.is_active = not bool(a.is_active)
        db.add(a)
        db.commit()
        _toast(request, "Assignment updated", "success")
        return RedirectResponse(url=f"{_rp(request)}/team", status_code=303)
    finally:
        db.close()

@router.get("/api/internal/therapists")
def api_internal_therapists(request: Request):
    _require_internal_key(request)
    tenant_slug = _tenant_from_query_or_400(request)
    db = _db()
    try:
        t = db.query(Tenant).filter(Tenant.slug == tenant_slug).first()
        if not t:
            raise HTTPException(status_code=404, detail="Tenant not found")
        _ensure_people_archive_columns(db)
        rows = db.query(Therapist).filter(Therapist.tenant_id == t.id).filter(_is_active_filter(Therapist)).order_by(Therapist.name.asc()).all()
        return JSONResponse({"tenant": tenant_slug, "therapists": [{"id": x.id, "name": x.name, "email": x.email, "role": x.role, "phone": x.phone, "archived": bool(getattr(x, "is_archived", False))} for x in rows]})
    finally:
        db.close()
@router.post("/api/internal/therapists")
async def api_internal_therapists_create(request: Request):
    _require_internal_key(request)
    tenant_slug = _tenant_from_query_or_400(request)
    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid JSON")
    name = str(payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    db = _db()
    try:
        trow = db.query(Tenant).filter(Tenant.slug == tenant_slug).first()
        if not trow:
            raise HTTPException(status_code=404, detail="Tenant not found")
        _ensure_people_archive_columns(db)
        now = datetime.utcnow()
        th = Therapist(
            tenant_id=trow.id,
            name=name,
            phone=str(payload.get("phone") or "").strip() or None,
            email=str(payload.get("email") or "").strip() or None,
            role=str(payload.get("role") or "").strip() or "therapist",
            created_at=now,
            updated_at=now,
            is_archived=False,
        )
        db.add(th)
        db.commit()
        db.refresh(th)
        return JSONResponse({"ok": True, "id": th.id, "name": th.name})
    finally:
        db.close()


@router.post("/api/internal/children/{child_id}/archive")
async def api_internal_child_archive(request: Request, child_id: int):
    _require_internal_key(request)
    tenant_slug = _tenant_from_query_or_400(request)
    db = _db()
    try:
        t = db.query(Tenant).filter(Tenant.slug == tenant_slug).first()
        if not t:
            raise HTTPException(status_code=404, detail="Tenant not found")
        _ensure_people_archive_columns(db)
        c = db.query(Child).filter(Child.tenant_id == t.id, Child.id == child_id).first()
        if not c:
            raise HTTPException(status_code=404, detail="Child not found")
        c.is_archived = True
        c.archived_at = datetime.utcnow()
        db.add(c)
        db.commit()
        return JSONResponse({"ok": True})
    finally:
        db.close()


@router.post("/api/internal/children/{child_id}/restore")
async def api_internal_child_restore(request: Request, child_id: int):
    _require_internal_key(request)
    tenant_slug = _tenant_from_query_or_400(request)
    db = _db()
    try:
        t = db.query(Tenant).filter(Tenant.slug == tenant_slug).first()
        if not t:
            raise HTTPException(status_code=404, detail="Tenant not found")
        _ensure_people_archive_columns(db)
        c = db.query(Child).filter(Child.tenant_id == t.id, Child.id == child_id).first()
        if not c:
            raise HTTPException(status_code=404, detail="Child not found")
        c.is_archived = False
        c.archived_at = None
        db.add(c)
        db.commit()
        return JSONResponse({"ok": True})
    finally:
        db.close()


@router.post("/api/internal/therapists/{therapist_id}/archive")
async def api_internal_therapist_archive(request: Request, therapist_id: int):
    _require_internal_key(request)
    tenant_slug = _tenant_from_query_or_400(request)
    db = _db()
    try:
        t = db.query(Tenant).filter(Tenant.slug == tenant_slug).first()
        if not t:
            raise HTTPException(status_code=404, detail="Tenant not found")
        _ensure_people_archive_columns(db)
        th = db.query(Therapist).filter(Therapist.tenant_id == t.id, Therapist.id == therapist_id).first()
        if not th:
            raise HTTPException(status_code=404, detail="Therapist not found")
        th.is_archived = True
        th.archived_at = datetime.utcnow()
        db.add(th)
        db.commit()
        return JSONResponse({"ok": True})
    finally:
        db.close()


@router.post("/api/internal/therapists/{therapist_id}/restore")
async def api_internal_therapist_restore(request: Request, therapist_id: int):
    _require_internal_key(request)
    tenant_slug = _tenant_from_query_or_400(request)
    db = _db()
    try:
        t = db.query(Tenant).filter(Tenant.slug == tenant_slug).first()
        if not t:
            raise HTTPException(status_code=404, detail="Tenant not found")
        _ensure_people_archive_columns(db)
        th = db.query(Therapist).filter(Therapist.tenant_id == t.id, Therapist.id == therapist_id).first()
        if not th:
            raise HTTPException(status_code=404, detail="Therapist not found")
        th.is_archived = False
        th.archived_at = None
        db.add(th)
        db.commit()
        return JSONResponse({"ok": True})
    finally:
        db.close()



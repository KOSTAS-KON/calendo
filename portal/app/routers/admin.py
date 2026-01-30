from __future__ import annotations

import os
import secrets
import hashlib
import uuid
from datetime import datetime, timedelta
from typing import Optional

import bcrypt
import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy.exc import ProgrammingError

from app.db import get_db
from app.models.tenant import Tenant
from app.models.clinic_settings import ClinicSettings
from app.models.licensing import Plan, Subscription, ActivationCode, LicenseAuditLog
from app.utils.security import generate_temp_password


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# ----------------------------
# Session helpers
# ----------------------------
def _session(request: Request) -> dict:
    s = request.scope.get("session")
    return s if isinstance(s, dict) else {}


<<<<<<< HEAD
def _flash_set(request: Request, key: str, value):
=======
def _flash_set(request: Request, key: str, value) -> None:
>>>>>>> 828a19d (Fix admin actions: reset password + ensure tenant lifecycle columns + no 404s)
    s = request.scope.get("session")
    if isinstance(s, dict):
        s[key] = value


def _flash_pop(request: Request, key: str, default=None):
    s = request.scope.get("session")
    if isinstance(s, dict):
        return s.pop(key, default)
    return default


# ----------------------------
<<<<<<< HEAD
# Admin key auth
=======
# Admin-key auth
>>>>>>> 828a19d (Fix admin actions: reset password + ensure tenant lifecycle columns + no 404s)
# ----------------------------
def _expected_admin_key() -> str:
    return (os.getenv("ADMIN_KEY") or "").strip()


def _get_admin_key_from_request(request: Request) -> str:
<<<<<<< HEAD
    """
    Order:
    1) Header X-Admin-Key
    2) Session 'admin_key'
    3) Query param '?admin_key=' (one-time bootstrap, optional)
    """
    hdr = (request.headers.get("X-Admin-Key") or request.headers.get("x-admin-key") or "").strip()
    if hdr:
        return hdr
=======
    """Header first, then session, then optional ?admin_key= bootstrap."""
    hdr = (request.headers.get("X-Admin-Key") or request.headers.get("x-admin-key") or "").strip()
    if hdr:
        return hdr
    sess_key = (_session(request).get("admin_key") or "").strip()
    if sess_key:
        return sess_key
    # Optional one-time bootstrap fallback
    qp = (request.query_params.get("admin_key") or "").strip()
    return qp
>>>>>>> 828a19d (Fix admin actions: reset password + ensure tenant lifecycle columns + no 404s)

    sess_key = (_session(request).get("admin_key") or "").strip()
    if sess_key:
        return sess_key

    qp = (request.query_params.get("admin_key") or "").strip()
    if qp:
        return qp

    return ""


def _set_admin_session_key(request: Request, key: str) -> None:
    s = request.scope.get("session")
    if isinstance(s, dict):
        s["admin_key"] = key


def _clear_admin_session_key(request: Request) -> None:
    s = request.scope.get("session")
    if isinstance(s, dict):
        s.pop("admin_key", None)


<<<<<<< HEAD
def _require_admin(request: Request) -> None:
    expected = _expected_admin_key()
    if not expected:
        raise HTTPException(status_code=403, detail="ADMIN_KEY not configured")
    got = _get_admin_key_from_request(request)
    if got != expected:
        raise HTTPException(status_code=403, detail="Forbidden")


=======
>>>>>>> 828a19d (Fix admin actions: reset password + ensure tenant lifecycle columns + no 404s)
# ----------------------------
# Utilities
# ----------------------------
def _hash_code(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _portal_base(request: Request) -> str:
    return str(request.base_url).rstrip("/")


def _sms_base() -> str:
    return (os.getenv("SMS_APP_URL") or "").strip().rstrip("/")


def _sms_url_for_tenant(slug: str) -> str:
    base = _sms_base()
    if not base:
        return ""
    if base.endswith("/sms"):
        return f"{base}?tenant={slug}"
    return f"{base}/sms?tenant={slug}"


def ensure_default_plans(db: Session) -> None:
    defaults = [
        ("TRIAL_7D", "7-day Trial", 7),
        ("MONTHLY_30D", "Monthly (30 days)", 30),
        ("YEARLY_365D", "Yearly (365 days)", 365),
    ]
    for code, name, days in defaults:
        p = db.query(Plan).filter(Plan.code == code).first()
        if not p:
            db.add(Plan(code=code, name=name, duration_days=days, features_json="{}"))
    db.commit()


<<<<<<< HEAD
def _tenant_lifecycle_columns_ready(db: Session) -> bool:
    """
    Checks if tenants lifecycle columns exist (is_archived, archived_at, deleted_at, deleted_by).
    This prevents Admin pages from 500'ing when DB migrations haven't been applied yet.
    """
=======
def _tenant_lifecycle_ready(db: Session) -> bool:
    """Detect whether tenant lifecycle columns exist."""
>>>>>>> 828a19d (Fix admin actions: reset password + ensure tenant lifecycle columns + no 404s)
    try:
        needed = {"is_archived", "archived_at", "deleted_at", "deleted_by"}
        rows = db.execute(
            sa.text(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name='tenants'
                """
            )
        ).fetchall()
        cols = {r[0] for r in rows}
        return needed.issubset(cols)
    except Exception:
        return False


<<<<<<< HEAD
def _db_warning_banner(lifecycle_ready: bool) -> str:
    if lifecycle_ready:
        return ""
    return (
        "Database is missing tenant lifecycle columns (is_archived/archived_at/deleted_at/deleted_by). "
        "Run Alembic migrations (alembic upgrade head). Admin actions are in compatibility mode."
    )
=======
def _ensure_tenant_lifecycle_columns(db: Session) -> bool:
    """
    Self-healing guard: if lifecycle columns are missing, add them.
    This prevents admin actions (archive/delete/reset) from 500'ing in production.
    """
    if _tenant_lifecycle_ready(db):
        return True
    try:
        # Add columns idempotently (Postgres)
        db.execute(sa.text("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS is_archived BOOLEAN NOT NULL DEFAULT FALSE;"))
        db.execute(sa.text("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP NULL;"))
        db.execute(sa.text("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS deleted_at TIMESTAMP NULL;"))
        db.execute(sa.text("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS deleted_by VARCHAR(255) NULL;"))
        # Remove default for cleanliness
        try:
            db.execute(sa.text("ALTER TABLE tenants ALTER COLUMN is_archived DROP DEFAULT;"))
        except Exception:
            pass
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        print(f"[admin] WARNING: failed to ensure tenant lifecycle columns: {e}")
        return False
>>>>>>> 828a19d (Fix admin actions: reset password + ensure tenant lifecycle columns + no 404s)


# ----------------------------
# Admin landing/unlock
# ----------------------------
@router.get("/admin", response_class=HTMLResponse)
def admin_index(request: Request):
    base_url = _portal_base(request)
    sms_base = _sms_base()

    expected = _expected_admin_key()
    if not expected:
        return HTMLResponse("<h2>ADMIN_KEY not configured</h2>", status_code=403)

    got = _get_admin_key_from_request(request)
    authenticated = (got == expected)

<<<<<<< HEAD
    # If user passed ?admin_key=... and it is valid, store it once and redirect to tenants
    qp = (request.query_params.get("admin_key") or "").strip()
    if qp and qp == expected:
        _set_admin_session_key(request, qp)
        return RedirectResponse(url="/admin/tenants", status_code=303)

    if authenticated:
        # ensure session contains key so subsequent requests work
=======
    # bootstrap via ?admin_key=... (optional)
    qp = (request.query_params.get("admin_key") or "").strip()
    if qp and qp == expected:
        _set_admin_session_key(request, expected)
        return RedirectResponse(url="/admin/tenants", status_code=303)

    if authenticated:
        # Ensure it sticks in session
>>>>>>> 828a19d (Fix admin actions: reset password + ensure tenant lifecycle columns + no 404s)
        if (_session(request).get("admin_key") or "").strip() != expected:
            _set_admin_session_key(request, expected)
        return RedirectResponse(url="/admin/tenants", status_code=303)

    return templates.TemplateResponse(
        "admin/index.html",
        {
            "request": request,
            "base_url": base_url,
            "sms_base": sms_base,
            "authenticated": False,
            "tenants_url": f"{base_url}/admin/tenants",
            "licensing_url": f"{base_url}/admin/licensing",
            "links_url": f"{base_url}/admin/links",
            "error": request.query_params.get("err") or request.query_params.get("msg") or "",
        },
    )


@router.post("/admin")
def admin_index_post(request: Request, admin_key: str = Form("")):
    expected = _expected_admin_key()
    if not expected:
        return RedirectResponse(url="/admin?err=not_configured", status_code=303)

    admin_key = (admin_key or "").strip()
    if admin_key != expected:
        return RedirectResponse(url="/admin?err=invalid", status_code=303)

<<<<<<< HEAD
    # store in session so it persists
=======
>>>>>>> 828a19d (Fix admin actions: reset password + ensure tenant lifecycle columns + no 404s)
    _set_admin_session_key(request, expected)
    return RedirectResponse(url="/admin/tenants", status_code=303)


@router.get("/admin/logout")
def admin_logout(request: Request):
    _clear_admin_session_key(request)
    return RedirectResponse(url="/admin", status_code=303)


# ----------------------------
<<<<<<< HEAD
# Tenants (scale)
=======
# Tenants list (scale)
>>>>>>> 828a19d (Fix admin actions: reset password + ensure tenant lifecycle columns + no 404s)
# ----------------------------
@router.get("/admin/tenants", response_class=HTMLResponse)
def admin_tenants(
    request: Request,
    q: str = "",
    status: str = "active",  # active|archived|deleted|all
    page: int = 1,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    _require_admin(request)
    base_url = _portal_base(request)

    page = max(1, int(page or 1))
    limit = int(limit or 50)
    limit = max(10, min(200, limit))
    offset = (page - 1) * limit

<<<<<<< HEAD
    lifecycle_ready = _tenant_lifecycle_columns_ready(db)
    banner = _db_warning_banner(lifecycle_ready)

=======
    lifecycle_ready = _tenant_lifecycle_ready(db)
    banner = ""
    if not lifecycle_ready:
        banner = (
            "Database is missing tenant lifecycle columns (is_archived/archived_at/deleted_at/deleted_by). "
            "Archive/Delete/Bulk actions will self-heal columns if possible, but you should run migrations."
        )

    # Owner email subquery
>>>>>>> 828a19d (Fix admin actions: reset password + ensure tenant lifecycle columns + no 404s)
    from app.models.user import User

    role_rank = sa.case(
        (User.role == "owner", 0),
        (User.role == "admin", 1),
        else_=2,
    )

    owner_email_sq = (
        sa.select(User.email)
        .where(
            User.tenant_id == Tenant.id,
            User.is_active.is_(True),
            User.role.in_(["owner", "admin"]),
        )
        .order_by(role_rank.asc(), User.email.asc())
        .limit(1)
        .scalar_subquery()
    )

<<<<<<< HEAD
    query = db.query(Tenant, owner_email_sq.label("owner_email"))

    q_clean = (q or "").strip().lower()
    if q_clean:
        query = query.filter(
            sa.or_(
                sa.func.lower(Tenant.slug).contains(q_clean),
                sa.func.lower(Tenant.name).contains(q_clean),
                sa.func.lower(owner_email_sq).contains(q_clean),
            )
        )

    status = (status or "active").strip().lower()

    # Apply lifecycle filters only if columns exist
    if lifecycle_ready:
        if status == "archived":
            query = query.filter(Tenant.deleted_at.is_(None), Tenant.is_archived.is_(True))
        elif status == "deleted":
            query = query.filter(Tenant.deleted_at.is_not(None))
        elif status == "all":
            pass
        else:
            query = query.filter(Tenant.deleted_at.is_(None), Tenant.is_archived.is_(False))
    else:
        # Compatibility mode: no deleted/archived filtering available
        status = "all"

    try:
        total = query.count()
        rows = (
            query.order_by(Tenant.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
    except ProgrammingError:
        # If ORM mapping includes missing columns in Tenant model, fall back to minimal SELECT
        db.rollback()
        banner = banner or "Database schema is behind; showing minimal tenant list."
        total = db.execute(sa.text("SELECT COUNT(*) FROM tenants")).scalar() or 0
        rows = db.execute(
            sa.text(
                """
                SELECT id, slug, name, status, created_at
                FROM tenants
                ORDER BY created_at DESC
                OFFSET :off LIMIT :lim
                """
            ),
            {"off": offset, "lim": limit},
        ).fetchall()
        # Normalize into (tenant_like, owner_email)
        tenants = []
        for r in rows:
            tenants.append(
                {
                    "slug": r[1],
                    "name": r[2],
                    "status": r[3],
                    "is_archived": False,
                    "deleted_at": None,
                    "owner_email": "",
                    "suite_url": f"{base_url}/t/{r[1]}/suite",
                    "sms_url": _sms_url_for_tenant(r[1]),
                }
            )

        onboard = {
            "tenant_slug": _flash_pop(request, "onboard_tenant_slug", ""),
            "owner_email": _flash_pop(request, "onboard_owner_email", ""),
            "temp_password": _flash_pop(request, "onboard_temp_password", ""),
        }

        return templates.TemplateResponse(
            "admin/tenants.html",
            {
                "request": request,
                "tenants": tenants,
                "base_url": base_url,
                "onboard": onboard,
                "q": q_clean,
                "status": status,
                "page": page,
                "limit": limit,
                "total": total,
                "page_start": (offset + 1) if total else 0,
                "page_end": min(offset + limit, total),
                "pages": (total + limit - 1) // limit if limit else 1,
                "banner": banner,
                "lifecycle_ready": lifecycle_ready,
            },
        )
=======
    q_clean = (q or "").strip().lower()
    status = (status or "active").strip().lower()

    try:
        query = db.query(Tenant, owner_email_sq.label("owner_email"))

        if q_clean:
            query = query.filter(
                sa.or_(
                    sa.func.lower(Tenant.slug).contains(q_clean),
                    sa.func.lower(Tenant.name).contains(q_clean),
                    sa.func.lower(owner_email_sq).contains(q_clean),
                )
            )
>>>>>>> 828a19d (Fix admin actions: reset password + ensure tenant lifecycle columns + no 404s)

        if lifecycle_ready:
            if status == "archived":
                query = query.filter(Tenant.deleted_at.is_(None), Tenant.is_archived.is_(True))
            elif status == "deleted":
                query = query.filter(Tenant.deleted_at.is_not(None))
            elif status == "all":
                pass
            else:
                query = query.filter(Tenant.deleted_at.is_(None), Tenant.is_archived.is_(False))
        else:
            status = "all"

        total = query.count()
        rows = (
            query.order_by(Tenant.created_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        tenants = []
        for t, owner_email in rows:
            tenants.append(
                {
                    "slug": t.slug,
                    "name": t.name,
                    "status": t.status,
                    "is_archived": bool(getattr(t, "is_archived", False)),
                    "deleted_at": getattr(t, "deleted_at", None),
                    "owner_email": owner_email or "",
                    "suite_url": f"{base_url}/t/{t.slug}/suite",
                    "sms_url": _sms_url_for_tenant(t.slug),
                }
            )

    except ProgrammingError:
        # Compatibility fallback if ORM selection fails
        db.rollback()
        banner = banner or "Database schema is behind; showing minimal tenant list. Run migrations." 

        total = db.execute(sa.text("SELECT COUNT(*) FROM tenants")).scalar() or 0
        rows = db.execute(
            sa.text(
                """
                SELECT slug, name, status
                FROM tenants
                ORDER BY created_at DESC NULLS LAST
                OFFSET :off LIMIT :lim
                """
            ),
            {"off": offset, "lim": limit},
        ).fetchall()

        tenants = []
        for slug, name, status_val in rows:
            tenants.append(
                {
                    "slug": slug,
                    "name": name,
                    "status": status_val,
                    "is_archived": False,
                    "deleted_at": None,
                    "owner_email": "",
                    "suite_url": f"{base_url}/t/{slug}/suite",
                    "sms_url": _sms_url_for_tenant(slug),
                }
            )

    onboard = {
        "tenant_slug": _flash_pop(request, "onboard_tenant_slug", ""),
        "owner_email": _flash_pop(request, "onboard_owner_email", ""),
        "temp_password": _flash_pop(request, "onboard_temp_password", ""),
    }

    return templates.TemplateResponse(
        "admin/tenants.html",
        {
            "request": request,
            "tenants": tenants,
            "base_url": base_url,
            "onboard": onboard,
            "q": q_clean,
            "status": status,
            "page": page,
            "limit": limit,
            "total": total,
            "page_start": (offset + 1) if total else 0,
            "page_end": min(offset + limit, total),
            "pages": (total + limit - 1) // limit if limit else 1,
            "banner": banner,
<<<<<<< HEAD
            "lifecycle_ready": lifecycle_ready,
=======
>>>>>>> 828a19d (Fix admin actions: reset password + ensure tenant lifecycle columns + no 404s)
        },
    )


# ----------------------------
# Reset password (schema-safe)
# ----------------------------
@router.get("/admin/tenants/{slug}/reset_password", response_class=HTMLResponse)
def admin_reset_password_page(request: Request, slug: str, db: Session = Depends(get_db)):
    _require_admin(request)

    row = db.execute(
        sa.text("SELECT id, slug, name FROM tenants WHERE slug = :slug LIMIT 1"),
        {"slug": slug},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Tenant not found")

    tenant_id, tenant_slug, tenant_name = row[0], row[1], row[2]

    from app.models.user import User

    role_rank = sa.case(
        (User.role == "owner", 0),
        (User.role == "admin", 1),
        else_=2,
    )
    target = (
        db.query(User)
        .filter(User.tenant_id == tenant_id, User.is_active.is_(True))
        .order_by(role_rank.asc(), User.email.asc())
        .first()
    )

    return templates.TemplateResponse(
        "admin/reset_password.html",
        {
            "request": request,
            "tenant": {"id": tenant_id, "slug": tenant_slug, "name": tenant_name},
            "target_user": target,
            "done": False,
        },
    )


@router.post("/admin/tenants/{slug}/reset_password", response_class=HTMLResponse)
def admin_reset_password_do(
    request: Request,
    slug: str,
    user_email: str = Form(...),
    db: Session = Depends(get_db),
):
    _require_admin(request)

    row = db.execute(
        sa.text("SELECT id, slug, name FROM tenants WHERE slug = :slug LIMIT 1"),
        {"slug": slug},
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Tenant not found")

    tenant_id, tenant_slug, tenant_name = row[0], row[1], row[2]

    from app.models.user import User
    from app.utils.security import generate_temp_password

    email_lc = (user_email or "").strip().lower()
    user = db.query(User).filter(User.tenant_id == tenant_id, User.email == email_lc).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found for this tenant")

    temp_pw = generate_temp_password()
    user.password_hash = bcrypt.hashpw(temp_pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    user.must_reset_password = True
    db.add(user)

    try:
<<<<<<< HEAD
        t = Tenant(id=secrets.token_hex(16), slug=slug, name=name.strip(), status="active")
        if hasattr(t, "created_at"):
            t.created_at = datetime.utcnow()

        db.add(t)
        db.commit()
        db.refresh(t)

        db.add(ClinicSettings(tenant_id=t.id))
        db.commit()

        plan = db.query(Plan).filter(Plan.code == plan_code).first()
        if not plan:
            raise HTTPException(400, f"Unknown plan: {plan_code}")

        db.add(
            Subscription(
                id=secrets.token_hex(16),
                tenant_id=t.id,
                plan_id=plan.id,
                status="active",
                starts_at=datetime.utcnow(),
                ends_at=datetime.utcnow() + timedelta(days=int(plan.duration_days)),
                source="manual",
            )
        )

        from app.models.user import User

        temp_password = secrets.token_urlsafe(10)
        pw_hash = bcrypt.hashpw(temp_password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")

        owner = User(
            id=str(uuid.uuid4()),
            tenant_id=t.id,
            email=owner_email,
            password_hash=pw_hash,
            role="owner",
            is_active=True,
            must_reset_password=True,
        )
        db.add(owner)

=======
>>>>>>> 828a19d (Fix admin actions: reset password + ensure tenant lifecycle columns + no 404s)
        db.add(
            LicenseAuditLog(
                id=secrets.token_hex(16),
                tenant_id=tenant_id,
                event_type="reset_password",
                details_json=f'{{"user":"{user.email}"}}',
                created_at=datetime.utcnow(),
            )
        )
    except Exception:
        pass

    db.commit()

    return templates.TemplateResponse(
        "admin/reset_password.html",
        {
            "request": request,
            "tenant": {"id": tenant_id, "slug": tenant_slug, "name": tenant_name},
            "target_user": user,
            "done": True,
            "temp_password": temp_pw,
        },
    )

# ----------------------------
# Reset password (per-tenant)
# ----------------------------
@router.get("/admin/tenants/{slug}/reset_password", response_class=HTMLResponse)
def admin_reset_password_page(
    request: Request,
    slug: str,
    db: Session = Depends(get_db),
):
    """Show the reset password drawer/page for the tenant."""
    _require_admin(request)

    from app.models.user import User

    tenant = db.query(Tenant).filter(Tenant.slug == slug).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # Default target: first active owner/admin, else first active user
    role_rank = sa.case(
        (User.role == "owner", 0),
        (User.role == "admin", 1),
        else_=2,
    )
    target_user = (
        db.query(User)
        .filter(User.tenant_id == tenant.id, User.is_active.is_(True))
        .order_by(role_rank.asc(), User.email.asc())
        .first()
    )

    return templates.TemplateResponse(
        "admin/reset_password.html",
        {"request": request, "tenant": tenant, "target_user": target_user, "done": False},
    )


@router.post("/admin/tenants/{slug}/reset_password", response_class=HTMLResponse)
def admin_reset_password_do(
    request: Request,
    slug: str,
    user_email: str = Form(...),
    db: Session = Depends(get_db),
):
    """Generate a new temporary password for the given tenant user."""
    _require_admin(request)

    from app.models.user import User

    tenant = db.query(Tenant).filter(Tenant.slug == slug).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    email_lc = (user_email or "").strip().lower()
    if not email_lc:
        raise HTTPException(status_code=400, detail="User email is required")

    user = db.query(User).filter(User.tenant_id == tenant.id, User.email == email_lc).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found for this tenant")

    temp_pw = generate_temp_password()
    user.password_hash = bcrypt.hashpw(temp_pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    user.must_reset_password = True
    db.add(user)

    # Audit log (best-effort; never break reset if audit fails)
    try:
        db.add(
            LicenseAuditLog(
                id=secrets.token_hex(16),
                tenant_id=tenant.id,
                event_type="reset_password",
                details_json=f'{{"user":"{user.email}"}}',
                created_at=datetime.utcnow(),
            )
        )
    except Exception:
        pass

    db.commit()

    return templates.TemplateResponse(
        "admin/reset_password.html",
        {"request": request, "tenant": tenant, "target_user": user, "done": True, "temp_password": temp_pw},
    )


# ----------------------------
# Tenant lifecycle actions (self-healing)
# ----------------------------
@router.post("/admin/tenants/bulk")
def admin_tenants_bulk(
    request: Request,
    action: str = Form(...),
    slugs: list[str] = Form([]),
    db: Session = Depends(get_db),
):
    _require_admin(request)

    if not _tenant_lifecycle_columns_ready(db):
        raise HTTPException(400, "Tenant lifecycle columns missing. Run migrations before bulk actions.")

    action = (action or "").strip().lower()
    slugs = [s.strip().lower() for s in (slugs or []) if s and s.strip()]
    if not slugs:
        raise HTTPException(400, "No tenants selected")

<<<<<<< HEAD
    now = datetime.utcnow()
    actor = (_session(request).get("email") or "admin")

=======
    # Try to ensure columns exist (best effort)
    if not _ensure_tenant_lifecycle_columns(db):
        raise HTTPException(500, "Tenant lifecycle columns missing and could not be created. Run migrations.")

    now = datetime.utcnow()
    actor = (_session(request).get("email") or "admin")
>>>>>>> 828a19d (Fix admin actions: reset password + ensure tenant lifecycle columns + no 404s)
    tenants = db.query(Tenant).filter(Tenant.slug.in_(slugs)).all()
    if not tenants:
        raise HTTPException(404, "Tenants not found")

    if action == "archive":
        for t in tenants:
            if t.deleted_at is None:
                t.is_archived = True
                t.archived_at = now
    elif action == "unarchive":
        for t in tenants:
            if t.deleted_at is None:
                t.is_archived = False
                t.archived_at = None
    elif action == "delete":
        for t in tenants:
            t.deleted_at = now
            t.deleted_by = str(actor)
            t.is_archived = True
            t.archived_at = now
    elif action == "restore":
        for t in tenants:
            t.deleted_at = None
            t.deleted_by = None
    else:
        raise HTTPException(400, "Invalid action")

    db.commit()
    return RedirectResponse(url="/admin/tenants", status_code=303)


<<<<<<< HEAD
=======
@router.post("/admin/tenants/{slug}/archive")
def admin_tenant_archive(request: Request, slug: str, db: Session = Depends(get_db)):
    _require_admin(request)
    if not _ensure_tenant_lifecycle_columns(db):
        raise HTTPException(500, "Tenant lifecycle columns missing and could not be created. Run migrations.")
    t = db.query(Tenant).filter(Tenant.slug == slug).first()
    if not t:
        raise HTTPException(404, "Tenant not found")
    if t.deleted_at is None:
        t.is_archived = True
        t.archived_at = datetime.utcnow()
        db.commit()
    return RedirectResponse(url="/admin/tenants?status=active", status_code=303)


@router.post("/admin/tenants/{slug}/unarchive")
def admin_tenant_unarchive(request: Request, slug: str, db: Session = Depends(get_db)):
    _require_admin(request)
    if not _ensure_tenant_lifecycle_columns(db):
        raise HTTPException(500, "Tenant lifecycle columns missing and could not be created. Run migrations.")
    t = db.query(Tenant).filter(Tenant.slug == slug).first()
    if not t:
        raise HTTPException(404, "Tenant not found")
    if t.deleted_at is None:
        t.is_archived = False
        t.archived_at = None
        db.commit()
    return RedirectResponse(url="/admin/tenants?status=archived", status_code=303)


@router.post("/admin/tenants/{slug}/delete")
def admin_tenant_delete(request: Request, slug: str, db: Session = Depends(get_db)):
    _require_admin(request)
    if not _ensure_tenant_lifecycle_columns(db):
        raise HTTPException(500, "Tenant lifecycle columns missing and could not be created. Run migrations.")
    t = db.query(Tenant).filter(Tenant.slug == slug).first()
    if not t:
        raise HTTPException(404, "Tenant not found")
    now = datetime.utcnow()
    actor = (_session(request).get("email") or "admin")
    t.deleted_at = now
    t.deleted_by = str(actor)
    t.is_archived = True
    t.archived_at = now
    db.commit()
    return RedirectResponse(url="/admin/tenants?status=active", status_code=303)


@router.post("/admin/tenants/{slug}/restore")
def admin_tenant_restore(request: Request, slug: str, db: Session = Depends(get_db)):
    _require_admin(request)
    if not _ensure_tenant_lifecycle_columns(db):
        raise HTTPException(500, "Tenant lifecycle columns missing and could not be created. Run migrations.")
    t = db.query(Tenant).filter(Tenant.slug == slug).first()
    if not t:
        raise HTTPException(404, "Tenant not found")
    t.deleted_at = None
    t.deleted_by = None
    db.commit()
    return RedirectResponse(url="/admin/tenants?status=deleted", status_code=303)


# ----------------------------
# Licensing
# ----------------------------
>>>>>>> 828a19d (Fix admin actions: reset password + ensure tenant lifecycle columns + no 404s)
@router.get("/admin/licensing", response_class=HTMLResponse)
def admin_licensing(request: Request, tenant: Optional[str] = None, db: Session = Depends(get_db)):
    _require_admin(request)
    ensure_default_plans(db)

    tenants = db.query(Tenant).order_by(Tenant.slug.asc()).all()
    plans = db.query(Plan).order_by(Plan.duration_days.asc()).all()

    selected = None
    current_sub = None
    if tenant:
        selected = db.query(Tenant).filter(Tenant.slug == tenant).first()
        if selected:
            current_sub = (
                db.query(Subscription)
                .filter(Subscription.tenant_id == selected.id)
                .order_by(Subscription.ends_at.desc())
                .first()
            )

    return templates.TemplateResponse(
        "admin/licensing.html",
        {"request": request, "tenants": tenants, "plans": plans, "selected": selected, "current_sub": current_sub},
    )


@router.post("/admin/licensing/generate")
def admin_generate_code(
    request: Request,
    tenant_slug: str = Form(...),
    plan_code: str = Form(...),
    db: Session = Depends(get_db),
):
    _require_admin(request)
    ensure_default_plans(db)

    t = db.query(Tenant).filter(Tenant.slug == tenant_slug).first()
    if not t:
        raise HTTPException(404, "Tenant not found")

    plan = db.query(Plan).filter(Plan.code == plan_code).first()
    if not plan:
        raise HTTPException(400, "Plan not found")

    raw = f"{tenant_slug.upper()}-{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}"
    code_hash = _hash_code(raw)

    db.add(
        ActivationCode(
            id=secrets.token_hex(16),
            tenant_id=t.id,
            plan_id=plan.id,
            code_hash=code_hash,
            issued_at=datetime.utcnow(),
            redeem_by=datetime.utcnow() + timedelta(days=90),
            max_redemptions=1,
            redeemed_count=0,
            revoked_at=None,
            note="",
        )
    )
    db.add(
        LicenseAuditLog(
            id=secrets.token_hex(16),
            tenant_id=t.id,
            event_type="code_generated",
            details_json=f'{{"plan":"{plan.code}"}}',
            created_at=datetime.utcnow(),
        )
    )
    db.commit()

    return RedirectResponse(url=f"/admin/licensing?tenant={tenant_slug}&code={raw}", status_code=303)


<<<<<<< HEAD
@router.get("/admin/tenants/{slug}/reset_password", response_class=HTMLResponse)
def admin_reset_password_page(request: Request, slug: str, db: Session = Depends(get_db)):
    _require_admin(request)

    from app.models.user import User

    tenant = db.query(Tenant).filter(Tenant.slug == slug).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    # default target: first active owner/admin
    role_rank = sa.case(
        (User.role == "owner", 0),
        (User.role == "admin", 1),
        else_=2,
    )
    target = (
        db.query(User)
        .filter(User.tenant_id == tenant.id, User.is_active.is_(True))
        .order_by(role_rank.asc(), User.email.asc())
        .first()
    )

    return templates.TemplateResponse(
        "admin/reset_password.html",
        {
            "request": request,
            "tenant": tenant,
            "target_user": target,
            "done": False,
        },
    )


@router.post("/admin/tenants/{slug}/reset_password", response_class=HTMLResponse)
def admin_reset_password_do(
    request: Request,
    slug: str,
    user_email: str = Form(...),
    db: Session = Depends(get_db),
):
    _require_admin(request)

    from app.models.user import User
    from app.utils.security import generate_temp_password  # make sure this file exists

    tenant = db.query(Tenant).filter(Tenant.slug == slug).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")

    email_lc = (user_email or "").strip().lower()
    user = db.query(User).filter(User.tenant_id == tenant.id, User.email == email_lc).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found for this tenant")

    temp_pw = generate_temp_password()
    user.password_hash = bcrypt.hashpw(temp_pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    user.must_reset_password = True
    db.add(user)

    # Audit (optional, safe)
    try:
        db.add(
            LicenseAuditLog(
                id=secrets.token_hex(16),
                tenant_id=tenant.id,
                event_type="reset_password",
                details_json=f'{{"user":"{user.email}"}}',
                created_at=datetime.utcnow(),
            )
        )
    except Exception:
        pass

    db.commit()

    return templates.TemplateResponse(
        "admin/reset_password.html",
        {
            "request": request,
            "tenant": tenant,
            "target_user": user,
            "done": True,
            "temp_password": temp_pw,
        },
    )


=======
# ----------------------------
# Links
# ----------------------------
>>>>>>> 828a19d (Fix admin actions: reset password + ensure tenant lifecycle columns + no 404s)
@router.get("/admin/links", response_class=HTMLResponse)
def admin_links(request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    base_url = _portal_base(request)
<<<<<<< HEAD
    tenants = db.query(Tenant).order_by(Tenant.slug.asc()).all()
=======
>>>>>>> 828a19d (Fix admin actions: reset password + ensure tenant lifecycle columns + no 404s)

    tenants = db.query(Tenant).order_by(Tenant.slug.asc()).all()
    rows = []
    for t in tenants:
        rows.append(
            {
                "slug": t.slug,
                "name": t.name,
                "suite_url": f"{base_url}/t/{t.slug}/suite",
                "sms_url": _sms_url_for_tenant(t.slug),
            }
        )

    return templates.TemplateResponse(
        "admin/links.html",
        {
            "request": request,
            "base_url": base_url,
            "sms_base": _sms_base(),
            "admin_tenants_url": f"{base_url}/admin/tenants",
            "admin_licensing_url": f"{base_url}/admin/licensing",
            "admin_links_url": f"{base_url}/admin/links",
            "tenants": rows,
        },
    )

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

from app.db import get_db
from app.config import settings
from app.models.tenant import Tenant
from app.models.clinic_settings import ClinicSettings
from app.models.licensing import Plan, Subscription, ActivationCode, LicenseAuditLog
from app.utils.security import generate_temp_password

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


# ----------------------------
# Session + flash helpers
# ----------------------------
def _session(request: Request) -> dict:
    s = request.scope.get("session")
    return s if isinstance(s, dict) else {}


def _flash_set(request: Request, key: str, value) -> None:
    s = request.scope.get("session")
    if isinstance(s, dict):
        s[key] = value


def _flash_pop(request: Request, key: str, default=None):
    s = request.scope.get("session")
    if isinstance(s, dict):
        return s.pop(key, default)
    return default


# ----------------------------
# Admin key auth
# ----------------------------
def _expected_admin_key() -> str:
    return (os.getenv("ADMIN_KEY") or "").strip()



def _get_admin_key_from_request(request: Request) -> str:
    """Header first, then session, then (optionally) ?admin_key= bootstrap.

    WARNING: Query-string auth can leak via logs/referrers. Disabled by default.
    """
    hdr = (request.headers.get("X-Admin-Key") or request.headers.get("x-admin-key") or "").strip()
    if hdr:
        return hdr

    sess_key = (_session(request).get("admin_key") or "").strip()
    if sess_key:
        return sess_key

    if settings.ALLOW_ADMIN_KEY_QUERY:
        qp = (request.query_params.get("admin_key") or "").strip()
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


def _require_admin(request: Request) -> None:
    expected = _expected_admin_key()
    if not expected:
        raise HTTPException(status_code=403, detail="ADMIN_KEY not configured")
    got = _get_admin_key_from_request(request)
    if got != expected:
        raise HTTPException(status_code=403, detail="Forbidden")


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


def _tenant_lifecycle_ready(db: Session) -> bool:
    """Detect whether tenant lifecycle columns exist."""
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


def _ensure_tenant_lifecycle_columns(db: Session) -> bool:
    """
    Self-healing guard: if lifecycle columns are missing, add them.

    This prevents admin actions (archive/delete/bulk) from erroring out when
    migrations haven't been applied yet.
    """
    if _tenant_lifecycle_ready(db):
        return True

    try:
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


# ----------------------------
# Admin landing / unlock
# ----------------------------
@router.get("/admin", response_class=HTMLResponse)
def admin_index(request: Request):
    base_url = _portal_base(request)
    sms_base = _sms_base()

    expected = _expected_admin_key()
    if not expected:
        return HTMLResponse("<h2>ADMIN_KEY not configured</h2>", status_code=403)

    got = _get_admin_key_from_request(request)
    authenticated = got == expected

    # Optional bootstrap: /admin?admin_key=...
    qp = (request.query_params.get("admin_key") or "").strip()
    if qp and qp == expected:
        _set_admin_session_key(request, expected)
        return RedirectResponse(url="/admin/tenants", status_code=303)

    if authenticated:
        # Ensure it sticks in session
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

    if (admin_key or "").strip() != expected:
        return RedirectResponse(url="/admin?err=invalid", status_code=303)

    _set_admin_session_key(request, expected)
    return RedirectResponse(url="/admin/tenants", status_code=303)


@router.get("/admin/logout")
def admin_logout(request: Request):
    _clear_admin_session_key(request)
    return RedirectResponse(url="/admin", status_code=303)


# ----------------------------
# Tenants (scale)
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
    limit = max(10, min(200, int(limit or 50)))
    offset = (page - 1) * limit

    lifecycle_ready = _tenant_lifecycle_ready(db)
    banner = ""
    if not lifecycle_ready:
        banner = (
            "Database is missing tenant lifecycle columns (is_archived/archived_at/deleted_at/deleted_by). "
            "Archive/Delete/Bulk actions will self-heal columns if possible, but you should run migrations."
        )

    # Owner email subquery
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

    # Apply filters only if lifecycle columns exist
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

    try:
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
    except Exception:
        db.rollback()
        # Minimal fallback
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
        for slug_val, name_val, status_val in rows:
            tenants.append(
                {
                    "slug": slug_val,
                    "name": name_val,
                    "status": status_val,
                    "is_archived": False,
                    "deleted_at": None,
                    "owner_email": "",
                    "suite_url": f"{base_url}/t/{slug_val}/suite",
                    "sms_url": _sms_url_for_tenant(slug_val),
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

    email_lc = (user_email or "").strip().lower()
    user = db.query(User).filter(User.tenant_id == tenant_id, User.email == email_lc).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found for this tenant")

    temp_pw = generate_temp_password()
    user.password_hash = bcrypt.hashpw(temp_pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    user.must_reset_password = True
    db.add(user)

    # best-effort audit
    try:
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

    if not _ensure_tenant_lifecycle_columns(db):
        raise HTTPException(500, "Tenant lifecycle columns missing and could not be created. Run migrations.")

    action = (action or "").strip().lower()
    slugs = [s.strip().lower() for s in (slugs or []) if s and s.strip()]
    if not slugs:
        raise HTTPException(400, "No tenants selected")

    now = datetime.utcnow()
    actor = (_session(request).get("email") or "admin")

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
    return RedirectResponse(url="/admin/tenants", status_code=303)


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
    return RedirectResponse(url="/admin/tenants", status_code=303)


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
    return RedirectResponse(url="/admin/tenants", status_code=303)


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
    return RedirectResponse(url="/admin/tenants", status_code=303)


# ----------------------------
# Licensing
# ----------------------------
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


# ----------------------------
# Links
# ----------------------------
@router.get("/admin/links", response_class=HTMLResponse)
def admin_links(request: Request, db: Session = Depends(get_db)):
    _require_admin(request)

    base_url = _portal_base(request)
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

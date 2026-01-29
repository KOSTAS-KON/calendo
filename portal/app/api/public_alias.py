from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, JSONResponse

from app.db import SessionLocal


router = APIRouter(prefix="/api", tags=["api-alias"])


def _format_iso_utc(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat() + "Z"


def _resolve_tenant_slug(request: Request) -> str:
    """
    Prefer:
    1) query param ?tenant=...
    2) session tenant_slug
    3) default
    """
    tenant = (request.query_params.get("tenant") or "").strip().lower()
    if tenant:
        return tenant

    s = request.scope.get("session")
    if isinstance(s, dict) and s.get("tenant_slug"):
        return str(s.get("tenant_slug") or "default").strip().lower()

    return "default"


@router.get("/clinic_settings")
def clinic_settings_alias():
    """
    Backward-compatible alias for the UI.
    Your backend already serves /api/internal/clinic_settings (confirmed by logs),
    so we redirect to it.
    """
    return RedirectResponse(url="/api/internal/clinic_settings", status_code=307)


@router.get("/license")
def license_alias(request: Request):
    """
    UI expects /api/license.
    Return subscription/license info based on latest Subscription for tenant.
    """
    tenant_slug = _resolve_tenant_slug(request)
    db = SessionLocal()
    try:
        from app.models.tenant import Tenant
        from app.models.licensing import Subscription, Plan

        t = db.query(Tenant).filter(Tenant.slug == tenant_slug).first()
        if not t:
            return JSONResponse({"tenant": tenant_slug, "active": False, "until": None, "plan": None})

        sub = (
            db.query(Subscription)
            .filter(Subscription.tenant_id == t.id)
            .order_by(Subscription.ends_at.desc())
            .first()
        )
        if not sub or not getattr(sub, "ends_at", None):
            return JSONResponse({"tenant": tenant_slug, "active": False, "until": None, "plan": None})

        plan_code = None
        plan_name = None
        try:
            p = db.query(Plan).filter(Plan.id == sub.plan_id).first()
            if p:
                plan_code = getattr(p, "code", None)
                plan_name = getattr(p, "name", None)
        except Exception:
            pass

        status = str(getattr(sub, "status", "active") or "active").lower()
        active = bool(status not in ("canceled", "expired") and sub.ends_at > datetime.utcnow())

        return JSONResponse(
            {
                "tenant": tenant_slug,
                "active": active,
                "until": _format_iso_utc(sub.ends_at),
                "plan": {"code": plan_code, "name": plan_name},
            }
        )
    finally:
        db.close()

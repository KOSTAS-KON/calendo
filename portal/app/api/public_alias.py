from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from app.db import SessionLocal
from app.config import settings

router = APIRouter(prefix="/api", tags=["api-alias"])


def _format_iso_utc(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat() + "Z"



def _resolve_tenant_slug(request: Request) -> str:
    """Resolve tenant for *public* endpoints safely.

    Security: avoid leaking tenant-specific info to unauthenticated callers.
    - If a user session exists, use its tenant_slug.
    - Otherwise, only allow "default" (marketing/demo) and ignore arbitrary ?tenant=...
    """
    s = request.scope.get("session")
    if isinstance(s, dict) and s.get("tenant_slug"):
        return str(s.get("tenant_slug") or "default").strip().lower()

    tenant = (request.query_params.get("tenant") or "").strip().lower()
    if tenant == "default" or not tenant:
        return "default"

    # Allow internal services (SMS) to request a tenant explicitly, but only with INTERNAL_API_KEY.
    internal_key = (request.headers.get("x-internal-key") or request.headers.get("X-Internal-Key") or "").strip()
    expected = (settings.INTERNAL_API_KEY or "").strip()
    if expected and internal_key and internal_key == expected:
        return tenant

    # Unauthenticated callers cannot enumerate other tenants
    return "default"


@router.get("/clinic_settings")
def clinic_settings_public(request: Request):
    """Public (UI) clinic settings endpoint.

    NOTE: We DO NOT proxy/redirect to /api/internal/clinic_settings because that endpoint
    may be protected (403). We read from DB and return safe JSON.
    """
    tenant_slug = _resolve_tenant_slug(request)
    db = SessionLocal()
    try:
        from app.models.tenant import Tenant
        from app.models.clinic_settings import ClinicSettings

        t = db.query(Tenant).filter(Tenant.slug == tenant_slug).first()
        if not t:
            raise HTTPException(status_code=404, detail="Tenant not found")

        cs = db.query(ClinicSettings).filter(ClinicSettings.tenant_id == t.id).first()
        if not cs:
            return JSONResponse(
                {
                    "tenant": tenant_slug,
                    "clinic_name": "",
                    "address": "",
                    "google_maps_link": "",
                    "lat": None,
                    "lng": None,
                    "sms_provider": "infobip",
                }
            )

        payload: Dict[str, Any] = {
            "tenant": tenant_slug,
            "clinic_name": getattr(cs, "clinic_name", "") or "",
            "address": getattr(cs, "address", "") or "",
            "google_maps_link": getattr(cs, "google_maps_link", "") or "",
            "lat": getattr(cs, "lat", None),
            "lng": getattr(cs, "lng", None),
            "sms_provider": getattr(cs, "sms_provider", "infobip") or "infobip",
        }
        return JSONResponse(payload)
    finally:
        db.close()


@router.get("/license")
def license_public(request: Request):
    """Public license/subscription status for UI."""
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

        plan_code: Optional[str] = None
        plan_name: Optional[str] = None
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

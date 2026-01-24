from __future__ import annotations

from dataclasses import dataclass
from fastapi import HTTPException, Request
from sqlalchemy.orm import Session

from app.models.tenant import Tenant


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    tenant_slug: str
    tenant_name: str
    status: str


def resolve_tenant(db: Session, request: Request, tenant_slug: str = "default") -> TenantContext:
    t = db.query(Tenant).filter(Tenant.slug == tenant_slug).first()
    if not t:
        raise HTTPException(status_code=404, detail=f"Tenant '{tenant_slug}' not found")
    if (t.status or "active") != "active":
        raise HTTPException(status_code=403, detail="Tenant is suspended")
    return TenantContext(tenant_id=t.id, tenant_slug=t.slug, tenant_name=t.name, status=t.status)

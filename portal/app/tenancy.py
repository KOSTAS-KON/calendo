from __future__ import annotations

from dataclasses import dataclass
from fastapi import Request, HTTPException
from sqlalchemy.orm import Session
import uuid

from app.models.tenant import Tenant

@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    tenant_slug: str
    tenant_name: str

def get_or_create_tenant(db: Session, slug: str) -> Tenant:
    slug = (slug or "").strip().lower() or "default"
    t = db.query(Tenant).filter(Tenant.slug == slug).first()
    if t:
        return t
    t = Tenant(id=str(uuid.uuid4()), slug=slug, name=slug.replace("-", " ").title(), status="active")
    db.add(t)
    db.commit()
    db.refresh(t)
    return t

def resolve_tenant(db: Session, request: Request, tenant_slug: str | None = None) -> TenantContext:
    # Priority: explicit path param -> query param -> session -> default
    slug = tenant_slug or request.path_params.get("tenant_slug") or request.query_params.get("tenant") or ""
    if not slug:
        slug = request.session.get("tenant_slug") if hasattr(request, "session") else ""
    t = get_or_create_tenant(db, slug)
    if (t.status or "active") != "active":
        raise HTTPException(status_code=403, detail="Tenant suspended")
    # stash in session for convenience
    try:
        request.session["tenant_slug"] = t.slug
        request.session["tenant_id"] = t.id
    except Exception:
        pass
    return TenantContext(tenant_id=t.id, tenant_slug=t.slug, tenant_name=t.name)

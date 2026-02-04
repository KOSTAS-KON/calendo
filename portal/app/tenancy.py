from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Type

from fastapi import HTTPException, Request
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Query, Session

from app.models.tenant import Tenant


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    tenant_slug: str
    tenant_name: str
    status: str


def resolve_tenant(db: Session, request: Request, tenant_slug: str = "default") -> TenantContext:
    slug = (tenant_slug or "default").strip().lower() or "default"

    # Defensive rollback: clears aborted transactions safely.
    try:
        if db.in_transaction():
            db.rollback()
    except Exception:
        pass

    try:
        t = db.query(Tenant).filter(Tenant.slug == slug).first()
    except SQLAlchemyError:
        try:
            db.rollback()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail="Database error while resolving tenant")

    if not t:
        raise HTTPException(status_code=404, detail=f"Tenant '{slug}' not found")
    if (t.status or "active") != "active":
        raise HTTPException(status_code=403, detail="Tenant is suspended")

    return TenantContext(tenant_id=t.id, tenant_slug=t.slug, tenant_name=t.name, status=t.status)


def tenant_query(model: Type[Any], db: Session, tenant_id: str) -> Query:
    if not hasattr(model, "tenant_id"):
        raise RuntimeError(f"Model {getattr(model, '__name__', str(model))} is not tenant-scoped (missing tenant_id).")
    return db.query(model).filter(getattr(model, "tenant_id") == tenant_id)

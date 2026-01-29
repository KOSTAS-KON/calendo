from __future__ import annotations

from pydantic import BaseModel
from typing import Optional


class PlanInfo(BaseModel):
    code: Optional[str] = None
    name: Optional[str] = None


class LicenseStatus(BaseModel):
    tenant: str
    active: bool
    until: Optional[str] = None
    plan: Optional[PlanInfo] = None

from __future__ import annotations

from pydantic import BaseModel
from typing import Optional


class ClinicSettingsOut(BaseModel):
    tenant: str
    clinic_name: str = ""
    address: str = ""
    google_maps_link: str = ""
    lat: Optional[float] = None
    lng: Optional[float] = None
    sms_provider: str = "infobip"

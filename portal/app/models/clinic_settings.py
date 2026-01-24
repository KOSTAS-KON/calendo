from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ClinicSettings(Base):
    __tablename__ = "clinic_settings"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_clinic_settings_tenant"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), index=True)

    clinic_name: Mapped[str] = mapped_column(String(200), default="Therapy Portal")
    address: Mapped[str] = mapped_column(String(300), default="")
    google_maps_link: Mapped[str] = mapped_column(String(1000), default="")

    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)

    sms_provider: Mapped[str] = mapped_column(String(50), default="infobip")

    infobip_base_url: Mapped[str] = mapped_column(String(300), default="https://api.infobip.com")
    infobip_api_key: Mapped[str] = mapped_column(String(400), default="")
    infobip_sender: Mapped[str] = mapped_column(String(100), default="")
    infobip_username: Mapped[str] = mapped_column(String(200), default="")
    infobip_userkey: Mapped[str] = mapped_column(String(300), default="")

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AppLicense(Base):
    """Legacy single-row license settings (kept for backwards compatibility).

    Multi-tenant licensing should use the subscriptions tables.
    This model must NOT include tenant_id unless the database has that column.
    """

    __tablename__ = "app_license"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    product_mode: Mapped[str] = mapped_column(String(20), default="BOTH")
    client_id: Mapped[str] = mapped_column(String(120), default="")
    activation_token: Mapped[str] = mapped_column(String(2000), default="")
    plan: Mapped[int] = mapped_column(Integer, default=0)
    activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    trial_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    license_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

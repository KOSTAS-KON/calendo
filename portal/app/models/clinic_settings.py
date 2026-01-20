from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class ClinicSettings(Base):
    """Singleton clinic configuration for branding + SMS credentials."""

    __tablename__ = "clinic_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    clinic_name: Mapped[str] = mapped_column(String(200), default="Therapy Portal")
    address: Mapped[str] = mapped_column(String(300), default="")
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lng: Mapped[float | None] = mapped_column(Float, nullable=True)

    # SMS Provider (per-client)
    sms_provider: Mapped[str] = mapped_column(String(50), default="infobip")

    # Infobip (per-client)
    infobip_base_url: Mapped[str] = mapped_column(String(300), default="https://api.infobip.com")
    infobip_api_key: Mapped[str] = mapped_column(String(400), default="")
    infobip_sender: Mapped[str] = mapped_column(String(100), default="")
    infobip_username: Mapped[str] = mapped_column(String(200), default="")
    infobip_userkey: Mapped[str] = mapped_column(String(300), default="")

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AppLicense(Base):
    """Simple local license/trial settings.

    This is a lightweight local licensing/trial mechanism intended for offline deployments and pilots.
    """

    __tablename__ = "app_license"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)

    # PORTAL / SMS / BOTH
    product_mode: Mapped[str] = mapped_column(String(20), default="BOTH")

    # Activation (Option A: signed offline codes)
    client_id: Mapped[str] = mapped_column(String(120), default="")
    activation_token: Mapped[str] = mapped_column(String(2000), default="")
    plan: Mapped[int] = mapped_column(Integer, default=0)  # 1=1w, 2=1m, 3=1y
    activated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Trial & expiry
    trial_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    license_end: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

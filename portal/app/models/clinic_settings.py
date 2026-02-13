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

    # ---------------------------------------------------------------------
    # Template / backwards-compat aliases
    # ---------------------------------------------------------------------
    @property
    def name(self) -> str:
        """Alias used by templates (suite, etc.)."""
        return self.clinic_name

    @property
    def latitude(self) -> float | None:
        """Legacy alias."""
        return self.lat

    @property
    def longitude(self) -> float | None:
        """Legacy alias."""
        return self.lng

    @property
    def map_url(self) -> str:
        """A user-friendly Google Maps URL (best-effort)."""
        if self.google_maps_link:
            return self.google_maps_link
        if self.lat is not None and self.lng is not None:
            return f"https://www.google.com/maps?q={self.lat},{self.lng}"
        if self.address:
            q = self.address.replace(" ", "+")
            return f"https://www.google.com/maps/search/?api=1&query={q}"
        return ""


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

    def effective_end(self) -> datetime | None:
        """Return the end of access (license_end preferred, else trial_end)."""
        return self.license_end or self.trial_end

    def days_left(self, now: datetime | None = None) -> int | None:
        """Return whole days remaining for trial/license (0..n), or None."""
        end = self.effective_end()
        if not end:
            return None
        now = now or datetime.utcnow()
        delta = end - now
        return max(0, int(delta.total_seconds() // 86400))

    def is_active(self, now: datetime | None = None) -> bool:
        end = self.effective_end()
        if not end:
            return False
        now = now or datetime.utcnow()
        return end > now

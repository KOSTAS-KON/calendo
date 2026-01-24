from __future__ import annotations

from datetime import datetime
from sqlalchemy import String, Integer, DateTime, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    duration_days: Mapped[int] = mapped_column(Integer, default=0)
    features_json: Mapped[str] = mapped_column(Text, default="{}")


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), index=True)

    plan_id: Mapped[int] = mapped_column(Integer, ForeignKey("plans.id"))
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|expired|canceled

    starts_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    ends_at: Mapped[datetime] = mapped_column(DateTime)
    source: Mapped[str] = mapped_column(String(40), default="manual")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ActivationCode(Base):
    __tablename__ = "activation_codes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), index=True)

    plan_id: Mapped[int] = mapped_column(Integer, ForeignKey("plans.id"))
    code_hash: Mapped[str] = mapped_column(String(200), unique=True, index=True)

    issued_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    redeem_by: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    max_redemptions: Mapped[int] = mapped_column(Integer, default=1)
    redeemed_count: Mapped[int] = mapped_column(Integer, default=0)

    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    note: Mapped[str] = mapped_column(String(300), default="")


class LicenseAuditLog(Base):
    __tablename__ = "license_audit_log"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), index=True)

    event_type: Mapped[str] = mapped_column(String(40))
    details_json: Mapped[str] = mapped_column(Text, default="{}")

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Tenant scoping (already present) :contentReference[oaicite:2]{index=2}
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), index=True)

    child_id: Mapped[int] = mapped_column(Integer, ForeignKey("children.id"), index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime, index=True)

    therapist_name: Mapped[str] = mapped_column(String(200), default="")
    procedure: Mapped[str] = mapped_column(String(200), default="Session")

    attendance_status: Mapped[str] = mapped_column(String(40), default="UNCONFIRMED")

    # Existing relationship :contentReference[oaicite:3]{index=3}
    child = relationship("Child", back_populates="appointments")

    # FIX: SessionNote expects back_populates="session_note" on Appointment side :contentReference[oaicite:4]{index=4}
    # Your SessionNote.appointment_id is unique=True (one-to-one), so set uselist=False.
    session_note = relationship(
        "SessionNote",
        back_populates="appointment",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

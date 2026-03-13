from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Integer, String, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)

    # Tenant scoping (already present)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), index=True)

    child_id: Mapped[int] = mapped_column(Integer, ForeignKey("children.id"), index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime, index=True)

    therapist_name: Mapped[str] = mapped_column(String(200), default="")
    procedure: Mapped[str] = mapped_column(String(200), default="Session")

    # Scheduling / reporting classification
    duration_minutes: Mapped[int] = mapped_column(Integer, default=60)
    appointment_type: Mapped[str] = mapped_column(String(40), default="UNSPECIFIED")
    therapy_type: Mapped[str] = mapped_column(String(40), default="UNSPECIFIED")

    # Once hours are finalised for a reporting/claim window, we mark them as counted
    # so future calculations can exclude them by default.
    hours_counted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    hours_counted_label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    hours_counted_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)

    attendance_status: Mapped[str] = mapped_column(String(40), default="UNCONFIRMED")

    child = relationship("Child", back_populates="appointments")

    # one-to-one session note
    session_note = relationship(
        "SessionNote",
        back_populates="appointment",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ForeignKey, Text, DateTime, func, String, Integer
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class SessionNote(Base):
    __tablename__ = "session_notes"

    id: Mapped[int] = mapped_column(primary_key=True)

    # enforce tenant_id on notes (required for multi-tenant consistency)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), index=True)

    # one note per appointment (unique=True)
    appointment_id: Mapped[int] = mapped_column(
        ForeignKey("appointments.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )

    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    what_went_wrong: Mapped[str | None] = mapped_column(Text, nullable=True)
    improvements: Mapped[str | None] = mapped_column(Text, nullable=True)
    next_steps: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), onupdate=func.now())

    # must match Appointment.session_note relationship name
    appointment = relationship("Appointment", back_populates="session_note")

    activities = relationship(
        "ActivityItem",
        back_populates="session_note",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    attachments = relationship(
        "Attachment",
        back_populates="session_note",
        passive_deletes=True,
    )


class ActivityItem(Base):
    __tablename__ = "activity_items"

    id: Mapped[int] = mapped_column(primary_key=True)

    session_note_id: Mapped[int] = mapped_column(
        ForeignKey("session_notes.id", ondelete="CASCADE"),
        index=True,
    )

    title: Mapped[str] = mapped_column(String(200))
    duration_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result: Mapped[str | None] = mapped_column(String(200), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    session_note = relationship("SessionNote", back_populates="activities")

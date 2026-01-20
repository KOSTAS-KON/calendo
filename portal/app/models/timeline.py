from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class TimelineEvent(Base):
    """Patient journey timeline.

    This table is intentionally generic: it can represent payments, visits,
    invoice events, exercises, parent communications, cancellations, etc.

    We keep optional links to related entities (appointment / billing row)
    for richer navigation without enforcing it.
    """

    __tablename__ = "timeline_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    child_id: Mapped[int] = mapped_column(ForeignKey("children.id"), index=True)

    # e.g. VISIT, PAYMENT, INVOICE_ISSUED, EXERCISE, PARENT_FEEDBACK, APPT_CANCELLED, NOTE, OTHER
    event_type: Mapped[str] = mapped_column(String(40), index=True)

    title: Mapped[str] = mapped_column(String(220))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)

    occurred_at: Mapped[datetime] = mapped_column(DateTime, index=True)

    # Optional links
    appointment_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    billing_item_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    child = relationship("Child", back_populates="timeline_events")

from __future__ import annotations

from sqlalchemy import String, Date, Text, ForeignKey, Boolean, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Child(Base):
    __tablename__ = "children"

    id: Mapped[int] = mapped_column(primary_key=True)

    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), index=True)

    full_name: Mapped[str] = mapped_column(String(200), index=True)
    date_of_birth: Mapped[Date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    parent1_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    parent1_phone: Mapped[str | None] = mapped_column(String(80), nullable=True)
    parent2_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    parent2_phone: Mapped[str | None] = mapped_column(String(80), nullable=True)

    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    archived_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)

    therapist_assignments = relationship(
        "ChildTherapistAssignment",
        back_populates="child",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    appointments = relationship(
        "Appointment",
        back_populates="child",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    attachments = relationship(
        "Attachment",
        back_populates="child",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    billing_items = relationship(
        "BillingItem",
        back_populates="child",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    billing_plans = relationship(
        "BillingPlan",
        back_populates="child",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    timeline_events = relationship(
        "TimelineEvent",
        back_populates="child",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

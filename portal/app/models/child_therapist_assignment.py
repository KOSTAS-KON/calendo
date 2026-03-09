from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ChildTherapistAssignment(Base):
    __tablename__ = "child_therapist_assignments"
    __table_args__ = (
        UniqueConstraint("tenant_id", "child_id", "therapist_id", name="uq_child_therapist_assignment"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), index=True)
    child_id: Mapped[int] = mapped_column(Integer, ForeignKey("children.id"), index=True)
    therapist_id: Mapped[int] = mapped_column(Integer, ForeignKey("therapists.id"), index=True)

    assigned_by_user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    assigned_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    child = relationship("Child", back_populates="therapist_assignments")
    therapist = relationship("Therapist", back_populates="child_assignments")

from __future__ import annotations

from sqlalchemy import String, Date, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Child(Base):
    __tablename__ = "children"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Tenant scoping already present :contentReference[oaicite:5]{index=5}
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), index=True)

    full_name: Mapped[str] = mapped_column(String(200), index=True)
    date_of_birth: Mapped[Date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    parent1_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    parent1_phone: Mapped[str | None] = mapped_column(String(80), nullable=True)
    parent2_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    parent2_phone: Mapped[str | None] = mapped_column(String(80), nullable=True)

    # Keep existing appointments relationship but make it consistent with Appointment.child back_populates :contentReference[oaicite:6]{index=6}
    appointments = relationship(
        "Appointment",
        back_populates="child",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # FIX: Add missing property that Attachment.child expects (back_populates="attachments") :contentReference[oaicite:7]{index=7}
    attachments = relationship(
        "Attachment",
        back_populates="child",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

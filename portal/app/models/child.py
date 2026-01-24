from __future__ import annotations

from sqlalchemy import String, Date, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Child(Base):
    __tablename__ = "children"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Keep existing column exactly :contentReference[oaicite:2]{index=2}
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), index=True)

    full_name: Mapped[str] = mapped_column(String(200), index=True)
    date_of_birth: Mapped[Date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    parent1_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    parent1_phone: Mapped[str | None] = mapped_column(String(80), nullable=True)
    parent2_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    parent2_phone: Mapped[str | None] = mapped_column(String(80), nullable=True)

    # Existing relationship kept, but add safe delete behavior
    appointments = relationship(
        "Appointment",
        back_populates="child",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # FIX: Attachment.child uses back_populates="attachments" :contentReference[oaicite:3]{index=3}
    attachments = relationship(
        "Attachment",
        back_populates="child",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # FIX: BillingItem.child expects Child.billing_items (your current crash)
    billing_items = relationship(
        "BillingItem",
        back_populates="child",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

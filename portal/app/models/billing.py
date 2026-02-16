from __future__ import annotations

from datetime import date

from sqlalchemy import ForeignKey, Date, String, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base

YES_NO = ("YES", "NO")


class BillingItem(Base):
    """
    Simple billing rows per child.

    Flags are stored as "YES"/"NO" strings (matches your Excel workflow).
    """
    __tablename__ = "billing_items"

    id: Mapped[int] = mapped_column(primary_key=True)

    tenant_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        index=True,
    )

    child_id: Mapped[int] = mapped_column(
        ForeignKey("children.id", ondelete="CASCADE"),
        index=True,
    )

    billing_due: Mapped[date] = mapped_column(Date, index=True)

    # Optional billing details (patched in Feb 2026)
    # Stored in the DB so Calendar/Billing can show amounts and descriptions.
    amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str] = mapped_column(String(8), default="EUR")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    paid: Mapped[str] = mapped_column(String(3), default="NO")
    invoice_created: Mapped[str] = mapped_column(String(3), default="NO")
    parent_signed_off: Mapped[str] = mapped_column(String(3), default="NO")

    child = relationship("Child", back_populates="billing_items")

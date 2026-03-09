from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func, select
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.models.child import Child


class TimelineEvent(Base):
    """Patient journey timeline.

    We intentionally keep tenant scoping via the related Child row to stay
    compatible with older databases that may not have a physical
    `timeline_events.tenant_id` column.
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

    @hybrid_property
    def tenant_id(self) -> str | None:
        # Runtime convenience; comes from related Child
        try:
            return getattr(self.child, "tenant_id", None)
        except Exception:
            return None

    @tenant_id.inplace.expression
    @classmethod
    def _tenant_id_expression(cls):
        # SQL expression for tenant-safe filtering without a physical column
        return (
            select(Child.tenant_id)
            .where(Child.id == cls.child_id)
            .scalar_subquery()
        )

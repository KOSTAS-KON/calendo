from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func, select
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.ext.hybrid import hybrid_property

from app.db import Base


class TimelineEvent(Base):
    """Patient journey timeline scoped through child -> tenant."""

    __tablename__ = "timeline_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    child_id: Mapped[int] = mapped_column(ForeignKey("children.id"), index=True)

    event_type: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(220))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime, index=True)

    appointment_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    billing_item_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    child = relationship("Child", back_populates="timeline_events")

    @hybrid_property
    def tenant_id(self):
        child = getattr(self, "child", None)
        return getattr(child, "tenant_id", None)

    @tenant_id.expression
    def tenant_id(cls):
        from app.models.child import Child
        return select(Child.tenant_id).where(Child.id == cls.child_id).scalar_subquery()

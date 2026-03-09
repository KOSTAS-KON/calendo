from sqlalchemy import String, Text, ForeignKey, Boolean, DateTime
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base

class Therapist(Base):
    __tablename__ = "therapists"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), index=True)
    user_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("users.id"), nullable=True, index=True)

    name: Mapped[str] = mapped_column(String(200), index=True)
    phone: Mapped[str | None] = mapped_column(String(80), nullable=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    role: Mapped[str | None] = mapped_column(String(120), nullable=True)

    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    archived_at: Mapped[DateTime | None] = mapped_column(DateTime, nullable=True)

    availability_json: Mapped[str] = mapped_column(Text, default="{}")
    annual_leave_json: Mapped[str] = mapped_column(Text, default="[]")

    child_assignments = relationship(
        "ChildTherapistAssignment",
        back_populates="therapist",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

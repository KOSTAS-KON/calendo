from __future__ import annotations

from datetime import datetime
from sqlalchemy import String, Boolean, DateTime, ForeignKey, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)

    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), index=True)

    email: Mapped[str] = mapped_column(String(255), index=True)  # store lowercase
    password_hash: Mapped[str] = mapped_column(String(2000))

    role: Mapped[str] = mapped_column(String(32), default="calendar_staff")  # clinic_superuser|calendar_staff|therapist|owner|admin|staff|read_only
    job_title: Mapped[str | None] = mapped_column(String(120), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    must_reset_password: Mapped[bool] = mapped_column(Boolean, default=False)


    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

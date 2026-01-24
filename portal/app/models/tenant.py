from __future__ import annotations

from datetime import datetime
from sqlalchemy import String, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Tenant(Base):
    __tablename__ = "tenants"

    # Use string UUID for portability across SQLite/Postgres
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    status: Mapped[str] = mapped_column(String(20), default="active")  # active|suspended

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

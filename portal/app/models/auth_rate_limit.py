from __future__ import annotations

from datetime import datetime
from sqlalchemy import String, Integer, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class AuthRateLimit(Base):
    __tablename__ = "auth_rate_limits"

    ip: Mapped[str] = mapped_column(String(64), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    count: Mapped[int] = mapped_column(Integer, default=0)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

from __future__ import annotations

from datetime import datetime

from sqlalchemy import String, Text, DateTime
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


class Therapist(Base):
    """Master list of therapists/providers.

    Availability and annual leave are stored as JSON strings (Text) to keep
    the packaging light (no Alembic required for the distributable).

    availability_json example:
      {
        "mon": [{"start": "09:00", "end": "13:00"}, {"start": "14:00", "end": "18:00"}],
        "tue": [{"start": "09:00", "end": "13:00"}],
        ...
      }

    annual_leave_json example:
      [
        {"start": "2026-08-01", "end": "2026-08-10", "reason": "Vacation"},
        ...
      ]
    """

    __tablename__ = "therapists"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200), index=True)

    phone: Mapped[str | None] = mapped_column(String(80), nullable=True)
    email: Mapped[str | None] = mapped_column(String(200), nullable=True)
    role: Mapped[str | None] = mapped_column(String(120), nullable=True)

    availability_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    annual_leave_json: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

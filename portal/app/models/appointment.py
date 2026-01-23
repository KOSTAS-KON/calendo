from datetime import datetime
from sqlalchemy import ForeignKey, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base

class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), index=True)
    child_id: Mapped[int] = mapped_column(ForeignKey("children.id"), index=True)

    starts_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    therapist_name: Mapped[str] = mapped_column(String(120))
    procedure: Mapped[str] = mapped_column(String(160), default="Office Visit")

    # Attendance archive status:
    # CONFIRMED, UNCONFIRMED, CANCELLED_PROVIDER, CANCELLED_ME, MISSED, ATTENDED
    attendance_status: Mapped[str] = mapped_column(String(40), default="UNCONFIRMED", index=True)
    attendance_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    attendance_marked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    child = relationship("Child", back_populates="appointments")
    session_note = relationship("SessionNote", back_populates="appointment", uselist=False, cascade="all, delete-orphan")

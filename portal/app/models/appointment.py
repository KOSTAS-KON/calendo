from datetime import datetime
from sqlalchemy import DateTime, Integer, String, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base

class Appointment(Base):
    __tablename__ = "appointments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), index=True)

    child_id: Mapped[int] = mapped_column(Integer, ForeignKey("children.id"), index=True)
    starts_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    ends_at: Mapped[datetime] = mapped_column(DateTime, index=True)

    therapist_name: Mapped[str] = mapped_column(String(200), default="")
    procedure: Mapped[str] = mapped_column(String(200), default="Session")

    attendance_status: Mapped[str] = mapped_column(String(40), default="UNCONFIRMED")

    child = relationship("Child", back_populates="appointments")

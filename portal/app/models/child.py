from sqlalchemy import String, Date, Text, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base

class Child(Base):
    __tablename__ = "children"

    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(36), ForeignKey("tenants.id"), index=True)

    full_name: Mapped[str] = mapped_column(String(200), index=True)
    date_of_birth: Mapped[Date | None] = mapped_column(Date, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    parent1_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    parent1_phone: Mapped[str | None] = mapped_column(String(80), nullable=True)
    parent2_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    parent2_phone: Mapped[str | None] = mapped_column(String(80), nullable=True)

    appointments = relationship("Appointment", back_populates="child")

from datetime import date
from sqlalchemy import ForeignKey, Date, String, Integer, Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base

class BillingPlan(Base):
    __tablename__ = "billing_plans"

    id: Mapped[int] = mapped_column(primary_key=True)
    child_id: Mapped[int] = mapped_column(ForeignKey("children.id"), index=True)

    # weekly or monthly
    frequency: Mapped[str] = mapped_column(String(16), default="monthly")  # weekly|monthly

    # weekly: every_n_weeks
    every_n_weeks: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # monthly: day_of_month (1-28/30/31)
    day_of_month: Mapped[int | None] = mapped_column(Integer, nullable=True)

    start_date: Mapped[date] = mapped_column(Date, index=True)
    until_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    indefinitely: Mapped[bool] = mapped_column(Boolean, default=False)

    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    child = relationship("Child", back_populates="billing_plans")

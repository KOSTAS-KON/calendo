from datetime import date
from sqlalchemy import ForeignKey, Date, String
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base

class BillingItem(Base):
    __tablename__ = "billing_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    child_id: Mapped[int] = mapped_column(ForeignKey("children.id"), index=True)

    billing_due: Mapped[date] = mapped_column(Date, index=True)

    # YES / NO values for simplicity (matches your Excel)
    paid: Mapped[str] = mapped_column(String(3), default="NO")             # YES / NO
    invoice_created: Mapped[str] = mapped_column(String(3), default="NO")  # YES / NO
    parent_signed_off: Mapped[str] = mapped_column(String(3), default="NO")# YES / NO

    child = relationship("Child", back_populates="billing_items")

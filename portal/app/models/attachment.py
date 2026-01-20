from sqlalchemy import ForeignKey, String, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base

class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)
    child_id: Mapped[int] = mapped_column(ForeignKey("children.id"), index=True)
    session_note_id: Mapped[int | None] = mapped_column(ForeignKey("session_notes.id"), nullable=True, index=True)

    original_name: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(120))
    storage_path: Mapped[str] = mapped_column(String(500))

    created_at: Mapped[str] = mapped_column(DateTime, server_default=func.now())

    child = relationship("Child", back_populates="attachments")
    session_note = relationship("SessionNote", back_populates="attachments")

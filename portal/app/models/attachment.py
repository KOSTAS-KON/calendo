from __future__ import annotations

from sqlalchemy import ForeignKey, String, DateTime, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class Attachment(Base):
    __tablename__ = "attachments"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Keep existing columns exactly :contentReference[oaicite:2]{index=2}
    child_id: Mapped[int] = mapped_column(
        ForeignKey("children.id", ondelete="CASCADE"),
        index=True,
    )
    session_note_id: Mapped[int | None] = mapped_column(
        ForeignKey("session_notes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    original_name: Mapped[str] = mapped_column(String(255))
    mime_type: Mapped[str] = mapped_column(String(120))
    storage_path: Mapped[str] = mapped_column(String(500))

    created_at: Mapped[str] = mapped_column(DateTime, server_default=func.now())

    # Relationships must match the other side names:
    # Child must provide `attachments` (we add it in child.py) :contentReference[oaicite:3]{index=3}
    child = relationship("Child", back_populates="attachments")

    # SessionNote must provide `attachments` (your session_note.py should have it) :contentReference[oaicite:4]{index=4}
    session_note = relationship("SessionNote", back_populates="attachments")

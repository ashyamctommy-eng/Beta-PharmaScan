"""
models/resource.py
------------------
SQLAlchemy ORM model for study resource metadata.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


class Resource(Base):
    __tablename__ = "resources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True, autoincrement=True)

    title: Mapped[str] = mapped_column(String(512), nullable=False)
    subject: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    semester: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    file_name: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)

    upload_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    __table_args__ = (
        Index("ix_resources_semester_subject", "semester", "subject"),
    )

    def __repr__(self) -> str:
        return f"<Resource id={self.id!r} title={self.title!r}>"

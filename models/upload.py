"""
models/upload.py
----------------
The document bytes themselves, when `STORAGE_BACKEND=database`.

Why this exists: free container hosts (Render, Koyeb, rollout.host) hand you an
**ephemeral filesystem** — a restart or a sleep wipes everything written to disk. The
database is the one thing that survives, so on those hosts the uploaded PDFs live in
a table next to the summaries and the token ledger, and the app becomes stateless.

On hosts with a real disk (cPanel, a VPS, PythonAnywhere) the default `disk` backend
is used instead and this table stays empty.
"""

from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, LargeBinary, String, func
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


class UploadedFile(Base):
    __tablename__ = "uploaded_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    resource_id: Mapped[int] = mapped_column(
        ForeignKey("resources.id", ondelete="CASCADE"), index=True, unique=True, nullable=False)

    file_name: Mapped[str] = mapped_column(String(512), nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), default="application/octet-stream",
                                              nullable=False)
    size: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), index=True, default="", nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc), server_default=func.now())

    def __repr__(self) -> str:  # pragma: no cover
        return f"<UploadedFile resource={self.resource_id} {self.size}B>"

"""
models/summary.py
-----------------
ORM models for the PDF -> short-notes pipeline.

Three tables, each earning its place:
  * `Summary`        — one row per document (keyed by file hash), holds the outline
                       and the final notes, plus progress and tokens spent.
  * `SummarySection` — one row per section, so a run that hits the daily token
                       budget resumes tomorrow instead of starting over.
  * `UsageEvent`     — one row per AI call, so the daily budget can be enforced.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from core.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Summary(Base):
    __tablename__ = "summaries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    resource_id: Mapped[int] = mapped_column(Integer, index=True, nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    file_name: Mapped[str] = mapped_column(String(512), nullable=False)

    # pending | running | done | budget_exhausted | rate_limited | failed
    status: Mapped[str] = mapped_column(String(24), default="pending", nullable=False)
    depth: Mapped[str] = mapped_column(String(16), default="standard", nullable=False)
    model: Mapped[str] = mapped_column(String(128), default="", nullable=False)

    pages: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sections_total: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sections_done: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    tokens_spent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    outline_json: Mapped[str] = mapped_column(Text, default="", nullable=False)
    notes_json: Mapped[str] = mapped_column(Text, default="", nullable=False)
    warnings_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)
    error: Mapped[str] = mapped_column(Text, default="", nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    sections: Mapped[list["SummarySection"]] = relationship(
        back_populates="summary", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Summary id={self.id} {self.file_name!r} {self.status}>"


class SummarySection(Base):
    __tablename__ = "summary_sections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    summary_id: Mapped[int] = mapped_column(ForeignKey("summaries.id", ondelete="CASCADE"), index=True)

    section_id: Mapped[str] = mapped_column(String(32), nullable=False)
    heading: Mapped[str] = mapped_column(String(512), nullable=False)
    page: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    end_page: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    importance: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # pending | done | skipped | error
    status: Mapped[str] = mapped_column(String(16), default="pending", nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, default="", nullable=False)
    tokens_spent: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    summary: Mapped[Summary] = relationship(back_populates="sections")

    def __repr__(self) -> str:  # pragma: no cover
        return f"<SummarySection {self.section_id} {self.status}>"


class UsageEvent(Base):
    __tablename__ = "usage_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)      # outline | section | reduce
    model: Mapped[str] = mapped_column(String(128), default="", nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    resource_id: Mapped[int] = mapped_column(Integer, default=0, nullable=False, index=True)
    client: Mapped[str] = mapped_column(String(64), default="", nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, server_default=func.now(), index=True
    )

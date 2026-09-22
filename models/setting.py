"""
models/setting.py
-----------------
Panel-editable settings, stored in the database so the admin can change them
without touching `.env` or restarting Passenger.

Precedence is **panel (DB) > server (.env / environment) > built-in default**.
Values are JSON-encoded so booleans and integers survive a round trip.
"""

from datetime import datetime, timezone

from sqlalchemy import DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from core.database import Base


class AppSetting(Base):
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    def __repr__(self) -> str:  # pragma: no cover
        return f"<AppSetting {self.key}>"

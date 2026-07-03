"""
schemas/resource.py
-------------------
Pydantic v2 schemas for resource API.
"""

from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

SemesterLiteral = Literal["Y1S1", "Y1S2", "Y2S1", "Y2S2", "Y3S1", "Y3S2"]


class ResourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    title: str
    subject: str
    semester: str
    file_name: str
    file_path: str
    upload_date: datetime


class ResourceListResponse(BaseModel):
    total: int
    items: list[ResourceOut]


class MessageResponse(BaseModel):
    message: str
    detail: Optional[str] = None


class SemesterCount(BaseModel):
    semester: str
    count: int


class SubjectCount(BaseModel):
    subject: str
    count: int


class ResourceStats(BaseModel):
    total: int
    by_semester: list[SemesterCount]
    by_subject: list[SubjectCount]
    recent: list[ResourceOut]

"""
schemas/analysis.py
-------------------
Pydantic v2 schemas for AI analysis endpoint.
"""

from typing import Optional
from pydantic import BaseModel, Field


class AnalysisRequest(BaseModel):
    """
    Payload for POST /api/analyze.
    - text_only=True  → skip file, route directly to text pipeline
    - text_only=False → file_data (base64) + file_name expected
    """
    prompt: str = Field(..., min_length=1, description="User's analysis prompt or question")
    file_data: Optional[str] = Field(None, description="Base64-encoded file content (without data URI prefix)")
    file_name: Optional[str] = Field(None, description="Original filename with extension")
    text_only: bool = Field(False, description="If True, skip file validation and use text-only pipeline")
    subject: Optional[str] = Field(None, description="CDACC subject/unit context selected by student")


class Pharmacy180Ref(BaseModel):
    concept: str
    summary: str
    url: str


class AnalysisResponse(BaseModel):
    analysis: str
    concept: Optional[str] = None
    pharmacy180_ref: Optional[Pharmacy180Ref] = None
    model: str
    tokens_used: Optional[int] = None
